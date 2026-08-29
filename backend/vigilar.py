"""
La parte de "la app vigila sola". No vuelve a analizar nada con IA — solo
refresca el PRECIO (barato, vía Finnhub) de las empresas marcadas VIGILAR,
comprueba si ya han caído a su precio de entrada, y guarda ese precio de
vuelta en Supabase — así el dashboard móvil puede mostrarlo sin tener que
llamar a Finnhub por su cuenta ni exponer esa clave en el navegador.

    python vigilar.py                 # comprueba todas las VIGILAR vigentes
    python vigilar.py --salida alertas.json

Pensado para ejecutarse a diario desde GitHub Actions. Si `--salida` apunta
a un fichero, se escribe ahí un JSON con las que han cruzado el umbral, para
que el propio workflow decida qué hacer con eso (p. ej. abrir un Issue).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

from pipeline import Repo
from providers import FinnhubProvider

log = logging.getLogger(__name__)


def vigilar_vencidas(repo: Repo) -> list[dict]:
    """Trae, de Supabase, el último análisis de cada empresa marcada VIGILAR
    que tiene un precio de entrada calculado. Es la vista `v_tesis_completa`
    ya definida en el esquema — un solo viaje a la base de datos."""
    res = (
        repo.db.table("v_tesis_completa")
        .select("analysis_id, ticker, name, veredicto_final, precio_entrada, price_at_analysis, analizado_el")
        .eq("veredicto_final", "VIGILAR")
        .not_.is_("precio_entrada", "null")
        .execute()
    )
    return res.data or []


def guardar_precio(repo: Repo, analysis_id: str, precio: float, cruzada: bool) -> None:
    """Escribe el precio recién comprobado en la fila de ai_analyses, para
    todas las vigiladas (no solo las que han cruzado) — así el dashboard
    siempre puede mostrar un precio actual razonablemente fresco."""
    repo.db.table("ai_analyses").update({
        "ultimo_precio": precio,
        "ultimo_precio_en": datetime.now(timezone.utc).isoformat(),
        "en_zona_compra": cruzada,
    }).eq("id", analysis_id).execute()


def main() -> None:
    ap = argparse.ArgumentParser(description="Vigilancia diaria de precio (sin IA).")
    ap.add_argument("--salida", help="fichero JSON donde escribir las que han cruzado el umbral")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not os.getenv("FINNHUB_API_KEY"):
        sys.exit("Falta FINNHUB_API_KEY: la vigilancia de precio la necesita sí o sí.")

    repo = Repo()
    finnhub = FinnhubProvider()

    candidatas = vigilar_vencidas(repo)
    print(f"Vigilando {len(candidatas)} empresa(s) marcadas VIGILAR con precio de entrada...")

    alertas = []
    for c in candidatas:
        ticker = c["ticker"]
        try:
            precio_actual = finnhub.quote(ticker)
        except Exception as e:  # noqa: BLE001
            log.warning("No se pudo obtener precio de %s: %s", ticker, e)
            continue

        if precio_actual is None:
            continue

        entrada = float(c["precio_entrada"])
        cruzada = precio_actual <= entrada
        marca = "✅ EN ZONA DE COMPRA" if cruzada else "  todavía por encima"
        print(f"  {ticker:8} precio actual {precio_actual:>9.2f} · entrada {entrada:>9.2f}  {marca}")

        guardar_precio(repo, c["analysis_id"], precio_actual, cruzada)

        if cruzada:
            alertas.append({
                "ticker": ticker,
                "nombre": c.get("name"),
                "precio_actual": precio_actual,
                "precio_entrada": entrada,
                "analizado_el": c.get("analizado_el"),
                "comprobado_el": datetime.now(timezone.utc).isoformat(),
            })

    print(f"\n{len(alertas)} empresa(s) han cruzado su precio de entrada.")

    if args.salida:
        with open(args.salida, "w", encoding="utf-8") as fh:
            json.dump(alertas, fh, ensure_ascii=False, indent=2)
        print(f"Escrito en {args.salida}")
    else:
        for a in alertas:
            print(f"  - {a['ticker']}: {a['precio_actual']} <= {a['precio_entrada']}")


if __name__ == "__main__":
    main()
