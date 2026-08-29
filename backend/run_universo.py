"""
Recorre el universo completo de tickers por el embudo, en lotes, guardando
progreso en Supabase — así una ejecución de GitHub Actions puede parar y la
siguiente continúa exactamente donde lo dejó la anterior, sin repetir
trabajo ni tener que caber todo el universo en una sola ejecución.

Dos fases en cada lote:
  1. CRIBADO (gratis): para todos los tickers del lote — descarga datos,
     filtra, guarda en Supabase. No llama a la IA.
  2. IA (limitada): de los que pasaron el cribado y no tienen ya un análisis
     en caché, solo se manda a la IA un número reducido (--max-ia), con
     pausa entre llamadas para no saturar el nivel gratuito de Gemini.

    python run_universo.py --sembrar              # una sola vez: carga el universo completo
    python run_universo.py                        # procesa el siguiente lote (por defecto 40)
    python run_universo.py --lote 100 --max-ia 8
    python run_universo.py --solo-cribado          # ningún ticker de este lote llega a la IA
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone

from pipeline import Repo, analyze
from universe import universo_completo
from llm_gemini import GEMINI_KEYS

log = logging.getLogger(__name__)

# Con una sola clave, mejor ir despacio. Con varias, las llamadas se
# reparten entre proyectos distintos (ver llm_gemini.py), así que cada
# clave individual recibe muchas menos peticiones por minuto — se puede
# acortar la pausa sin acercarse más a ningún límite por clave.
PAUSA_ENTRE_IA_SEG = 5 if len(GEMINI_KEYS) <= 1 else 3


def sembrar(repo: Repo) -> int:
    """Carga el universo completo de EDGAR en universe_progreso, una sola vez.
    Los tickers que ya estaban no se tocan (upsert que no pisa el progreso)."""
    tickers = universo_completo()
    filas = [
        {"ticker": t.ticker, "cik": t.cik, "nombre": t.nombre}
        for t in tickers
    ]
    # Insertar en lotes grandes; ignora los que ya existan (no reinicia su progreso).
    TAM = 500
    insertados = 0
    for i in range(0, len(filas), TAM):
        lote = filas[i : i + TAM]
        repo.db.table("universe_progreso").upsert(
            lote, on_conflict="ticker", ignore_duplicates=True
        ).execute()
        insertados += len(lote)
        print(f"  sembrados {insertados}/{len(filas)}...")
    return len(filas)


def siguiente_lote(repo: Repo, tamaño: int) -> list[dict]:
    res = (
        repo.db.table("universe_progreso")
        .select("ticker, cik, nombre, veces_intentado")
        .order("ultimo_intento", desc=False, nullsfirst=True)
        .limit(tamaño)
        .execute()
    )
    return res.data or []


def marcar_progreso(
    repo: Repo, ticker: str, veces_previas: int, ok: bool, error: str = ""
) -> None:
    repo.db.table("universe_progreso").update({
        "ultimo_intento": datetime.now(timezone.utc).isoformat(),
        "ultimo_screening_ok": ok,
        "ultimo_error": (error[:500] if error else None),
        "veces_intentado": veces_previas + 1,
    }).eq("ticker", ticker).execute()


def main() -> None:
    ap = argparse.ArgumentParser(description="Barrido del universo completo, por lotes.")
    ap.add_argument("--sembrar", action="store_true",
                     help="carga el universo completo en universe_progreso (una vez)")
    ap.add_argument("--lote", type=int, default=40, help="tickers a procesar en esta ejecución")
    ap.add_argument("--max-ia", type=int, default=6,
                     help="cuántos, como máximo, llegan a la IA en esta ejecución")
    ap.add_argument("--solo-cribado", action="store_true", help="ningún ticker llega a la IA")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not os.getenv("SEC_USER_AGENT"):
        sys.exit("Falta SEC_USER_AGENT.")

    repo = Repo()

    if args.sembrar:
        n = sembrar(repo)
        print(f"Universo sembrado: {n} tickers en total.")
        return

    lote = siguiente_lote(repo, args.lote)
    if not lote:
        print("universe_progreso está vacío. Ejecuta primero: python run_universo.py --sembrar")
        return

    print(f"Procesando {len(lote)} tickers de este lote...")
    ia_usadas = 0
    resultados = {"cribado_ok": 0, "cribado_fuera": 0, "pasan_a_ia": 0, "analizados_ia": 0, "error": 0}

    for fila in lote:
        ticker = fila["ticker"]
        veces_previas = fila.get("veces_intentado") or 0
        con_ia = (not args.solo_cribado) and ia_usadas < args.max_ia
        try:
            o = analyze(ticker, repo, con_ia=con_ia)
            ok = o.stage != "error"

            if o.stage == "error":
                resultados["error"] += 1
                print(f"  {ticker:8} ERROR: {o.detail}")
            elif o.stage == "rechazo_quant":
                resultados["cribado_fuera"] += 1
                print(f"  {ticker:8} descarta (score {o.quant_score})")
            elif o.stage == "pendiente_ia":
                resultados["cribado_ok"] += 1
                resultados["pasan_a_ia"] += 1
                print(f"  {ticker:8} PASA cribado (score {o.quant_score}) — a la espera de IA")
            elif o.stage in ("analizado", "cache"):
                resultados["cribado_ok"] += 1
                resultados["analizados_ia"] += 1
                if o.stage == "analizado":
                    ia_usadas += 1
                    time.sleep(PAUSA_ENTRE_IA_SEG)
                print(f"  {ticker:8} {o.stage:10} {o.verdict}")

            marcar_progreso(repo, ticker, veces_previas, ok, o.detail)

        except Exception as e:  # noqa: BLE001
            resultados["error"] += 1
            log.exception("Fallo inesperado con %s", ticker)
            marcar_progreso(repo, ticker, veces_previas, False, str(e))

    print(f"\nResumen del lote: {resultados}")
    print(f"Llamadas a la IA en esta ejecución: {ia_usadas}/{args.max_ia}")


if __name__ == "__main__":
    main()
