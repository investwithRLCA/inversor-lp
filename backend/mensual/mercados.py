"""
¿En qué índice y en qué bolsa está cada empresa?  →  docs/mercados.json

Lo usa el Vigía (vista «Cartera») para filtrar las recomendaciones por mercado:
S&P 500, Nasdaq-100, Russell 2000, S&P 400, S&P 600 y bolsa (Nasdaq / NYSE).
Solo son listas públicas de empresas: nada de tu cartera.

Fuentes (gratis, sin claves):
  · S&P 500 ........ CSV de github.com/datasets/s-and-p-500-companies (respaldo: Wikipedia)
  · Nasdaq-100 ..... Wikipedia
  · Russell 2000 ... posiciones del ETF IWM (iShares); respaldo: ETF VTWO (Vanguard)
  · S&P 400 / 600 .. Wikipedia
  · Bolsa .......... SEC, company_tickers_exchange.json

Si una fuente falla, se conserva la lista de la ejecución anterior (con su fecha)
en vez de dejar el filtro vacío, y se avisa. Nunca se guarda una lista con menos
empresas de las razonables (p. ej. un Russell 2000 de 300 empresas).

    python -m mensual.mercados --salida ../docs/mercados.json
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests

VERSION = 1

# código → (nombre en pantalla, mínimo de empresas para darla por buena)
MERCADOS: dict[str, tuple[str, int]] = {
    "sp500": ("S&P 500", 480),
    "ndx": ("Nasdaq-100", 95),
    "r2000": ("Russell 2000", 1500),
    "sp400": ("S&P 400", 350),
    "sp600": ("S&P 600", 550),
    "nasdaq": ("Bolsa Nasdaq", 2000),
    "nyse": ("Bolsa NYSE", 1200),
}

UA_WEB = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) inversor-lp/1.0",
          "Accept": "text/csv,application/json,text/html;q=0.9,*/*;q=0.8"}

URL_SP500_CSV = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
URL_SP500_WIKI = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
URL_NDX_WIKI = "https://en.wikipedia.org/wiki/Nasdaq-100"
URL_SP400_WIKI = "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies"
URL_SP600_WIKI = "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies"
URL_IWM_CSV = "https://www.ishares.com/us/products/239710/x/latest-holdings.csv"
URL_IWM_CSV_2 = "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/latest-holdings.csv"
URL_IWM_AJAX = ("https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/"
                "1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund")
URL_VTWO = "https://investor.vanguard.com/vmf/api/VTWO/portfolio-holding/stock.json"
URL_SEC_BOLSA = "https://www.sec.gov/files/company_tickers_exchange.json"
URL_NDX_API = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"


# --------------------------------------------------------------------------- #
# Utilidades puras (se prueban sin red)
# --------------------------------------------------------------------------- #

def norm(x) -> str:
    """Ticker en el formato del bot: mayúsculas, BRK.B → BRK-B, sin espacios."""
    return str(x).strip().upper().replace(".", "-").replace("/", "-").replace(" ", "")


def valido(t: str) -> bool:
    return bool(t) and t not in ("-", "NAN", "NONE") and t.replace("-", "").isalpha() and len(t) <= 7


def limpiar(tickers) -> list[str]:
    vistos: set[str] = set()
    out: list[str] = []
    for x in tickers:
        t = norm(x)
        if valido(t) and t not in vistos:
            vistos.add(t)
            out.append(t)
    return sorted(out)


def tabla_ishares(texto: str) -> tuple[list[str], str | None]:
    """CSV de posiciones de iShares → (tickers de acciones, fecha «Fund Holdings as of»).
    Devuelve ([], None) si el texto no es ese CSV (p. ej. la página web del producto)."""
    texto = texto.lstrip("﻿")
    lineas = texto.splitlines()
    fecha = None
    for l in lineas[:12]:
        if l.replace('"', "").lower().startswith("fund holdings as of"):
            crudo = l.split(",", 1)[1].strip().strip('"') if "," in l else ""
            for fmt in ("%b %d, %Y", "%d/%b/%Y", "%m/%d/%Y"):
                try:
                    fecha = datetime.strptime(crudo, fmt).date().isoformat()
                    break
                except ValueError:
                    continue
    ini = next((i for i, l in enumerate(lineas) if l.replace('"', "").startswith("Ticker,")), None)
    if ini is None:
        return [], None
    df = pd.read_csv(io.StringIO("\n".join(lineas[ini:])), on_bad_lines="skip", dtype=str)
    df.columns = [str(c).strip() for c in df.columns]
    if "Asset Class" in df.columns:
        df = df[df["Asset Class"].astype(str).str.strip().str.lower() == "equity"]
    if "Ticker" not in df.columns:
        return [], None
    return limpiar(df["Ticker"].dropna().tolist()), fecha


def tickers_vanguard(paginas: list[dict]) -> tuple[list[str], str | None]:
    """Páginas JSON de la API de Vanguard → (tickers, fecha)."""
    ts, fecha = [], None
    for p in paginas:
        fecha = fecha or str(p.get("asOfDate") or "")[:10] or None
        for e in ((p.get("fund") or {}).get("entity") or []):
            if e.get("ticker"):
                ts.append(e["ticker"])
    return limpiar(ts), fecha


def bolsas_sec(js: dict) -> dict[str, list[str]]:
    """company_tickers_exchange.json → {"nasdaq": [...], "nyse": [...]}"""
    campos = js.get("fields") or []
    try:
        i_t, i_b = campos.index("ticker"), campos.index("exchange")
    except ValueError:
        return {"nasdaq": [], "nyse": []}
    nas, nyse = [], []
    for fila in js.get("data") or []:
        b = str(fila[i_b] or "").strip().lower()
        if b == "nasdaq":
            nas.append(fila[i_t])
        elif b.startswith("nyse"):
            nyse.append(fila[i_t])
    return {"nasdaq": limpiar(nas), "nyse": limpiar(nyse)}


def _cabecera(c) -> str:
    """Nombre de columna limpio: sin notas al pie «[1]» y, si es doble, su último nivel."""
    if isinstance(c, tuple):
        if any(str(x).strip().lower() in ("added", "removed") for x in c):
            return ""                            # tablas de altas y bajas del índice
        c = c[-1]
    return re.sub(r"\[.*?\]", "", str(c)).strip().lower()


def tickers_wikipedia(html: str, minimo: int) -> list[str]:
    """Primera tabla de Wikipedia con columna Symbol/Ticker y al menos `minimo` filas."""
    for t in pd.read_html(io.StringIO(html)):
        col = next((c for c in t.columns if _cabecera(c) in ("symbol", "ticker", "ticker symbol", "stock symbol")), None)
        if col is None:
            continue
        ts = limpiar(t[col].dropna().tolist())
        if len(ts) >= minimo:
            return ts
    return []


def cabeceras_wikipedia(html: str) -> list[str]:
    """Para el diagnóstico: columnas y filas de cada tabla de la página."""
    return [f"{len(t)} filas: {[str(c) for c in t.columns][:6]}" for t in pd.read_html(io.StringIO(html))]


def tickers_nasdaq_api(js: dict) -> list[str]:
    """api.nasdaq.com (lista del Nasdaq-100) → tickers."""
    filas = (((js or {}).get("data") or {}).get("data") or {}).get("rows") or []
    return limpiar([f.get("symbol") for f in filas if f.get("symbol")])


def construir(nuevas: dict[str, dict], previo: dict | None, hoy: str) -> dict:
    """Junta las listas descargadas hoy con las de la ejecución anterior.

    `nuevas[codigo]` = {"tickers": [...], "fuente": str, "fecha": str|None} o
    {"error": str}. Una lista vale si tiene al menos el mínimo de su mercado;
    si no, se conserva la anterior (marcada con su fecha) y se avisa."""
    prev = (previo or {}).get("mercados", {}) if (previo or {}).get("version") == VERSION else {}
    mercados, avisos = {}, []
    for cod, (nombre, minimo) in MERCADOS.items():
        n = nuevas.get(cod) or {"error": "sin intentar"}
        ts = n.get("tickers") or []
        if len(ts) >= minimo:
            mercados[cod] = {"nombre": nombre, "fuente": n.get("fuente", ""), "fecha": n.get("fecha") or hoy,
                             "n": len(ts), "tickers": ts}
            continue
        motivo = n.get("error") or f"solo {len(ts)} empresas (mínimo {minimo})"
        if cod in prev and len(prev[cod].get("tickers") or []) >= minimo:
            p = prev[cod]
            mercados[cod] = {**p, "nombre": nombre}
            avisos.append(f"{nombre}: no se pudo actualizar ({motivo}); se usa la lista anterior, del {p.get('fecha', '¿?')}.")
        else:
            avisos.append(f"{nombre}: no disponible ({motivo}).")
    return {"version": VERSION, "generado": hoy, "mercados": mercados, "avisos": avisos}


# --------------------------------------------------------------------------- #
# Descargas (necesitan red: se ejecutan en GitHub Actions)
# --------------------------------------------------------------------------- #

def _get(url: str, headers: dict | None = None, timeout: int = 60) -> requests.Response:
    r = requests.get(url, headers=headers or UA_WEB, timeout=timeout)
    r.raise_for_status()
    return r


def russell2000(detalle: bool = False) -> dict:
    """Componentes del Russell 2000: posiciones del ETF IWM (iShares) o, si no, del VTWO (Vanguard)."""
    errores = []
    for url in (URL_IWM_CSV, URL_IWM_CSV_2, URL_IWM_AJAX):
        try:
            r = _get(url, {**UA_WEB, "Referer": "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf"}, 90)
            ts, fecha = tabla_ishares(r.text)
            if detalle:
                print(f"  iShares {url[-40:]}: {len(r.text)} bytes, {len(ts)} acciones, fecha {fecha}")
            if len(ts) >= MERCADOS["r2000"][1]:
                return {"tickers": ts, "fuente": "posiciones del ETF IWM (iShares)", "fecha": fecha}
            errores.append(f"iShares: {len(ts)} acciones")
        except Exception as e:  # noqa: BLE001
            errores.append(f"iShares: {type(e).__name__}")
            if detalle:
                print(f"  iShares {url[-40:]}: {type(e).__name__}: {str(e)[:120]}")
    try:
        paginas, inicio = [], 1
        cab = {**UA_WEB, "Accept": "application/json", "Referer": "https://investor.vanguard.com/"}
        while inicio < 6000:
            p = _get(f"{URL_VTWO}?start={inicio}&count=500", cab).json()
            paginas.append(p)
            n = len(((p.get("fund") or {}).get("entity") or []))
            if detalle:
                print(f"  Vanguard VTWO start={inicio}: {n} posiciones (size={p.get('size')})")
            if n < 500:
                break
            inicio += 500
        ts, fecha = tickers_vanguard(paginas)
        if len(ts) >= MERCADOS["r2000"][1]:
            return {"tickers": ts, "fuente": "posiciones del ETF VTWO (Vanguard)", "fecha": fecha}
        errores.append(f"Vanguard: {len(ts)} acciones")
    except Exception as e:  # noqa: BLE001
        errores.append(f"Vanguard: {type(e).__name__}")
        if detalle:
            print(f"  Vanguard: {type(e).__name__}: {str(e)[:120]}")
    return {"error": "; ".join(errores)}


def sp500() -> dict:
    try:
        df = pd.read_csv(io.StringIO(_get(URL_SP500_CSV).text))
        ts = limpiar(df["Symbol"].tolist())
        if len(ts) >= MERCADOS["sp500"][1]:
            return {"tickers": ts, "fuente": "datasets/s-and-p-500-companies (GitHub)"}
    except Exception:  # noqa: BLE001
        pass
    return wikipedia("sp500", URL_SP500_WIKI)


def wikipedia(cod: str, url: str, detalle: bool = False) -> dict:
    try:
        html = _get(url).text
        ts = tickers_wikipedia(html, MERCADOS[cod][1])
        if not ts and detalle:
            print(f"  Wikipedia {cod}: tablas → " + " | ".join(cabeceras_wikipedia(html)[:8]))
        return {"tickers": ts, "fuente": "Wikipedia"} if ts else {"error": "tabla no encontrada en Wikipedia"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"Wikipedia: {type(e).__name__}"}


def nasdaq100(detalle: bool = False) -> dict:
    w = wikipedia("ndx", URL_NDX_WIKI, detalle)
    if w.get("tickers"):
        return w
    try:
        cab = {**UA_WEB, "Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com",
               "Referer": "https://www.nasdaq.com/", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
        ts = tickers_nasdaq_api(_get(URL_NDX_API, cab, 30).json())
        if detalle:
            print(f"  api.nasdaq.com: {len(ts)} empresas")
        if len(ts) >= MERCADOS["ndx"][1]:
            return {"tickers": ts, "fuente": "nasdaq.com"}
        return {"error": f"{w.get('error')}; nasdaq.com: {len(ts)} empresas"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{w.get('error')}; nasdaq.com: {type(e).__name__}"}


def bolsas() -> dict[str, dict]:
    ua = os.environ.get("SEC_USER_AGENT") or "inversor-lp contacto@example.com"
    try:
        js = _get(URL_SEC_BOLSA, {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}).json()
        b = bolsas_sec(js)
        return {k: {"tickers": v, "fuente": "SEC (company_tickers_exchange)"} for k, v in b.items()}
    except Exception as e:  # noqa: BLE001
        return {k: {"error": f"SEC: {type(e).__name__}"} for k in ("nasdaq", "nyse")}


def descargar(detalle: bool = False) -> dict[str, dict]:
    nuevas: dict[str, dict] = {
        "sp500": sp500(),
        "ndx": nasdaq100(detalle),
        "r2000": russell2000(detalle),
        "sp400": wikipedia("sp400", URL_SP400_WIKI),
        "sp600": wikipedia("sp600", URL_SP600_WIKI),
    }
    nuevas.update(bolsas())
    return nuevas


def resumen_tesis(res: dict, filas: list[dict]) -> list[str]:
    """Cuántas recomendaciones del bot (COMPRA FUERTE / COMPRA / VIGILAR) hay en cada mercado."""
    grupos = (("COMPRA_FUERTE",), ("COMPRA",), ("VIGILAR",))
    de: dict[str, set] = {}
    for cod, m in res["mercados"].items():
        for t in m["tickers"]:
            de.setdefault(t, set()).add(cod)
    indices = ("sp500", "ndx", "r2000", "sp400", "sp600")
    lineas = ["Mercado          COMPRA_FUERTE  COMPRA  VIGILAR"]
    for cod in list(MERCADOS) + ["otros"]:
        if cod != "otros" and cod not in res["mercados"]:
            continue
        nombre = MERCADOS[cod][0] if cod != "otros" else "Fuera de índices"
        def dentro(f):
            c = de.get(norm(f.get("ticker")), set())
            return not (c & set(indices)) if cod == "otros" else cod in c
        n = [sum(1 for f in filas if f.get("veredicto_final") in g and dentro(f)) for g in grupos]
        lineas.append(f"{nombre:16s} {n[0]:13d} {n[1]:7d} {n[2]:8d}")
    return lineas


def _tesis_supabase() -> list[dict]:
    url, clave = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not clave:
        return []
    from supabase import create_client
    r = create_client(url, clave).table("v_tesis_completa").select("ticker,veredicto_final").execute()
    return r.data or []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Listas de índices y bolsas para el filtro del Vigía")
    ap.add_argument("--salida", default="../docs/mercados.json")
    ap.add_argument("--detalle", action="store_true", help="imprime qué devuelve cada fuente")
    ap.add_argument("--resumen", action="store_true", help="cuenta las recomendaciones del bot por mercado (lee Supabase)")
    a = ap.parse_args(argv)
    salida = Path(a.salida)
    previo = None
    if salida.exists():
        try:
            previo = json.loads(salida.read_text(encoding="utf-8"))
        except ValueError:
            previo = None
    nuevas = descargar(a.detalle)
    res = construir(nuevas, previo, date.today().isoformat())
    for cod, (nombre, _) in MERCADOS.items():
        m = res["mercados"].get(cod)
        print(f"{nombre:14s} {m['n'] if m else 0:5d}  {(m or {}).get('fuente', '—')}  {(m or {}).get('fecha', '')}")
    for av in res["avisos"]:
        print("Aviso:", av)
    if a.resumen:
        try:
            filas = _tesis_supabase()
            if filas:
                print(f"\nRecomendaciones del bot por mercado ({len(filas)} empresas analizadas):")
                print("\n".join(resumen_tesis(res, filas)))
        except Exception as e:  # noqa: BLE001  (el resumen es informativo)
            print(f"(sin resumen: {type(e).__name__})")
    if not res["mercados"]:
        print("No se pudo obtener ninguna lista: no se toca el fichero.")
        return 1
    if previo and previo.get("mercados") == res["mercados"] and previo.get("avisos") == res["avisos"]:
        print("Sin cambios respecto al fichero anterior: no se reescribe.")
        return 0
    salida.parent.mkdir(parents=True, exist_ok=True)
    salida.write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Guardado {salida} ({salida.stat().st_size // 1024} KB).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
