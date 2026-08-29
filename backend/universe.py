"""
Capa 0 — UNIVERSO.

Hasta ahora escribíamos los tickers a mano: `probar_ia.py MSFT KO F`. Este
módulo responde a "cómo amplío más allá del S&P 500": la SEC publica un
listado con TODAS las empresas que presentan cuentas — unas 10.000 tickers,
no 500. Es el mismo fichero que `providers.SecEdgarProvider` ya usa para
resolver un ticker suelto; aquí simplemente lo recorremos entero.

No hace falta ninguna fuente de datos nueva ni ninguna clave adicional.

    from universe import universo_completo, universo_por_lotes

    todos = universo_completo()                      # ~10.000 tickers
    for lote in universo_por_lotes(tamaño=50):        # para procesar por partes
        ...
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterator

from providers import SecEdgarProvider

log = logging.getLogger(__name__)


@dataclass
class TickerUniverso:
    ticker: str
    cik: str
    nombre: str


def universo_completo(excluir_clases_raras: bool = True) -> list[TickerUniverso]:
    """Descarga el listado completo de tickers de la SEC. Es UNA sola petición
    HTTP (no 10.000): la SEC publica un fichero único con todo el mercado.

    El filtrado fino (tamaño, solvencia, calidad...) no se hace aquí — de eso
    se encarga `quant_filter.screen()` después, ticker a ticker. Esta función
    solo responde "¿qué existe?".

    `excluir_clases_raras=True` (por defecto) descarta tickers con puntos o
    guiones (BRK.A, GOOGL.WS...) — casi siempre son clases de acciones
    secundarias, preferentes o warrants que duplican la misma empresa."""
    provider = SecEdgarProvider()
    mapa = provider.full_ticker_list()          # {ticker: {"cik":..., "name":...}}

    out = []
    for ticker, info in mapa.items():
        if excluir_clases_raras and not ticker.isalpha():
            continue
        out.append(TickerUniverso(ticker=ticker, cik=info["cik"], nombre=info["name"]))

    log.info("Universo EDGAR: %d tickers", len(out))
    return out


def universo_por_lotes(tamaño: int = 50, **kwargs) -> Iterator[list[TickerUniverso]]:
    """Trocea el universo en lotes. Sirve para procesar por partes y guardar
    progreso entre lotes en vez de lanzar 10.000 peticiones seguidas de un tirón."""
    todos = universo_completo(**kwargs)
    for i in range(0, len(todos), tamaño):
        yield todos[i : i + tamaño]


if __name__ == "__main__":
    import sys

    u = universo_completo()
    print(f"Total de tickers en EDGAR: {len(u)}")
    print("Primeros 15:", [t.ticker for t in u[:15]])

    if "--buscar" in sys.argv:
        i = sys.argv.index("--buscar")
        termino = sys.argv[i + 1].upper()
        coincidencias = [t for t in u if termino in t.ticker or termino in t.nombre.upper()]
        print(f"\nCoincidencias con '{termino}' ({len(coincidencias)}):")
        for t in coincidencias[:20]:
            print(f"  {t.ticker:8} {t.nombre}")
