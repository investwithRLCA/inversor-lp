"""
Precios históricos para el módulo mensual — gratis y sin clave.

Fuente: Yahoo Finance vía la librería `yfinance` (no oficial, pero es la única
fuente gratuita con ~20 años de histórico ajustado por dividendos y splits que
funciona sin registrarse). Alternativas por si Yahoo bloquea las IPs de GitHub
Actions (pasa a veces; ver el hilo ranaroussi/yfinance#2431):
  - Stooq: desde marzo 2026 exige una clave gratuita (se pide en su web).
  - Alpaca (cuenta gratuita de paper trading): fiable y oficial, pero el
    histórico del plan gratuito empieza en 2016 — corto para este backtest.
Si Yahoo falla, este módulo lo dice claramente en vez de seguir con datos a
medias (ver `MIN_COBERTURA`).

Los precios son AJUSTADOS (dividendos reinvertidos): lo que importa para
comparar rentabilidades totales.

El universo del backtest es, de forma PROVISIONAL, el S&P 500 ACTUAL
(listado mantenido en github.com/datasets/s-and-p-500-companies). Eso sesga
el resultado al alza (las empresas que ya no están en el índice, porque
quebraron o se hundieron, no aparecen) — el backtest lo compensa con
controles, ver `backtest.py`.
"""

from __future__ import annotations

import io
import logging
import time
from pathlib import Path

import pandas as pd
import requests

log = logging.getLogger(__name__)

SP500_CSV = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
    "main/data/constituents.csv"
)
BENCHMARK = "SPY"
CACHE_DIR = Path(".cache_precios")
MIN_COBERTURA = 0.80       # si baja de aquí, se aborta: Yahoo está bloqueando


def _norm_ticker(x: str) -> str:
    return str(x).strip().upper().replace(".", "-")


def universo_sp500_con_sector() -> tuple[list[str], dict[str, str]]:
    """Tickers del S&P 500 actual (formato Yahoo: BRK.B → BRK-B) y su sector
    GICS. Si el listado no trajera la columna de sector, el diccionario sale
    vacío y el backtest simplemente no aplica el tope por sector."""
    r = requests.get(SP500_CSV, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    simbolos = df["Symbol"].map(_norm_ticker)
    sectores: dict[str, str] = {}
    col = next((c for c in df.columns if c.strip().lower() in ("gics sector", "sector")), None)
    if col:
        sectores = {t: str(sec).strip() for t, sec in zip(simbolos, df[col]) if isinstance(sec, str)}
    return sorted(set(simbolos)), sectores


def universo_sp500() -> list[str]:
    """Solo los tickers del S&P 500 actual."""
    return universo_sp500_con_sector()[0]


def _extraer_cierres(data: pd.DataFrame, tickers: list[str]) -> dict[str, pd.Series]:
    """Saca la serie de cierres ajustados de cada ticker de lo que devuelve
    yfinance, tolerando sus dos formatos de columnas (campo→ticker y
    ticker→campo) y el caso de un único ticker sin MultiIndex."""
    salida: dict[str, pd.Series] = {}
    if data is None or len(data) == 0:
        return salida

    if isinstance(data.columns, pd.MultiIndex):
        nivel0 = set(data.columns.get_level_values(0))
        for t in tickers:
            try:
                if "Close" in nivel0:                 # formato campo → ticker
                    s = data["Close"][t]
                elif t in nivel0:                     # formato ticker → campo
                    s = data[t]["Close"]
                else:
                    continue
            except KeyError:
                continue
            s = s.dropna()
            if len(s):
                salida[t] = s
    elif len(tickers) == 1 and "Close" in data.columns:
        s = data["Close"].dropna()
        if len(s):
            salida[tickers[0]] = s
    return salida


def descargar_precios(tickers: list[str], desde: str, lote: int = 40,
                      pausa: float = 3.0, reintentos: int = 3) -> pd.DataFrame:
    """Descarga cierres ajustados por lotes pequeños, con reintentos y pausa
    para no activar el límite de Yahoo."""
    import yfinance as yf  # import perezoso: solo hace falta aquí

    series: dict[str, pd.Series] = {}
    for i in range(0, len(tickers), lote):
        parte = tickers[i : i + lote]
        for intento in range(1, reintentos + 1):
            try:
                data = yf.download(
                    parte, start=desde, auto_adjust=True, progress=False,
                    group_by="ticker", threads=False,
                )
                encontrados = _extraer_cierres(data, parte)
                series.update(encontrados)
                if encontrados or intento == reintentos:
                    break
            except Exception as e:  # noqa: BLE001
                log.warning("Lote %d: intento %d/%d falló: %s", i // lote + 1, intento, reintentos, e)
            time.sleep(pausa * intento)
        print(f"  descargados {min(i + lote, len(tickers))}/{len(tickers)} "
              f"(con datos: {len(series)})")
        time.sleep(pausa)

    if not series:
        return pd.DataFrame()
    return pd.concat(series, axis=1)


def limpiar(precios: pd.DataFrame) -> pd.DataFrame:
    """Ordena, rellena huecos cortos (festivos sueltos de un ticker) y elimina
    columnas inservibles. No inventa datos más allá de 5 sesiones seguidas."""
    p = precios.sort_index()
    p = p[~p.index.duplicated(keep="last")]
    p = p.where(p > 0)
    p = p.ffill(limit=5)
    return p.dropna(axis=1, how="all")


def cargar_precios(tickers: list[str], desde: str, cache_dir: Path = CACHE_DIR,
                   max_dias_cache: int = 20) -> pd.DataFrame:
    """Precios limpios de `tickers` + SPY. Usa caché en disco si es reciente
    (en GitHub Actions se conserva entre ejecuciones con actions/cache)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    f = cache_dir / "precios.csv.gz"
    todos = sorted(set(tickers) | {BENCHMARK})

    if f.exists() and (time.time() - f.stat().st_mtime) < max_dias_cache * 86400:
        cache = pd.read_csv(f, index_col=0, parse_dates=True)
        if len(set(todos) & set(cache.columns)) >= MIN_COBERTURA * len(todos):
            print(f"Usando caché de precios ({f}, {len(cache.columns)} tickers).")
            return limpiar(cache)

    print(f"Descargando precios de {len(todos)} tickers desde {desde}...")
    bruto = descargar_precios(todos, desde)
    if bruto.empty or BENCHMARK not in bruto.columns:
        raise RuntimeError(
            "No se ha podido descargar ni siquiera el SPY. Casi seguro Yahoo Finance "
            "está bloqueando las peticiones desde este servidor. Reintenta en unas "
            "horas, o dime y cambiamos de fuente (Stooq con clave, o Alpaca)."
        )
    cobertura = len(set(bruto.columns) & set(todos)) / len(todos)
    if cobertura < MIN_COBERTURA:
        raise RuntimeError(
            f"Solo se han descargado {cobertura:.0%} de los tickers (mínimo "
            f"{MIN_COBERTURA:.0%}). Yahoo está limitando las peticiones; un backtest "
            "con tantos datos a medias no sería de fiar. Reintenta más tarde."
        )

    bruto.to_csv(f, compression="gzip")
    faltan = sorted(set(todos) - set(bruto.columns))
    if faltan:
        print(f"Sin datos ({len(faltan)}): {', '.join(faltan[:25])}{'…' if len(faltan) > 25 else ''}")
    return limpiar(bruto)
