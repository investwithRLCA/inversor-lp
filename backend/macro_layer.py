"""
Capa MACRO — evaluación estructural de sector (no de empresa).

Diferencia económica con `pipeline.py`: el análisis macro se cachea por
(sector, subsector), no por empresa. IAG, Lufthansa y Air France comparten el
mismo veredicto macro. Analizar 300 empresas ≈ 35 llamadas macro.

TTL largo (270 días) porque las dinámicas estructurales no cambian en trimestres.
Se invalida por tiempo o a mano cuando ocurre un hecho que fija fecha
(prohibición legislada, cruce de coste de una tecnología sustitutiva).

Uso:
    from macro_layer import MacroTarget, macro_for, combine

    t = MacroTarget(sector="Aerolíneas", subsector="red europea",
                    modelo="transporte de pasajeros en red radial con hub",
                    insumos=["queroseno", "slots", "tripulaciones", "aeronaves"],
                    geo="Europa 70%, Latam 20%, resto 10%")
    macro = macro_for(t, repo)
    final = combine(macro["score"], "COMPRA", moat=8)
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import unicodedata

from llm_gemini import GEMINI_MODEL, call_gemini

log = logging.getLogger(__name__)

MACRO_PROMPT_VERSION = "macro-v1.0"
MACRO_TTL_DAYS = 270
MODEL = GEMINI_MODEL

MACRO_SYSTEM = (Path(__file__).parent.parent / "prompts" / "macro_prompt_es.md").read_text(
    encoding="utf-8"
)


# --------------------------------------------------------------------------- #
# Entrada
# --------------------------------------------------------------------------- #

@dataclass
class MacroTarget:
    """Lo único que necesita la capa macro. Sin estados financieros: son irrelevantes
    para juzgar si un modelo de negocio tiene sentido dentro de 10 años."""

    sector: str
    subsector: str = ""
    modelo: str = ""                              # el negocio en una frase
    insumos: list[str] = field(default_factory=list)
    geo: str = ""                                 # exposición por geografía
    clientes: str = ""                            # quién paga: consumidor, empresa, Estado
    notas: str = ""                               # hechos con fecha, si los hay

    @property
    def cache_key(self) -> str:
        """Clave estable e insensible a acentos: 'Aerolíneas|red europea'
        → 'aerolineas|red-europea'. Evita duplicar el análisis por una tilde."""
        s = f"{self.sector}|{self.subsector}".strip("| ").lower()
        s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
        return re.sub(r"[^a-z0-9|]+", "-", s).strip("-")


def build_macro_payload(t: MacroTarget) -> str:
    """~90 tokens. La capa macro es barata; lo caro sería repetirla por empresa."""
    lines = [
        f'<SECTOR nombre="{t.sector}" subsector="{t.subsector or "n/d"}">',
        f"MODELO|{t.modelo or 'n/d'}",
        f"INSUMOS|{'|'.join(t.insumos) if t.insumos else 'n/d'}",
        f"GEO|{t.geo or 'n/d'}",
        f"CLIENTE|{t.clientes or 'n/d'}",
    ]
    if t.notas:
        lines.append(f"HECHOS|{t.notas}")
    lines += ["</SECTOR>", "", "Evalúa según tu mandato."]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Llamada y parseo
# --------------------------------------------------------------------------- #

def call_macro(payload: str) -> dict:
    return call_gemini(MACRO_SYSTEM, payload, max_tokens=1800, temperature=0.3)


def parse_macro(md: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if m := re.search(r"\*\*Puntuaci[óo]n Macro:\*\*\s*([\d,.]+)\s*/\s*10", md):
        out["score"] = float(m.group(1).replace(",", "."))
    if m := re.search(r"\*\*Confianza:\*\*\s*(Alta|Media|Baja)", md):
        out["confianza"] = m.group(1)
    if m := re.search(r"\*\*Tesis macro \(2 frases\):\*\*\s*(.+?)(?:\n-|\n#|$)", md, re.S):
        out["tesis"] = m.group(1).strip()
    if m := re.search(
        r"\*\*Reloj de disrupci[óo]n:\*\*\s*(.+?)(?:\n\n|\n#)", md, re.S
    ):
        out["reloj_disrupcion"] = m.group(1).strip()
    if m := re.search(r"\*\*Qu[ée] refutar[íi]a esta tesis:\*\*\s*(.+?)(?:\n-|\n#|$)", md, re.S):
        out["refutacion"] = m.group(1).strip()
    return out


# --------------------------------------------------------------------------- #
# Caché por sector
# --------------------------------------------------------------------------- #

def macro_for(t: MacroTarget, repo, force: bool = False) -> dict:
    """Devuelve el análisis macro del sector, reutilizando el guardado si sigue vigente."""
    key = t.cache_key
    if not force:
        hit = repo.cached_macro(key, MACRO_PROMPT_VERSION, MODEL)
        if hit:
            log.info("macro cache HIT %s", key)
            return hit

    resp = call_macro(build_macro_payload(t))
    parsed = parse_macro(resp["text"])
    row = {
        "cache_key": key,
        "sector": t.sector,
        "subsector": t.subsector or None,
        "prompt_version": MACRO_PROMPT_VERSION,
        "model": resp.get("model", MODEL),  # puede ser el de reserva si el principal falló
        "score": parsed.get("score"),
        "confianza": parsed.get("confianza"),
        "tesis": parsed.get("tesis"),
        "reloj_disrupcion": parsed.get("reloj_disrupcion"),
        "refutacion": parsed.get("refutacion"),
        "report_md": resp["text"],
        "report_json": parsed,
        "input_tokens": resp["input_tokens"],
        "output_tokens": resp["output_tokens"],
        "cached_tokens": resp["cached_tokens"],
        "valid_until": (
            datetime.now(timezone.utc) + timedelta(days=MACRO_TTL_DAYS)
        ).isoformat(),
    }
    return repo.save_macro(row)


# --------------------------------------------------------------------------- #
# Combinación macro × fundamental
# --------------------------------------------------------------------------- #

ESCALA = ["DESCARTE", "NO_INVERTIBLE", "VIGILAR", "COMPRA", "COMPRA_FUERTE"]


def combine(
    macro_score: float | None,
    fundamental_verdict: str,
    moat: float | None = None,
    excepcion_documentada: bool = False,
) -> dict[str, Any]:
    """El macro acota el veredicto fundamental, no se le suma.

    A 10 años el sector explica más dispersión de retorno que la calidad relativa
    de una empresa dentro de él: una empresa excelente en un sector en contracción
    acaba siendo mediocre.
    """
    if macro_score is None:
        return {"verdict": fundamental_verdict, "ajuste": "sin macro disponible"}

    try:
        i = ESCALA.index(fundamental_verdict)
    except ValueError:
        i = 0

    if macro_score <= 3:
        if excepcion_documentada:
            techo = ESCALA.index("VIGILAR")
            nota = "sector 1-3 pero excepción documentada: techo VIGILAR"
        else:
            techo, nota = 0, f"sector estructuralmente en contracción (macro {macro_score})"
        j = min(i, techo)
    elif macro_score <= 5:
        j = min(i, ESCALA.index("VIGILAR"))
        nota = f"buen negocio en sector sin viento a favor (macro {macro_score}): techo VIGILAR"
    elif macro_score <= 7:
        j, nota = i, "sin ajuste macro"
    else:
        if moat is not None and moat >= 8 and i < len(ESCALA) - 1:
            j = i + 1
            nota = f"macro {macro_score} + moat {moat}: sube un escalón"
        else:
            j, nota = i, f"macro {macro_score} favorable, pero moat insuficiente para subir"

    return {
        "verdict": ESCALA[j],
        "verdict_fundamental": fundamental_verdict,
        "macro_score": macro_score,
        "ajuste": nota,
        "modificado": j != i,
    }


# --------------------------------------------------------------------------- #
# Taxonomía mínima: mapea sector de Finnhub → objetivo macro
# --------------------------------------------------------------------------- #

TAXONOMIA: dict[str, dict[str, Any]] = {
    "Airlines": {
        "sector": "Aerolíneas",
        "insumos": ["queroseno", "slots aeroportuarios", "tripulaciones", "aeronaves"],
        "clientes": "consumidor y empresa",
    },
    "Semiconductors": {
        "sector": "Semiconductores",
        "insumos": ["silicio ultrapuro", "agua ultrapura", "litografía EUV", "energía", "talento"],
        "clientes": "fabricantes de electrónica e hiperescalares",
    },
    "Utilities": {
        "sector": "Utilities eléctricas",
        "insumos": ["capital", "capacidad de red", "permisos", "cobre"],
        "clientes": "consumidor regulado e industria",
    },
    "Oil & Gas": {
        "sector": "Petróleo y gas",
        "insumos": ["reservas", "capital", "licencia social"],
        "clientes": "industria y transporte",
    },
    "Pharmaceuticals": {
        "sector": "Farmacéutica de patente",
        "insumos": ["I+D", "propiedad intelectual", "aprobación regulatoria"],
        "clientes": "sistemas públicos de salud y aseguradoras",
    },
    "Retail": {
        "sector": "Retail",
        "insumos": ["suelo comercial", "logística", "mano de obra"],
        "clientes": "consumidor",
    },
}


def target_from_company(sector_finnhub: str | None, name: str, modelo: str = "") -> MacroTarget:
    base = TAXONOMIA.get(sector_finnhub or "", {"sector": sector_finnhub or "Desconocido"})
    return MacroTarget(
        sector=base["sector"],
        modelo=modelo or f"modelo de negocio de {name}",
        insumos=base.get("insumos", []),
        clientes=base.get("clientes", ""),
    )


if __name__ == "__main__":
    # Demostración de la regla de combinación, sin llamadas a la API.
    casos = [
        (3, "COMPRA", 9, False),
        (3, "COMPRA", 9, True),
        (5, "COMPRA_FUERTE", 8, False),
        (7, "COMPRA", 7, False),
        (9, "COMPRA", 9, False),
        (9, "COMPRA", 6, False),
    ]
    for ms, fv, moat, exc in casos:
        r = combine(ms, fv, moat, exc)
        print(f"macro={ms} fund={fv:14} moat={moat} → {r['verdict']:14} ({r['ajuste']})")
