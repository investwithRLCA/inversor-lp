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
    python run_universo.py --estado                # solo muestra cómo va el barrido

Prioridad (nov-2026): primero las empresas que están en algún índice (S&P 500,
S&P 400, S&P 600, Russell 2000, Nasdaq-100; listas de docs/mercados.json), que
son las ~3.000 empresas reales con negocio. Del resto del listado de la SEC
(fondos, SPACs, extranjeras sin cuentas en EE. UU....) casi nada pasa el
cribado. Cada empresa de índice se vuelve a repasar cada RONDA_INDICES_DIAS.

Cola de IA: las que pasan el cribado pero no caben en el cupo de IA de esta
ejecución quedan marcadas como pendientes y la siguiente ejecución las coge
primero (antes se perdían hasta la siguiente vuelta al universo entero). Si la
IA se queda sin cupo (HTTP 429), se para y lo pendiente sigue en cola.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

# Mismo texto que deja pipeline.analyze() cuando una empresa pasa el cribado
# pero aún no se ha mandado a la IA: así se reconoce la cola pendiente.
PENDIENTE_IA = "pasa el cribado, aún sin analizar con IA"
RUTA_MERCADOS = Path(__file__).resolve().parent.parent / "docs" / "mercados.json"
INDICES_PRIORITARIOS = ("sp500", "sp400", "sp600", "r2000", "ndx")
RONDA_INDICES_DIAS = 45          # cada cuánto se vuelve a repasar una empresa de índice
TROZO_CONSULTA = 150             # tickers por consulta a Supabase (la URL no puede ser enorme)
FALLOS_CUPO_PARA_PARAR = 2       # 429 seguidos → la IA está sin cupo hoy: no insistir


def tickers_prioritarios(ruta: Path = RUTA_MERCADOS) -> list[str]:
    """Empresas de los índices (docs/mercados.json). Lista vacía si no hay fichero."""
    try:
        js = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: set[str] = set()
    for cod in INDICES_PRIORITARIOS:
        out.update((js.get("mercados", {}).get(cod) or {}).get("tickers") or [])
    return sorted(out)


def _fecha(x) -> datetime | None:
    if not x:
        return None
    try:
        return datetime.fromisoformat(str(x).replace("Z", "+00:00"))
    except ValueError:
        return None


def elegir_lote(prioritarias: list[dict], resto: list[dict], tamaño: int,
                ahora: datetime, ronda_dias: int = RONDA_INDICES_DIAS) -> list[dict]:
    """Primero las de índice nunca vistas o vistas hace más de `ronda_dias`
    (las más antiguas antes); el hueco que quede, con el orden de siempre."""
    limite = ahora - timedelta(days=ronda_dias)
    toca = [f for f in prioritarias if (_fecha(f.get("ultimo_intento")) or datetime.min.replace(tzinfo=timezone.utc)) < limite]
    toca.sort(key=lambda f: _fecha(f.get("ultimo_intento")) or datetime.min.replace(tzinfo=timezone.utc))
    lote = toca[:tamaño]
    vistos = {f["ticker"] for f in lote}
    for f in resto:
        if len(lote) >= tamaño:
            break
        if f["ticker"] not in vistos:
            vistos.add(f["ticker"])
            lote.append(f)
    return lote


def es_falta_de_cupo(detalle: str) -> bool:
    d = (detalle or "").lower()
    return "429" in d or "límite de peticiones" in d or "resource_exhausted" in d


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


def siguiente_lote(repo: Repo, tamaño: int, ronda_dias: int = RONDA_INDICES_DIAS) -> list[dict]:
    campos = "ticker, cik, nombre, veces_intentado, ultimo_intento"
    prioridad = tickers_prioritarios()
    prioritarias: list[dict] = []
    for i in range(0, len(prioridad), TROZO_CONSULTA):
        trozo = prioridad[i : i + TROZO_CONSULTA]
        res = repo.db.table("universe_progreso").select(campos).in_("ticker", trozo).execute()
        prioritarias.extend(res.data or [])
    res = (
        repo.db.table("universe_progreso")
        .select(campos)
        .order("ultimo_intento", desc=False, nullsfirst=True)
        .limit(tamaño)
        .execute()
    )
    lote = elegir_lote(prioritarias, res.data or [], tamaño, datetime.now(timezone.utc), ronda_dias)
    n_idx = sum(1 for f in lote if f["ticker"] in set(prioridad))
    print(f"Lote: {len(lote)} tickers ({n_idx} de índices; {len(prioridad)} empresas de índice en la lista).")
    return lote


def lote_de_errores(repo: Repo, patron: str, tamaño: int) -> list[dict]:
    """Empresas que fallaron con un error de datos que contiene `patron` (p. ej. tras
    arreglar cómo se leen esos datos): se vuelven a intentar ya, sin esperar a la ronda."""
    campos = "ticker, cik, nombre, veces_intentado, ultimo_intento"
    todas: list[dict] = []
    for desde in range(0, 20_000, 1000):                           # todas las que tienen ese error…
        trozo = (
            repo.db.table("universe_progreso")
            .select(campos)
            .eq("ultimo_screening_ok", False)
            .ilike("ultimo_error", f"%{patron}%")
            .order("ultimo_intento", desc=False)
            .range(desde, desde + 999)
            .execute()
        ).data or []
        todas += trozo
        if len(trozo) < 1000:
            break
    prioridad = set(tickers_prioritarios())
    todas.sort(key=lambda f: f["ticker"] not in prioridad)         # …primero las de índices (orden estable)
    lote = todas[:tamaño]
    print(f"Lote de reintento («{patron}»): {len(todas)} con ese error; se reintentan {len(lote)} "
          f"({sum(1 for f in lote if f['ticker'] in prioridad)} de índices).")
    return lote


def cola_pendiente_ia(repo: Repo, n: int) -> list[dict]:
    """Empresas que pasaron el cribado en ejecuciones anteriores y siguen sin IA."""
    if n <= 0:
        return []
    res = (
        repo.db.table("universe_progreso")
        .select("ticker, veces_intentado")
        .eq("ultimo_error", PENDIENTE_IA)
        .order("ultimo_intento", desc=False)
        .limit(n)
        .execute()
    )
    return res.data or []


def _contar(q) -> int:
    return q.limit(1).execute().count or 0


def estado(repo: Repo) -> None:
    """Cómo va el barrido (solo lectura)."""
    T = lambda: repo.db.table("universe_progreso").select("ticker", count="exact")
    total = _contar(T())
    nunca = _contar(T().is_("ultimo_intento", "null"))
    pend = _contar(T().eq("ultimo_error", PENDIENTE_IA))
    err = _contar(T().eq("ultimo_screening_ok", False))
    print(f"Universo: {total} tickers · nunca vistos {nunca} · vistos {total - nunca} · "
          f"con error de datos {err} · pasan el cribado y esperan IA {pend}")
    prioridad = tickers_prioritarios()
    vistos = no_estan = 0
    for i in range(0, len(prioridad), TROZO_CONSULTA):
        trozo = prioridad[i : i + TROZO_CONSULTA]
        filas = repo.db.table("universe_progreso").select("ticker, ultimo_intento").in_("ticker", trozo).execute().data or []
        vistos += sum(1 for f in filas if f.get("ultimo_intento"))
        no_estan += len(trozo) - len(filas)
    print(f"Empresas de índices: {len(prioridad)} · ya vistas {vistos} · sin ver {len(prioridad) - vistos - no_estan}"
          f" · no están en el listado de la SEC {no_estan}")
    filas = repo.db.table("v_ultimo_analisis").select("verdict").execute().data or []
    cuenta: dict[str, int] = {}
    for f in filas:
        cuenta[f.get("verdict") or "?"] = cuenta.get(f.get("verdict") or "?", 0) + 1
    print(f"Empresas con análisis de IA: {len(filas)} · " + " · ".join(f"{k} {v}" for k, v in sorted(cuenta.items())))
    pasan = _contar(repo.db.table("quant_screens").select("id", count="exact").eq("passed", True))
    print(f"Cribados aprobados guardados (todas las versiones y fechas): {pasan}")


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
    ap.add_argument("--estado", action="store_true", help="solo muestra cómo va el barrido")
    ap.add_argument("--ronda-dias", type=int, default=RONDA_INDICES_DIAS,
                     help="repasar las empresas de índice vistas hace más de N días (0 = todas otra vez)")
    ap.add_argument("--reintentar", default="",
                     help="en vez del lote normal, reintentar las que fallaron con un error que contenga este texto")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not os.getenv("SEC_USER_AGENT"):
        sys.exit("Falta SEC_USER_AGENT.")

    repo = Repo()

    if args.sembrar:
        n = sembrar(repo)
        print(f"Universo sembrado: {n} tickers en total.")
        return
    if args.estado:
        estado(repo)
        return

    if args.reintentar:
        lote = lote_de_errores(repo, args.reintentar, args.lote)
        if not lote:
            print("No hay empresas con ese error: nada que reintentar.")
            return
    else:
        lote = siguiente_lote(repo, args.lote, args.ronda_dias)
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

    # --- Fase 2: IA, secuencial: primero la cola de ejecuciones anteriores,
    #     luego las de este lote (mejor puntuación del cribado primero) ------ #
    pendientes = sorted((t for t, o in outcomes.items() if o.stage == "pendiente_ia"),
                        key=lambda t: -(outcomes[t].quant_score or 0))
    cola = [] if args.solo_cribado else [f for f in cola_pendiente_ia(repo, args.max_ia) if f["ticker"] not in outcomes]
    for f in cola:
        veces_previas[f["ticker"]] = f.get("veces_intentado") or 0
    a_analizar = [] if args.solo_cribado else ([f["ticker"] for f in cola] + pendientes)[: args.max_ia]
    print(f"\nFase 2/2 — {len(a_analizar)} pasan a la IA "
          f"({len(cola)} de la cola pendiente + {len(pendientes)} de este lote que superaron el cribado)...")

    ia_usadas = 0
    fallos_cupo = 0
    for ticker in a_analizar:
        o = analyze(ticker, repo, con_ia=True)
        if o.stage == "error" and es_falta_de_cupo(o.detail):
            fallos_cupo += 1
            outcomes[ticker] = Outcome(ticker, "pendiente_ia", o.quant_score, detail=PENDIENTE_IA)
            print(f"  {ticker:8} sin cupo de IA: queda en cola")
            if fallos_cupo >= FALLOS_CUPO_PARA_PARAR:
                print("  La IA no tiene cupo ahora mismo: se para aquí; lo pendiente sigue en cola.")
                break
            continue
        fallos_cupo = 0
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
