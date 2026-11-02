"""
Recorre el universo completo de tickers por el embudo, en lotes, guardando
progreso en Supabase — así una ejecución de GitHub Actions puede parar y la
siguiente continúa exactamente donde lo dejó la anterior, sin repetir
trabajo ni tener que caber todo el universo en una sola ejecución.

Este es el barrido de DESCUBRIMIENTO: recorre las ~10.000 empresas del
universo EDGAR por turnos, una vez cada una antes de repetir ninguna. Para
mantener al día las que YA tienen un análisis guardado (sobre todo las que
más te interesan: COMPRA_FUERTE, COMPRA, VIGILAR) hay una cola aparte,
mucho más corta y frecuente — ver `refrescar.py`. Las dos son
independientes: esta no "sabe" de aquella, y viceversa.

Dos fases en cada lote:
  1. CRIBADO (gratis, EN PARALELO): para todos los tickers del lote —
     descarga datos, filtra, guarda en Supabase. No llama a la IA. Se hace
     con varios hilos a la vez (PARALELISMO_CRIBADO) porque el cuello de
     botella real es la red (EDGAR + Finnhub), no la CPU.
  2. IA (limitada, secuencial): de los que pasaron el cribado y no tienen
     ya un análisis en caché, solo se manda a la IA un número reducido
     (--max-ia), con pausa entre llamadas para no saturar el nivel
     gratuito de Gemini. Esta fase sí es secuencial a propósito: el límite
     de Gemini es por proyecto de Google Cloud, no por lote de trabajo.

    python run_universo.py --sembrar              # una sola vez: carga el universo completo
    python run_universo.py                        # procesa el siguiente lote (por defecto 100, hasta 20 con IA)
    python run_universo.py --lote 150 --max-ia 30
    python run_universo.py --solo-cribado          # ningún ticker de este lote llega a la IA
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from pipeline import Outcome, Repo, analyze
from universe import universo_completo
from llm_gemini import GEMINI_KEYS

log = logging.getLogger(__name__)

# Con una sola clave, mejor ir despacio. Con varias, las llamadas se
# reparten entre proyectos distintos (ver llm_gemini.py), así que cada
# clave individual recibe muchas menos peticiones por minuto — se puede
# acortar la pausa sin acercarse más a ningún límite por clave.
#
# OJO: esto solo multiplica el cupo real si cada clave viene de un
# proyecto de Google Cloud DISTINTO. Varias claves del MISMO proyecto
# comparten el mismo límite y no aceleran nada — ver el aviso en
# llm_gemini.py.
PAUSA_ENTRE_IA_SEG = 5 if len(GEMINI_KEYS) <= 1 else 3

# Cuántos tickers se criban EN PARALELO. El cribado es ~100% espera de red
# (EDGAR + Finnhub), así que varios hilos a la vez aceleran mucho sin más
# coste — providers.py ya reparte el límite de peticiones/segundo de la SEC
# entre estos hilos (ver el throttle de SecEdgarProvider) y serializa
# Finnhub con un hueco mínimo global, así que subir este número no dispara
# ningún límite por sí solo. 3 es un punto razonable; no hace falta tocarlo.
PARALELISMO_CRIBADO = int(os.getenv("CRIBADO_WORKERS", "3"))


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


def _cribar(ticker: str, repo: Repo) -> tuple[str, Outcome]:
    """Fase 1 de un ticker: datos + cribado cuantitativo, sin IA. Pensada
    para lanzarse en un hilo — no hace ninguna llamada a Gemini, así que
    varios hilos a la vez no compiten por el mismo cupo."""
    return ticker, analyze(ticker, repo, con_ia=False)


def main() -> None:
    ap = argparse.ArgumentParser(description="Barrido del universo completo, por lotes.")
    ap.add_argument("--sembrar", action="store_true",
                     help="carga el universo completo en universe_progreso (una vez)")
    ap.add_argument("--lote", type=int, default=100, help="tickers a procesar en esta ejecución")
    ap.add_argument("--max-ia", type=int, default=20,
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

    veces_previas = {f["ticker"]: (f.get("veces_intentado") or 0) for f in lote}

    # --- Fase 1: cribado en paralelo (gratis) -------------------------- #
    print(f"Fase 1/2 — cribando {len(lote)} tickers "
          f"({PARALELISMO_CRIBADO} en paralelo)...")
    outcomes: dict[str, Outcome] = {}
    with ThreadPoolExecutor(max_workers=PARALELISMO_CRIBADO) as pool:
        futuros = {pool.submit(_cribar, f["ticker"], repo): f["ticker"] for f in lote}
        for fut in as_completed(futuros):
            ticker = futuros[fut]
            try:
                _, o = fut.result()
            except Exception as e:  # noqa: BLE001
                log.exception("Fallo inesperado criando %s", ticker)
                o = Outcome(ticker, "error", detail=str(e))
            outcomes[ticker] = o
            if o.stage == "error":
                print(f"  {ticker:8} ERROR: {o.detail}")
            elif o.stage == "rechazo_quant":
                print(f"  {ticker:8} descarta (score {o.quant_score})")
            elif o.stage == "cache":
                print(f"  {ticker:8} cache       {o.verdict}")
            # "pendiente_ia" se anuncia en la fase 2.

    # --- Fase 2: IA, secuencial, para quien pasó el cribado ------------ #
    pendientes = [t for t, o in outcomes.items() if o.stage == "pendiente_ia"]
    a_analizar = [] if args.solo_cribado else pendientes[: args.max_ia]
    print(f"\nFase 2/2 — {len(a_analizar)} pasan a la IA "
          f"(de {len(pendientes)} candidata(s) que superaron el cribado)...")

    ia_usadas = 0
    for ticker in a_analizar:
        o = analyze(ticker, repo, con_ia=True)
        outcomes[ticker] = o
        if o.stage == "analizado":
            ia_usadas += 1
            time.sleep(PAUSA_ENTRE_IA_SEG)
        print(f"  {ticker:8} {o.stage:10} {o.verdict}")

    # --- Progreso + resumen --------------------------------------------- #
    resultados = {"cribado_ok": 0, "cribado_fuera": 0, "pasan_a_ia": 0, "analizados_ia": 0, "error": 0}
    for ticker, o in outcomes.items():
        ok = o.stage != "error"
        marcar_progreso(repo, ticker, veces_previas.get(ticker, 0), ok, o.detail)

        if o.stage == "error":
            resultados["error"] += 1
        elif o.stage == "rechazo_quant":
            resultados["cribado_fuera"] += 1
        elif o.stage == "pendiente_ia":
            resultados["cribado_ok"] += 1
            resultados["pasan_a_ia"] += 1
        elif o.stage in ("analizado", "cache"):
            resultados["cribado_ok"] += 1
            resultados["analizados_ia"] += 1

    print(f"\nResumen del lote: {resultados}")
    print(f"Llamadas a la IA en esta ejecución: {ia_usadas}/{args.max_ia}")


if __name__ == "__main__":
    main()
