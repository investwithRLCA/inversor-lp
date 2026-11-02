"""
Refresco PRIORITARIO de la cartera ya analizada — distinto del barrido de
descubrimiento (run_universo.py).

Por qué hace falta un script aparte: `run_universo.py` recorre las ~10.000
empresas del universo EDGAR por turnos, una vez cada una antes de repetir
ninguna. Eso es correcto para DESCUBRIR candidatas nuevas, pero significa
que a una empresa que YA analizaste (sobre todo si es COMPRA_FUERTE,
COMPRA o VIGILAR — las que de verdad te importa tener al día) le puede
tocar de nuevo dentro de semanas, según cuántas haya por delante en la
cola de descubrimiento. Este script es una cola aparte, mucho más corta:
solo las empresas que ya tienen al menos un análisis guardado, ordenadas
por interés (COMPRA_FUERTE primero, luego COMPRA, luego VIGILAR...) y,
dentro de cada grupo, por antigüedad (la que lleva más tiempo sin
revisarse, primero).

Cada una vuelve a pasar por el embudo completo con datos frescos de EDGAR
y Finnhub. Casi siempre será gratis: si los fundamentales no han cambiado
desde el último 10-K/10-Q, `data_hash` sale idéntico y `pipeline.analyze()`
reutiliza el análisis en caché — solo se paga una llamada nueva a la IA
cuando de verdad ha salido información nueva. Esto es, además, lo que te
avisa si una empresa que antes pasaba el cribado cuantitativo ha dejado
de pasarlo (p. ej. se ha endeudado, o el margen se ha deteriorado): algo
que el barrido de descubrimiento, por sí solo, no te señalaría hasta que
le volviera a tocar turno.

    python refrescar.py                  # las 30 más urgentes (por defecto)
    python refrescar.py --limite 50

Pensado para ejecutarse a diario (o más a menudo) desde GitHub Actions,
con su propio workflow (refresco.yml), totalmente independiente del
barrido de descubrimiento.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from pipeline import Repo, analyze
from llm_gemini import GEMINI_KEYS

log = logging.getLogger(__name__)

PAUSA_ENTRE_IA_SEG = 4 if len(GEMINI_KEYS) <= 1 else 2

# Cuanto más interesante el veredicto, antes se refresca. Dentro del mismo
# veredicto, gana la que lleva más tiempo sin revisarse.
PRIORIDAD_VEREDICTO = {
    "COMPRA_FUERTE": 0,
    "COMPRA": 1,
    "VIGILAR": 2,
    "NO_INVERTIBLE": 3,
    "DESCARTE": 4,
}


def candidatas_a_refrescar(repo: Repo, limite: int) -> list[dict]:
    """Todas las empresas con al menos un análisis guardado — reutiliza la
    vista `v_ultimo_analisis` ya definida en schema.sql, sin necesitar
    ninguna migración nueva — ordenadas por interés y antigüedad."""
    res = (
        repo.db.table("v_ultimo_analisis")
        .select("ticker, verdict, analizado_el")
        .execute()
    )
    filas = res.data or []
    filas.sort(key=lambda f: (PRIORIDAD_VEREDICTO.get(f["verdict"], 9), f["analizado_el"] or ""))
    return filas[:limite]


def main() -> None:
    ap = argparse.ArgumentParser(description="Refresco prioritario de empresas ya analizadas.")
    ap.add_argument("--limite", type=int, default=30,
                     help="cuántas empresas, como máximo, se revisan en esta ejecución")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not os.getenv("SEC_USER_AGENT"):
        sys.exit("Falta SEC_USER_AGENT.")

    repo = Repo()
    candidatas = candidatas_a_refrescar(repo, args.limite)
    if not candidatas:
        print("Todavía no hay ninguna empresa con análisis guardado — nada que refrescar.")
        return

    print(f"Refrescando {len(candidatas)} empresa(s) ya analizadas, por orden de prioridad "
          f"({', '.join(sorted(set(c['verdict'] for c in candidatas)))})...")
    resultados = {"sin_cambios": 0, "reanalizada": 0, "ya_no_pasa_cribado": 0, "error": 0}

    for fila in candidatas:
        ticker = fila["ticker"]
        try:
            # force=False a propósito: si los fundamentales no han cambiado,
            # esto es gratis (reutiliza la caché). Solo cuesta cuando de
            # verdad hay datos nuevos que justifican gastar una llamada.
            o = analyze(ticker, repo, con_ia=True)

            if o.stage == "cache":
                resultados["sin_cambios"] += 1
                print(f"  {ticker:8} sin cambios desde el último análisis · {o.verdict}")
            elif o.stage == "analizado":
                resultados["reanalizada"] += 1
                print(f"  {ticker:8} ¡DATOS NUEVOS! reanalizada · {o.verdict}")
                time.sleep(PAUSA_ENTRE_IA_SEG)
            elif o.stage == "rechazo_quant":
                resultados["ya_no_pasa_cribado"] += 1
                print(f"  {ticker:8} YA NO pasa el cribado cuantitativo (score {o.quant_score}) "
                      f"— antes era {fila['verdict']}, revisa si sigue teniendo sentido vigilarla")
            else:
                print(f"  {ticker:8} {o.stage}: {o.detail}")

        except Exception as e:  # noqa: BLE001
            resultados["error"] += 1
            log.exception("Fallo refrescando %s", ticker)
            print(f"  {ticker:8} ERROR: {e}")

    print(f"\nResumen del refresco: {resultados}")


if __name__ == "__main__":
    main()
