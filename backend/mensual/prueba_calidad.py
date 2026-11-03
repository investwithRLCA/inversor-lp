"""
Prueba histórica de los tres paquetes con las cuentas «tal como se conocían».

Pregunta: si cada mes, desde 2015, se hubieran elegido las empresas que el bot
habría elegido con lo que YA era público ese día, ¿habrían rendido más que el
S&P 500 y que una selección sin criterio de la misma lista?

Cómo se hace (y por qué es justo):
  · Cada fin de mes se reconstruyen los fundamentales de cada empresa con lo que
    la SEC ya tenía publicado (`pit.py`) y se ejecuta el MISMO código del bot:
    cribado cuantitativo → reglas de cada paquete → puntuación → tope por sector.
  · El precio y las señales usan solo datos hasta esa fecha.
  · Se mide la rentabilidad total (dividendos incluidos) a 12, 24 y 36 meses.
  · Se compara con el S&P 500 (SPY) y con «cualquier empresa del listado a partes
    iguales» en las mismas fechas. Ese segundo control importa: el listado son las
    empresas que HOY están en el S&P 500, que ya ganan al índice por sí solas
    (sesgo de supervivencia). Un paquete solo aporta algo si bate también eso.

Regla de decisión, fijada ANTES de ver resultados:
  · Un paquete se mantiene solo si a 12 y a 24 meses su rentabilidad media supera
    TANTO al S&P 500 COMO a la media del listado.
  · Si no, se quita. Aprobar no demuestra nada (hay sesgo a favor); suspender sí
    es una señal clara.

Limitaciones que no se pueden quitar con datos gratis:
  · listado = componentes actuales (faltan las empresas que quebraron o salieron);
  · sin la IA ni el análisis de tesis (no existen en el pasado): se prueba el filtro
    cuantitativo y la regla de precio, que son lo que se puede reconstruir;
  · rentabilidades brutas: sin impuestos ni comisiones.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from quant_filter import screen

from .estrategia import Params, aplicar_tope_sector, elegir_candidatas, senales
from .paquetes import ReglasPaquetes, evaluar
from .pit import FuentePIT

HORIZONTES = (12, 24, 36)
PAQUETES = ("corto", "medio", "largo")
NOMBRE = {"corto": "1–3 años · Descuento con tesis (precio + cribado)",
          "medio": "3–5 años · Calidad a precio razonable",
          "largo": "5+ años · Compounders"}
MIN_FECHAS = 24           # menos fechas con resultado que esto: no se juzga


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #

def momento(adj: pd.DataFrame) -> np.ndarray:
    """Rendimiento de los últimos 12 meses sin el último (igual que el recomendador)."""
    a = adj.to_numpy(dtype=float)
    if len(a) < 253:
        return np.full(a.shape[1], np.nan)
    fin, ini = a[-22], a[-253]
    with np.errstate(invalid="ignore", divide="ignore"):
        m = fin / ini - 1.0
    m[~(np.isfinite(fin) & np.isfinite(ini) & (ini > 0))] = np.nan
    return m


def fines_de_mes(idx: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """Último día con cotización de cada mes."""
    s = pd.Series(idx, index=idx)
    return [pd.Timestamp(x) for x in s.groupby([idx.year, idx.month]).max().to_numpy()]


# --------------------------------------------------------------------------- #
# 1. Cohortes: qué habría elegido cada paquete en cada fecha
# --------------------------------------------------------------------------- #

@dataclass
class Cohortes:
    picks: pd.DataFrame       # fecha, paquete, ticker, sector, puntos, r12, r24, r36   (las elegidas: tope y sector)
    aptas: pd.DataFrame       # igual, pero TODAS las que cumplen las reglas (sin tope)
    ref: pd.DataFrame         # por fecha: spy_h, todas_h, cribado_h para cada horizonte
    n_cribado: pd.Series      # nº de empresas que pasan el cribado cada fecha


def calcular_cohortes(adj: pd.DataFrame, raw: pd.DataFrame, pit: FuentePIT, sectores: dict[str, str],
                      inicio: str = "2015-01-01", bench: str = "SPY", p: Params | None = None,
                      reglas: ReglasPaquetes | None = None, progreso: bool = True) -> Cohortes:
    p = p or Params()
    reglas = dataclasses.replace(reglas or ReglasPaquetes(), n_corto=p.n_max, n_max_sector=p.n_max_sector)
    acciones = adj.drop(columns=[bench], errors="ignore")
    cols = list(acciones.columns)
    pos = {c: k for k, c in enumerate(cols)}
    fin = fines_de_mes(adj.index)
    cod = {s: k for k, s in enumerate(sorted({sectores.get(c, "") for c in cols} - {""}))}
    sector_col = np.array([cod.get(sectores.get(c, ""), -1) for c in cols], dtype=int)
    adj_np = acciones.to_numpy(dtype=float)
    raw_np = raw.reindex(index=adj.index, columns=cols).to_numpy(dtype=float)
    spy = adj[bench].to_numpy(dtype=float) if bench in adj.columns else None
    filas_idx = {d: adj.index.get_loc(d) for d in fin}
    ini = pd.Timestamp(inicio)

    def fwd(j: int, h: int):
        """Rentabilidad de cada empresa de fin[j] a fin[j+h] (NaN si no hay)."""
        if j + h >= len(fin):
            return None
        a, b = filas_idx[fin[j]], filas_idx[fin[j + h]]
        with np.errstate(invalid="ignore", divide="ignore"):
            r = adj_np[b] / adj_np[a] - 1.0
        r[~(np.isfinite(adj_np[a]) & np.isfinite(adj_np[b]) & (adj_np[a] > 0))] = np.nan
        rs = None
        if spy is not None and np.isfinite(spy[a]) and np.isfinite(spy[b]) and spy[a] > 0:
            rs = spy[b] / spy[a] - 1.0
        return r, rs

    picks, aptas, ref, ncrib = [], [], [], {}
    p_sin_tope = dataclasses.replace(p, n_max=len(cols) or 1, n_max_sector=0)
    for j, fecha in enumerate(fin):
        if fecha < ini:
            continue
        hist = acciones.loc[:fecha]
        sen = senales(hist, fecha, p)
        desc = sen["descuento"].to_numpy(dtype=float)
        pct = sen["percentil"].to_numpy(dtype=float)
        reb = sen["rebote"].to_numpy(dtype=float)
        mom = momento(hist)
        ya = filas_idx[fecha]

        pasan = np.zeros(len(cols), dtype=bool)
        ev: dict[str, dict] = {"medio": {}, "largo": {}}
        for c in cols:
            k = pos[c]
            precio = raw_np[ya, k]
            if not np.isfinite(precio) or precio <= 0 or not np.isfinite(adj_np[ya, k]):
                continue
            f = pit.fundamentales(c, fecha, float(precio))
            if f is None:
                continue
            sr = screen(f)
            if not sr.passed:
                continue
            pasan[k] = True
            senal = {"percentil": pct[k], "descuento": desc[k], "mom": mom[k]}
            m = sr.metrics.to_dict()
            for paq in ("medio", "largo"):
                e = evaluar(paq, m, senal, f.is_financial, reglas)
                if e is not None:
                    ev[paq][c] = e
        ncrib[fecha] = int(pasan.sum())

        # --- referencias de esta fecha ---
        fila_ref = {"fecha": fecha}
        retornos = {}
        for h in HORIZONTES:
            r = fwd(j, h)
            if r is None:
                continue
            ret, rs = r
            retornos[h] = ret
            fila_ref[f"spy_{h}"] = rs if rs is not None else np.nan
            fila_ref[f"todas_{h}"] = float(np.nanmean(ret)) if np.isfinite(ret).any() else np.nan
            fila_ref[f"cribado_{h}"] = float(np.nanmean(ret[pasan])) if (pasan & np.isfinite(ret)).any() else np.nan
        ref.append(fila_ref)

        def registrar(destino, paquete, indices, puntos=None):
            for k in indices:
                reg = {"fecha": fecha, "paquete": paquete, "ticker": cols[int(k)],
                       "sector": sectores.get(cols[int(k)], ""),
                       "puntos": None if puntos is None else puntos[int(k)]}
                for h in HORIZONTES:
                    reg[f"r{h}"] = float(retornos[h][int(k)]) if h in retornos else np.nan
                destino.append(reg)

        # --- corto: cribado + regla de precio (ordenada por descuento) ---
        elegible = pasan & np.isfinite(desc)
        orden = elegir_candidatas(np.where(elegible, desc, np.nan), pct, reb, p_sin_tope, None)
        registrar(aptas, "corto", orden)
        registrar(picks, "corto", aplicar_tope_sector(orden, sector_col, reglas.n_max_sector, reglas.n_corto))
        # --- medio y largo: puertas + puntuación ---
        for paq, n in (("medio", reglas.n_medio), ("largo", reglas.n_largo)):
            if not ev[paq]:
                continue
            pts = np.full(len(cols), np.nan)
            for c, e in ev[paq].items():
                pts[pos[c]] = e["puntos"]
            orden = np.argsort(-np.where(np.isfinite(pts), pts, -1e9), kind="stable")
            orden = orden[np.isfinite(pts[orden])]
            registrar(aptas, paq, orden, pts)
            registrar(picks, paq, aplicar_tope_sector(orden, sector_col, reglas.n_max_sector, n), pts)
        if progreso and len(ref) % 12 == 0:
            print(f"  {fecha:%Y-%m}: pasan el cribado {ncrib[fecha]} · cumplen medio {len(ev['medio'])} · largo {len(ev['largo'])}")

    return Cohortes(picks=pd.DataFrame(picks), aptas=pd.DataFrame(aptas),
                    ref=pd.DataFrame(ref).set_index("fecha") if ref else pd.DataFrame(),
                    n_cribado=pd.Series(ncrib, dtype=float))


# --------------------------------------------------------------------------- #
# 2. Estadísticas
# --------------------------------------------------------------------------- #

def _bloques(x: np.ndarray, largo: int, n_boot: int = 2000, seed: int = 7):
    """IC 90 % de la media con bootstrap de bloques móviles circulares (las fechas
    mensuales con horizonte de h meses se solapan: se remuestrean trozos de h meses)."""
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 6:
        return None
    rng = np.random.default_rng(seed)
    largo = max(1, min(largo, n))
    nb = -(-n // largo)
    ini = rng.integers(0, n, size=(n_boot, nb))
    idx = (ini[:, :, None] + np.arange(largo)[None, None, :]) % n
    medias = x[idx].reshape(n_boot, -1)[:, :n].mean(axis=1)
    return float(np.percentile(medias, 5)), float(np.percentile(medias, 95))


def serie_cohorte(df: pd.DataFrame, paquete: str, h: int, quitar: str | None = None) -> pd.Series:
    """Rentabilidad media de lo elegido en cada fecha (a partes iguales)."""
    if df is None or df.empty:
        return pd.Series(dtype=float)
    d = df[df["paquete"] == paquete]
    if quitar:
        d = d[d["ticker"] != quitar]
    return d.groupby("fecha")[f"r{h}"].mean().dropna()


def estadisticas(coh: Cohortes, paquete: str, h: int, base: str = "picks") -> dict | None:
    df = coh.picks if base == "picks" else coh.aptas
    c = serie_cohorte(df, paquete, h)
    if len(c) == 0 or coh.ref.empty or f"spy_{h}" not in coh.ref.columns:
        return None
    ref = coh.ref.reindex(c.index)
    ok = c.notna() & ref[f"spy_{h}"].notna() & ref[f"todas_{h}"].notna()
    c, ref = c[ok], ref[ok]
    if len(c) < MIN_FECHAS:
        return {"n_fechas": int(len(c)), "insuficiente": True}
    spy, todas, crib = ref[f"spy_{h}"], ref[f"todas_{h}"], ref[f"cribado_{h}"]
    ex_spy, ex_todas, ex_crib = c - spy, c - todas, c - crib
    d = coh.picks if base == "picks" else coh.aptas
    n_por_fecha = d[d["paquete"] == paquete].groupby("fecha").size().reindex(c.index)
    anual = lambda m: (1 + m) ** (12.0 / h) - 1
    return {
        "n_fechas": int(len(c)), "n_medio_por_fecha": float(n_por_fecha.mean()),
        "media": float(c.mean()), "mediana": float(c.median()),
        "spy": float(spy.mean()), "todas": float(todas.mean()), "cribado": float(crib.mean()),
        "anual": float(anual(c.mean())), "anual_spy": float(anual(spy.mean())),
        "anual_todas": float(anual(todas.mean())),
        "ex_spy": float(ex_spy.mean()), "ex_todas": float(ex_todas.mean()), "ex_cribado": float(ex_crib.mean()),
        "ic_spy": _bloques(ex_spy.to_numpy(), h), "ic_todas": _bloques(ex_todas.to_numpy(), h),
        "ic_cribado": _bloques(ex_crib.to_numpy(), h),
        "gana_spy": float((ex_spy > 0).mean()), "gana_todas": float((ex_todas > 0).mean()),
        "peor_decil": float(np.percentile(c, 10)),
    }


def por_epoca(coh: Cohortes, paquete: str, h: int = 12) -> list[dict]:
    c = serie_cohorte(coh.picks, paquete, h)
    if c.empty or coh.ref.empty:
        return []
    ref = coh.ref.reindex(c.index)
    out = []
    for nombre, a, b in (("2015-2019", 2015, 2019), ("2020-2022", 2020, 2022), ("2023 en adelante", 2023, 2100)):
        m = (c.index.year >= a) & (c.index.year <= b) & ref[f"spy_{h}"].notna().to_numpy()
        if m.sum() < 6:
            continue
        cc, rr = c[m], ref[m]
        out.append({"epoca": nombre, "n": int(m.sum()), "media": float(cc.mean()),
                    "spy": float(rr[f"spy_{h}"].mean()), "todas": float(rr[f"todas_{h}"].mean())})
    return out


def concentracion(coh: Cohortes, paquete: str, h: int = 12):
    """¿Depende de una sola empresa? Aporte de cada empresa al exceso sobre el S&P 500
    y exceso medio quitando la que más aporta."""
    d = coh.picks[coh.picks["paquete"] == paquete].dropna(subset=[f"r{h}"])
    if d.empty or coh.ref.empty:
        return None
    ref = coh.ref[f"spy_{h}"]
    d = d.assign(spy=d["fecha"].map(ref)).dropna(subset=["spy"])
    if d.empty:
        return None
    d["n"] = d.groupby("fecha")["ticker"].transform("size")
    d["aporte"] = (d[f"r{h}"] - d["spy"]) / d["n"]
    fechas = d["fecha"].nunique()
    por_t = (d.groupby("ticker")["aporte"].sum() / fechas).sort_values(ascending=False)
    total = float(por_t.sum())
    mejor = por_t.index[0]
    sin = serie_cohorte(coh.picks, paquete, h, quitar=mejor)
    ref2 = coh.ref.reindex(sin.index)[f"spy_{h}"]
    ex_sin = float((sin - ref2).dropna().mean()) if len(sin) else float("nan")
    return {"top": [(t, float(v)) for t, v in por_t.head(5).items()], "total": total,
            "mejor": mejor, "ex_sin_mejor": ex_sin, "n_empresas": int(d["ticker"].nunique())}


def decidir(coh: Cohortes, paquete: str) -> dict:
    """Aplica la regla fijada de antemano: mantener solo si a 12 y a 24 meses la
    media supera al S&P 500 y al listado."""
    res = {"paquete": paquete, "mantener": False, "motivos": [], "stats": {}}
    for h in (12, 24):
        s = estadisticas(coh, paquete, h)
        res["stats"][h] = s
        if s is None or s.get("insuficiente"):
            res["motivos"].append(f"a {h} meses hay pocas fechas con resultado ({(s or {}).get('n_fechas', 0)}): no se puede juzgar")
            continue
        if s["ex_spy"] <= 0:
            res["motivos"].append(f"a {h} meses NO supera al S&P 500 ({s['ex_spy'] * 100:+.1f} pp)")
        if s["ex_todas"] <= 0:
            res["motivos"].append(f"a {h} meses NO supera a una selección sin criterio del mismo listado ({s['ex_todas'] * 100:+.1f} pp)")
    res["mantener"] = not res["motivos"]
    return res


# --------------------------------------------------------------------------- #
# 3. Informe
# --------------------------------------------------------------------------- #

def _p(x, dec=1, signo=False):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "—"
    return f"{x * 100:+.{dec}f} %" if signo else f"{x * 100:.{dec}f} %"


def _pp(x, dec=1):
    return "—" if x is None or not np.isfinite(x) else f"{x * 100:+.{dec}f} pp"


def _ic(ic):
    return "—" if not ic else f"[{ic[0] * 100:+.1f}, {ic[1] * 100:+.1f}]"


def informe_md(coh: Cohortes, desde: str, n_empresas: int, n_con_sec: int) -> str:
    L: list[str] = []
    A = L.append
    A("# ¿Habrían batido al S&P 500 los paquetes del módulo mensual?\n")
    A("Prueba histórica con las cuentas **tal como se conocían en cada fecha** (SEC, fecha de presentación de cada cifra) "
      "y el MISMO código del bot: cribado de calidad → reglas del paquete → puntuación → tope por sector. "
      f"Cada fin de mes desde {desde[:7]}; se mantiene 12, 24 o 36 meses. Rentabilidades totales **brutas** "
      "(dividendos incluidos; sin impuestos ni comisiones).\n")
    A(f"Listado: componentes actuales del S&P 500 ({n_empresas} empresas; {n_con_sec} con cuentas utilizables en la SEC). "
      "**Sesgo de supervivencia:** son las que HOY siguen en el índice y por eso ya le ganan por sí solas; "
      "por eso se compara también con «cualquier empresa del listado a partes iguales».\n")
    A("**Regla de decisión (fijada antes de ver los resultados):** un paquete se mantiene solo si a 12 y a 24 meses su "
      "rentabilidad media supera al S&P 500 **y** al listado a partes iguales. Aprobar no demuestra nada (hay sesgo a favor); "
      "suspender es una señal clara.\n")
    if not coh.n_cribado.empty:
        A(f"Empresas que pasan el cribado de calidad cada mes: mediana {coh.n_cribado.median():.0f} "
          f"(mín. {coh.n_cribado.min():.0f}, máx. {coh.n_cribado.max():.0f}).\n")

    decs = {p: decidir(coh, p) for p in PAQUETES}
    A("## Veredicto\n")
    A("| Paquete | Veredicto | Por qué |")
    A("|---|---|---|")
    for p in PAQUETES:
        d = decs[p]
        A(f"| {NOMBRE[p]} | {'✅ se mantiene (en prueba)' if d['mantener'] else '❌ se quita'} | "
          f"{'supera al S&P 500 y al listado a 12 y 24 meses' if d['mantener'] else '; '.join(d['motivos'])} |")
    A("")
    for p in PAQUETES:
        d = decs[p]
        A(f"## {NOMBRE[p]}\n")
        A("**Lo que habría elegido el paquete** (máx. por fecha según reglas), a partes iguales:\n")
        A("| Horizonte | Fechas | Nº medio | Media elegidas | S&P 500 | Listado (iguales) | Exceso vs S&P 500 [IC 90 %] | Exceso vs listado [IC 90 %] | Meses que ganan al S&P | Peor 10 % |")
        A("|---|---:|---:|---:|---:|---:|---|---|---:|---:|")
        for h in HORIZONTES:
            s = estadisticas(coh, p, h)
            if not s or s.get("insuficiente"):
                A(f"| {h} m | {(s or {}).get('n_fechas', 0)} | | pocas fechas | | | | | | |")
                continue
            A(f"| {h} m | {s['n_fechas']} | {s['n_medio_por_fecha']:.1f} | {_p(s['media'])} | {_p(s['spy'])} | {_p(s['todas'])} | "
              f"{_pp(s['ex_spy'])} {_ic(s['ic_spy'])} | {_pp(s['ex_todas'])} {_ic(s['ic_todas'])} | {_p(s['gana_spy'], 0)} | {_p(s['peor_decil'])} |")
        A("")
        a12 = estadisticas(coh, p, 12, base="aptas")
        a24 = estadisticas(coh, p, 24, base="aptas")
        if a12 and not a12.get("insuficiente"):
            A(f"**Todas las que cumplen las reglas** (sin tope de nº): a 12 m {_p(a12['media'])} frente a {_p(a12['spy'])} del S&P 500 "
              f"y {_p(a12['todas'])} del listado ({_pp(a12['ex_spy'])} / {_pp(a12['ex_todas'])})"
              + (f"; a 24 m {_p(a24['media'])} frente a {_p(a24['spy'])} y {_p(a24['todas'])}." if a24 and not a24.get("insuficiente") else ".")
              + f" Frente a las que solo pasan el cribado: {_pp(a12['ex_cribado'])} a 12 m.\n")
        ep = por_epoca(coh, p)
        if ep:
            A("**Por época** (a 12 meses):\n")
            A("| Época | Fechas | Elegidas | S&P 500 | Listado |")
            A("|---|---:|---:|---:|---:|")
            for e in ep:
                A(f"| {e['epoca']} | {e['n']} | {_p(e['media'])} | {_p(e['spy'])} | {_p(e['todas'])} |")
            A("")
        c = concentracion(coh, p)
        if c:
            tops = ", ".join(f"{t} ({v * 100:+.1f} pp)" for t, v in c["top"])
            A(f"**¿Depende de una empresa?** Las que más aportan al exceso sobre el S&P 500 a 12 m: {tops}. "
              f"Quitando {c['mejor']}, el exceso medio pasa a {_pp(c['ex_sin_mejor'])} (en total {c['n_empresas']} empresas distintas elegidas).\n")
        A(f"**Decisión:** {'se mantiene en prueba' if d['mantener'] else 'se quita'}."
          + ("" if d["mantener"] else " Motivos: " + "; ".join(d["motivos"]) + ".") + "\n")
    A("---\n")
    A("**Cómo leerlo.** «Se mantiene» significa solo que no suspende: con el sesgo de supervivencia a favor, no es una prueba de que "
      "vaya a batir al índice. «Se quita» significa que ni siquiera con esa ventaja lo consigue. La IA y el análisis de tesis no se "
      "pueden reconstruir en el pasado, así que no están en esta prueba.")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# 4. Datos reales y ejecución
# --------------------------------------------------------------------------- #

def main() -> None:
    ap = argparse.ArgumentParser(description="Prueba histórica point-in-time de los paquetes.")
    ap.add_argument("--salida", default="resultados_calidad")
    ap.add_argument("--cache", default=".cache_pit", help="carpeta con las cuentas de la SEC ya descargadas")
    ap.add_argument("--desde-precios", default="2008-01-01")
    ap.add_argument("--inicio", default="2015-01-01", help="primera fecha de selección")
    ap.add_argument("--max-empresas", type=int, default=0, help="solo para pruebas")
    args = ap.parse_args()

    from .dividendos import descargar_dividendos
    from .precios import universo_sp500_con_sector, BENCHMARK

    tickers, sectores = universo_sp500_con_sector()
    if args.max_empresas:
        tickers = tickers[:args.max_empresas]
    print(f"Universo: {len(tickers)} empresas del S&P 500 actual.")
    pit = FuentePIT.descargar(tickers, Path(args.cache))
    print(f"Cuentas de la SEC utilizables: {len(pit.datos)}/{len(tickers)}")
    if len(pit.datos) < 0.6 * len(tickers):
        raise RuntimeError("La SEC ha devuelto cuentas de menos del 60 % de las empresas: resultado poco fiable.")

    usables = sorted(pit.datos)
    print("Descargando precios (cierre y cierre ajustado)...")
    datos = descargar_dividendos(sorted(set(usables) | {BENCHMARK}), args.desde_precios)
    if BENCHMARK not in datos:
        raise RuntimeError("No se pudo descargar el SPY: Yahoo está bloqueando o caído.")
    cierre = pd.concat({t: d["close"] for t, d in datos.items()}, axis=1).sort_index()
    ajust = pd.concat({t: d["adj"] for t, d in datos.items()}, axis=1).sort_index()
    cierre, ajust = cierre.where(cierre > 0), ajust.where(ajust > 0)
    ajust = ajust.ffill(limit=5)
    cierre = cierre.reindex(ajust.index).ffill(limit=5)
    cov = len(set(ajust.columns) & set(usables)) / max(1, len(usables))
    print(f"Precios: {len(ajust.columns)} columnas, cobertura {cov:.0%}, {ajust.index[0]:%Y-%m-%d} → {ajust.index[-1]:%Y-%m-%d}")
    if cov < 0.8:
        raise RuntimeError(f"Solo hay precios del {cov:.0%} de las empresas: resultado poco fiable.")

    print("Reconstruyendo, mes a mes, lo que habría elegido cada paquete...")
    coh = calcular_cohortes(ajust, cierre, pit, sectores, inicio=args.inicio)
    md = informe_md(coh, args.inicio, len(tickers), len(pit.datos))
    out = Path(args.salida)
    out.mkdir(parents=True, exist_ok=True)
    (out / "informe.md").write_text(md, encoding="utf-8")
    coh.picks.to_csv(out / "elegidas.csv", index=False)
    coh.aptas.to_csv(out / "aptas.csv.gz", index=False, compression="gzip")
    coh.ref.to_csv(out / "referencias.csv")
    (out / "decisiones.json").write_text(json.dumps(
        {p: {"mantener": d["mantener"], "motivos": d["motivos"]} for p, d in
         ((p, decidir(coh, p)) for p in PAQUETES)}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + md)


if __name__ == "__main__":
    warnings.filterwarnings("ignore")
    main()
