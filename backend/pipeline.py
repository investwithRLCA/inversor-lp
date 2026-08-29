"""
Orquestador del embudo: DATOS → FILTRO → (solo si procede) IA.

Flujo por ticker:
  1. ¿Hay snapshot vigente en Supabase?      → sí: reutiliza. no: llama a EDGAR/Finnhub.
  2. Calcula data_hash sobre las métricas (SIN el precio).
  3. Cribado cuantitativo. Si no pasa → se guarda el rechazo y se acaba. Coste: 0 tokens.
  4. ¿Existe ya un análisis con ese data_hash + prompt_version + model? → devuelve el guardado.
  5. Si no, construye el payload compacto y llama al LLM con prompt caching.
  6. Persiste informe, métricas de coste y campos estructurados.

Dependencias: requests, supabase
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from providers import Fundamentals, build_fundamentals
from quant_filter import RULESET_VERSION, ScreenResult, build_llm_payload, screen
from llm_gemini import GEMINI_MODEL, GEMINI_MODEL_FALLBACK, call_gemini

log = logging.getLogger(__name__)

PROMPT_VERSION = "analista-v1.3"
MODEL = GEMINI_MODEL
# Un análisis puede haberlo respondido el modelo principal o el de reserva
# (ver llm_gemini.call_gemini): la caché tiene que reconocer cualquiera de
# los dos como válido, o cada vez que el principal falle una vez se pierde
# la caché de esa empresa y se vuelve a pagar el análisis entero.
MODELOS_CACHE = [GEMINI_MODEL] + (
    [GEMINI_MODEL_FALLBACK] if GEMINI_MODEL_FALLBACK != GEMINI_MODEL else []
)
SNAPSHOT_TTL_DAYS = 90

SYSTEM_PROMPT = (Path(__file__).parent.parent / "prompts" / "system_prompt_es.md").read_text(
    encoding="utf-8"
)


# --------------------------------------------------------------------------- #
# Hash de invalidación
# --------------------------------------------------------------------------- #

VOLATILE = {"per", "ev_ebit", "p_fcf", "p_book", "dividend_yield"}  # dependen del precio


def data_hash(metrics: dict[str, Any]) -> str:
    """Huella estable de los fundamentales. Excluye todo lo que se mueve con la
    cotización: que la acción suba un 5 % no invalida una tesis a 10 años."""
    clean = {k: v for k, v in sorted(metrics.items()) if k not in VOLATILE}
    blob = json.dumps(clean, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Repositorio Supabase
# --------------------------------------------------------------------------- #

class Repo:
    def __init__(self, client=None):
        if client is None:
            from supabase import create_client  # import perezoso

            client = create_client(
                os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"]
            )
        self.db = client

    # -- companies ------------------------------------------------------- #
    def upsert_company(self, f: Fundamentals) -> str:
        row = {
            "ticker": f.ticker,
            "cik": f.cik,
            "name": f.name,
            "sector": f.sector,
            "country": f.country,
            "currency": f.currency,
            "is_financial": f.is_financial,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        res = self.db.table("companies").upsert(row, on_conflict="ticker").execute()
        return res.data[0]["id"]

    # -- snapshots ------------------------------------------------------- #
    def fresh_snapshot(self, ticker: str) -> dict | None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=SNAPSHOT_TTL_DAYS)).isoformat()
        res = (
            self.db.table("fundamentals_snapshots")
            .select("*, companies!inner(ticker)")
            .eq("companies.ticker", ticker.upper())
            .gte("fetched_at", cutoff)
            .order("as_of", desc=True)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None

    def save_snapshot(self, company_id: str, f: Fundamentals, metrics: dict, h: str) -> str:
        row = {
            "company_id": company_id,
            "source": "mixto",
            "as_of": f.as_of.isoformat(),
            "last_fy": f.fiscal_years[-1] if f.fiscal_years else None,
            "raw": f.to_dict(),
            "metrics": metrics,
            "data_hash": h,
            "price": f.price,
            "price_at": datetime.now(timezone.utc).isoformat(),
        }
        res = (
            self.db.table("fundamentals_snapshots")
            .upsert(row, on_conflict="company_id,source,as_of")
            .execute()
        )
        return res.data[0]["id"]

    # -- screens --------------------------------------------------------- #
    def save_screen(self, company_id: str, snapshot_id: str, r: ScreenResult) -> str:
        row = {
            "company_id": company_id,
            "snapshot_id": snapshot_id,
            "ruleset_version": r.ruleset_version,
            "score": r.score,
            "passed": r.passed,
            "vetoes": r.vetoes,
            "warnings": r.warnings,
            "breakdown": r.breakdown,
        }
        res = (
            self.db.table("quant_screens")
            .upsert(row, on_conflict="snapshot_id,ruleset_version")
            .execute()
        )
        return res.data[0]["id"]

    # -- analyses -------------------------------------------------------- #
    def cached_analysis(self, company_id: str, h: str) -> dict | None:
        res = (
            self.db.table("ai_analyses")
            .select("*")
            .eq("company_id", company_id)
            .eq("data_hash", h)
            .eq("prompt_version", PROMPT_VERSION)
            .in_("model", MODELOS_CACHE)
            .gt("expires_at", datetime.now(timezone.utc).isoformat())
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None

    def save_analysis(self, row: dict) -> dict:
        res = (
            self.db.table("ai_analyses")
            .upsert(row, on_conflict="company_id,data_hash,prompt_version,model")
            .execute()
        )
        return res.data[0]


# --------------------------------------------------------------------------- #
# Llamada al LLM
# --------------------------------------------------------------------------- #

def call_claude(payload: str) -> dict:
    """Nombre histórico (el proyecto empezó con Claude); ahora llama a Gemini.
    Se mantiene el nombre para no romper `probar_ia.py` ni tus propios scripts."""
    return call_gemini(SYSTEM_PROMPT, payload, max_tokens=2000, temperature=0.2)


# --------------------------------------------------------------------------- #
# Parseo del informe
# --------------------------------------------------------------------------- #

_VERDICTS = ("COMPRA_FUERTE", "COMPRA", "VIGILAR", "NO_INVERTIBLE", "DESCARTE")


def parse_report(md: str) -> dict:
    """Extrae los campos estructurados del Markdown para poder ordenar y filtrar
    en la app sin volver a leer el texto."""
    out: dict[str, Any] = {}

    for v in _VERDICTS:                       # el orden importa: COMPRA_FUERTE antes que COMPRA
        if re.search(rf"\*\*Veredicto:\*\*\s*{v}\b", md):
            out["verdict"] = v
            break

    if m := re.search(r"\*\*Nota:\*\*\s*([\d,.]+)\s*/\s*10", md):
        out["score_total"] = float(m.group(1).replace(",", "."))
    if m := re.search(r"\*\*Confianza:\*\*\s*(Alta|Media|Baja)", md):
        out["confianza"] = m.group(1)

    pilares = {
        "score_moat": r"\|\s*Moat\s*\|\s*([\d,.]+)\s*/\s*10",
        "score_salud": r"\|\s*Salud financiera\s*\|\s*([\d,.]+)\s*/\s*10",
        "score_revaloriz": r"\|\s*Revalorizaci[óo]n[^|]*\|\s*([\d,.]+)\s*/\s*10",
        "score_margen": r"\|\s*Margen de seguridad\s*\|\s*([\d,.]+)\s*/\s*10",
    }
    for k, pat in pilares.items():
        if m := re.search(pat, md, re.IGNORECASE):
            out[k] = float(m.group(1).replace(",", "."))

    if m := re.search(r"intr[íi]nseco[^*]*\*\*([\d.,]+)\s*-\s*([\d.,]+)", md):
        out["valor_min"] = float(m.group(1).replace(",", "."))
        out["valor_max"] = float(m.group(2).replace(",", "."))
    if m := re.search(r"m[áa]ximo de entrada[^*]*\*\*([\d.,]+)", md):
        out["precio_entrada"] = float(m.group(1).replace(",", "."))

    return out


def estimate_cost(u: dict) -> float:
    """0.0 mientras estés dentro del nivel gratuito de Gemini — que es el caso
    salvo que hayas activado facturación en Google AI Studio. Se conserva la
    función (y el campo `cost_usd` en Supabase) para cuando quieras pasar a un
    modelo de pago sin tocar el resto del pipeline."""
    return 0.0


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #

@dataclass
class Outcome:
    ticker: str
    stage: str            # 'cache' | 'rechazo_quant' | 'analizado' | 'error'
    quant_score: float | None = None
    verdict: str | None = None
    report_md: str | None = None
    cost_usd: float = 0.0
    detail: str = ""


def analyze(ticker: str, repo: Repo, force: bool = False) -> Outcome:
    ticker = ticker.upper()
    try:
        f = build_fundamentals(ticker)
        company_id = repo.upsert_company(f)

        r = screen(f)
        metrics = r.metrics.to_dict()
        h = data_hash(metrics)

        snapshot_id = repo.save_snapshot(company_id, f, metrics, h)
        repo.save_screen(company_id, snapshot_id, r)

        if not r.passed:
            return Outcome(ticker, "rechazo_quant", r.score,
                           detail="; ".join(r.vetoes) or f"score {r.score} bajo el umbral")

        if not force:
            if hit := repo.cached_analysis(company_id, h):
                return Outcome(ticker, "cache", r.score, hit["verdict"], hit["report_md"])

        payload = build_llm_payload(f, r)
        resp = call_claude(payload)
        parsed = parse_report(resp["text"])
        cost = estimate_cost(resp)

        row = {
            "company_id": company_id,
            "snapshot_id": snapshot_id,
            "data_hash": h,
            "prompt_version": PROMPT_VERSION,
            "model": resp.get("model", MODEL),  # puede ser el de reserva si el principal falló
            "verdict": parsed.get("verdict", "NO_INVERTIBLE"),
            "price_at_analysis": f.price,
            "report_md": resp["text"],
            "report_json": parsed,
            "input_tokens": resp["input_tokens"],
            "output_tokens": resp["output_tokens"],
            "cached_tokens": resp["cached_tokens"],
            "cost_usd": cost,
            **{k: v for k, v in parsed.items() if k.startswith(("score_", "valor_", "precio_"))},
            "confianza": parsed.get("confianza"),
        }
        repo.save_analysis(row)
        return Outcome(ticker, "analizado", r.score, row["verdict"], resp["text"], cost)

    except Exception as e:  # noqa: BLE001
        log.exception("Fallo analizando %s", ticker)
        return Outcome(ticker, "error", detail=str(e))


def analyze_universe(tickers: list[str], repo: Repo | None = None) -> list[Outcome]:
    repo = repo or Repo()
    results = [analyze(t, repo) for t in tickers]
    coste = sum(o.cost_usd for o in results)
    pasan = sum(1 for o in results if o.stage in ("analizado", "cache"))
    log.info("%d/%d llegaron a la IA · coste %.4f USD", pasan, len(results), coste)
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    import sys

    for o in analyze_universe(sys.argv[1:] or ["MSFT", "KO", "F"]):
        print(f"{o.ticker:6} {o.stage:15} quant={o.quant_score} {o.verdict or ''} {o.detail}")
