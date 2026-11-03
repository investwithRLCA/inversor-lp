"""
Prueba histórica del filtro Peter Lynch (lynch.py) con las cuentas «tal como se conocían».

Pregunta: si cada fin de mes desde 2015 se hubieran comprado las empresas que pasan el
filtro Lynch con lo que YA era público ese día, ¿habrían rendido más que el índice y que
una selección sin criterio del mismo grupo? ¿Cuántas veces acierta y cuánto ganan las
buenas frente a lo que pierden las malas?

Universo: las empresas que HOY están en el S&P 500, S&P 400, S&P 600, Russell 2000 o
Nasdaq-100 (docs/mercados.json), ~2.900. En cada fecha se separan por tamaño de ese día:
  · «grandes»: las 1.000 mayores (≈ Russell 1000) → se comparan con el S&P 500 (SPY);
  · «pequeñas»: el resto con más de 300 M$ (≈ Russell 2000) → con el Russell 2000 (IWM).

Variantes (fijadas antes de ver resultados):
  · lynch: solo las reglas de Lynch (lynch.py) → las 15 con más puntos, máx. 3 por sector;
  · lynch_cribado: además tienen que pasar el cribado de calidad del bot (quant_filter).

Regla de decisión (fijada ANTES de ver resultados, la misma de los paquetes): una variante
vale en un grupo solo si a 12 y a 24 meses su rentabilidad media supera AL ÍNDICE y A LA
MEDIA DEL GRUPO a partes iguales. Aprobar no demuestra nada (hay sesgo de supervivencia
a favor); suspender es una señal clara.

Además, lo que preguntaba Lynch: % de elegidas que baten al índice («si eres bueno,
aciertas 6 de cada 10») y cuánto ganan las que aciertan frente a lo que pierden las que
fallan (unas pocas muy buenas compensan muchas regulares).

Límites: sin empresas que quebraron o desaparecieron (sesgo de supervivencia); sin compras
de directivos ni % de fondos (no hay datos gratis por fecha); rentabilidades brutas.
"""

from __future__ import annotations

import argparse
import json
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from lynch import ReglasLynch, evaluar as evaluar_lynch
from quant_filter import screen

from .estrategia import aplicar_tope_sector
from .pit import FuentePIT
from .prueba_calidad import _bloques, fines_de_mes

HORIZONTES = (12, 24, 36)
VARIANTES = ("lynch", "lynch_cribado")
GRUPOS = ("grandes", "pequenas")
NOMBRE_VAR = {"lynch": "Filtro Lynch", "lynch_cribado": "Filtro Lynch + cribado de calidad del bot"}
NOMBRE_GRUPO = {"grandes": "Grandes (las 1.000 mayores)", "pequenas": "Pequeñas (resto, > 300 M$)"}
BENCH = {"grandes": "SPY", "pequenas": "IWM"}
NOMBRE_BENCH = {"grandes": "S&P 500", "pequenas": "Russell 2000"}
N_GRANDES = 1000
CAP_MIN = 300.0
N_ELEGIDAS = 15
MAX_SECTOR = 3
MIN_FECHAS = 24
RUTA_MERCADOS = Path(__file__).resolve().parents[2] / "docs" / "mercados.json"


@dataclass
class Resultado:
    picks: pd.DataFrame      # fecha, variante, grupo, ticker, sector, categoria, puntos, r12, r24, r36
    aptas: pd.DataFrame      # igual, todas las que pasan (sin tope)
    ref: pd.DataFrame        # fecha, grupo → bench_h, todas_h, n, acierto_base_h, gana_base_h, pierde_base_h, dobla_base, hunde_base
    n_grupo: pd.DataFrame    # fecha, grupo → nº empresas, nº pasan lynch, nº pasan lynch_cribado


def calcular(adj: pd.DataFrame, pit: FuentePIT, sectores: dict[str, str], inicio: str = "2015-01-01",
             reglas: ReglasLynch | None = None, progreso: bool = True, crudo: pd.DataFrame | None = None) -> Resultado:
    """`adj`: precios ajustados (rentabilidades); `crudo`: precios sin ajustar (PER y capitalización)."""
    reglas = reglas or ReglasLynch()
    benchs = [b for b in set(BENCH.values()) if b in adj.columns]
    acciones = adj.drop(columns=benchs, errors="ignore")
    cols = [c for c in acciones.columns if c in pit.datos]
    acciones = acciones[cols]
    a = acciones.to_numpy(dtype=float)
    cr = crudo.reindex(index=adj.index, columns=cols).to_numpy(dtype=float) if crudo is not None else a
    bench_np = {b: adj[b].to_numpy(dtype=float) for b in benchs}
    fin = fines_de_mes(adj.index)
    fila = {d: adj.index.get_loc(d) for d in fin}
    cod = {s: k for k, s in enumerate(sorted({sectores.get(c, "") for c in cols} - {""}))}
    sec_col = np.array([cod.get(sectores.get(c, ""), -1) for c in cols], dtype=int)
    ini = pd.Timestamp(inicio)

    def fwd(j: int, h: int):
        if j + h >= len(fin):
            return None, {}
        x, y = fila[fin[j]], fila[fin[j + h]]
        with np.errstate(invalid="ignore", divide="ignore"):
            r = a[y] / a[x] - 1.0
        r[~(np.isfinite(a[x]) & np.isfinite(a[y]) & (a[x] > 0))] = np.nan
        rb = {}
        for b, s in bench_np.items():
            if np.isfinite(s[x]) and np.isfinite(s[y]) and s[x] > 0:
                rb[b] = s[y] / s[x] - 1.0
        return r, rb

    picks, aptas, ref, ngr = [], [], [], []
    for j, fecha in enumerate(fin):
        if fecha < ini:
            continue
        x = fila[fecha]
        caps = np.full(len(cols), np.nan)
        res_l: dict[int, object] = {}
        for k, c in enumerate(cols):
            precio = cr[x, k]
            if not np.isfinite(precio) or precio <= 0 or not np.isfinite(a[x, k]):
                continue
            f = pit.fundamentales(c, fecha, float(precio))
            if f is None or not f.shares_diluted or not f.market_cap:
                continue
            res_l[k] = f
            caps[k] = f.market_cap
        validas = np.isfinite(caps) & (caps >= CAP_MIN)
        orden_cap = np.argsort(-np.where(validas, caps, -1))
        n_val = int(validas.sum())
        grupo = np.full(len(cols), "", dtype=object)
        grupo[orden_cap[:min(N_GRANDES, n_val)]] = "grandes"
        grupo[orden_cap[min(N_GRANDES, n_val):n_val]] = "pequenas"

        pts = {v: np.full(len(cols), np.nan) for v in VARIANTES}
        cat = {}
        for k in np.where(validas)[0]:
            f = res_l[int(k)]
            rl = evaluar_lynch(f, reglas)
            if not rl.pasa:
                continue
            pts["lynch"][k] = rl.puntos
            cat[int(k)] = rl.categoria
            if screen(f).passed:
                pts["lynch_cribado"][k] = rl.puntos

        retornos, rbench = {}, {}
        for h in HORIZONTES:
            r, rb = fwd(j, h)
            if r is not None:
                retornos[h], rbench[h] = r, rb

        for g in GRUPOS:
            en = grupo == g
            if not en.any():
                continue
            fr = {"fecha": fecha, "grupo": g, "n": int(en.sum())}
            for h, r in retornos.items():
                b = rbench[h].get(BENCH[g], np.nan)
                rr = r[en & np.isfinite(r)]
                fr[f"bench_{h}"] = b
                fr[f"todas_{h}"] = float(rr.mean()) if len(rr) else np.nan
                if len(rr) and np.isfinite(b):
                    ex = rr - b
                    fr[f"acierto_base_{h}"] = float((ex > 0).mean())
                    fr[f"gana_base_{h}"] = float(ex[ex > 0].mean()) if (ex > 0).any() else np.nan
                    fr[f"pierde_base_{h}"] = float(ex[ex <= 0].mean()) if (ex <= 0).any() else np.nan
                if h == 36 and len(rr):
                    fr["dobla_base"] = float((rr >= 1.0).mean())
                    fr["hunde_base"] = float((rr <= -0.5).mean())
            ref.append(fr)
            ngr.append({"fecha": fecha, "grupo": g, "n": int(en.sum()),
                        **{f"n_{v}": int((en & np.isfinite(pts[v])).sum()) for v in VARIANTES}})

            for v in VARIANTES:
                p = np.where(en & np.isfinite(pts[v]), pts[v], np.nan)
                orden = np.argsort(-np.where(np.isfinite(p), p, -1e9), kind="stable")
                orden = orden[np.isfinite(p[orden])]
                elegidas = aplicar_tope_sector(orden, sec_col, MAX_SECTOR, N_ELEGIDAS)
                for destino, idx in ((aptas, orden), (picks, elegidas)):
                    for k in idx:
                        k = int(k)
                        reg = {"fecha": fecha, "variante": v, "grupo": g, "ticker": cols[k],
                               "sector": sectores.get(cols[k], ""), "categoria": cat.get(k), "puntos": float(p[k])}
                        for h in HORIZONTES:
                            reg[f"r{h}"] = float(retornos[h][k]) if h in retornos else np.nan
                            reg[f"b{h}"] = rbench[h].get(BENCH[g], np.nan) if h in rbench else np.nan
                        destino.append(reg)
        if progreso and (fecha.month == 12):
            ultimo = [n for n in ngr if n["fecha"] == fecha]
            print(f"  {fecha:%Y-%m}: " + " · ".join(
                f"{n['grupo']} {n['n']} (Lynch {n['n_lynch']}, +cribado {n['n_lynch_cribado']})" for n in ultimo))

    return Resultado(picks=pd.DataFrame(picks), aptas=pd.DataFrame(aptas), ref=pd.DataFrame(ref), n_grupo=pd.DataFrame(ngr))


# --------------------------------------------------------------------------- #
# Estadísticas
# --------------------------------------------------------------------------- #

def _ref(res: Resultado, g: str) -> pd.DataFrame:
    if res.ref.empty:
        return pd.DataFrame()
    return res.ref[res.ref["grupo"] == g].set_index("fecha")


def estadisticas(res: Resultado, v: str, g: str, h: int, base: str = "picks") -> dict | None:
    df = res.picks if base == "picks" else res.aptas
    if df.empty:
        return None
    d = df[(df["variante"] == v) & (df["grupo"] == g)]
    c = d.groupby("fecha")[f"r{h}"].mean().dropna()
    ref = _ref(res, g)
    if c.empty or ref.empty or f"bench_{h}" not in ref.columns:
        return None
    ref = ref.reindex(c.index)
    ok = c.notna() & ref[f"bench_{h}"].notna() & ref[f"todas_{h}"].notna()
    c, ref = c[ok], ref[ok]
    if len(c) < MIN_FECHAS:
        return {"n_fechas": int(len(c)), "insuficiente": True}
    ex_b, ex_t = c - ref[f"bench_{h}"], c - ref[f"todas_{h}"]
    anual = lambda m: (1 + m) ** (12.0 / h) - 1
    # aciertos, empresa a empresa
    dd = d[d["fecha"].isin(c.index)].dropna(subset=[f"r{h}", f"b{h}"])
    ex_i = (dd[f"r{h}"] - dd[f"b{h}"]).to_numpy()
    gana, pierde = ex_i[ex_i > 0], ex_i[ex_i <= 0]
    out = {
        "n_fechas": int(len(c)), "n_medio": float(d.groupby("fecha").size().reindex(c.index).mean()),
        "media": float(c.mean()), "bench": float(ref[f"bench_{h}"].mean()), "todas": float(ref[f"todas_{h}"].mean()),
        "anual": float(anual(c.mean())), "anual_bench": float(anual(ref[f"bench_{h}"].mean())),
        "anual_todas": float(anual(ref[f"todas_{h}"].mean())),
        "ex_bench": float(ex_b.mean()), "ex_todas": float(ex_t.mean()),
        "ic_bench": _bloques(ex_b.to_numpy(), h), "ic_todas": _bloques(ex_t.to_numpy(), h),
        "meses_gana": float((ex_b > 0).mean()),
        "n_compras": int(len(ex_i)),
        "acierto": float((ex_i > 0).mean()) if len(ex_i) else np.nan,
        "gana_media": float(gana.mean()) if len(gana) else np.nan,
        "pierde_media": float(pierde.mean()) if len(pierde) else np.nan,
        "acierto_base": float(ref.get(f"acierto_base_{h}", pd.Series(dtype=float)).mean()),
        "gana_base": float(ref.get(f"gana_base_{h}", pd.Series(dtype=float)).mean()),
        "pierde_base": float(ref.get(f"pierde_base_{h}", pd.Series(dtype=float)).mean()),
    }
    out["ratio"] = (out["gana_media"] / abs(out["pierde_media"])) if out["pierde_media"] and np.isfinite(out["pierde_media"]) and out["pierde_media"] != 0 else np.nan
    out["ratio_base"] = (out["gana_base"] / abs(out["pierde_base"])) if out["pierde_base"] and np.isfinite(out["pierde_base"]) and out["pierde_base"] != 0 else np.nan
    if h == 36:
        r36 = dd["r36"].to_numpy()
        out["dobla"] = float((r36 >= 1.0).mean()) if len(r36) else np.nan
        out["hunde"] = float((r36 <= -0.5).mean()) if len(r36) else np.nan
        out["dobla_base"] = float(ref.get("dobla_base", pd.Series(dtype=float)).mean())
        out["hunde_base"] = float(ref.get("hunde_base", pd.Series(dtype=float)).mean())
    return out


def decidir(res: Resultado, v: str, g: str) -> dict:
    d = {"variante": v, "grupo": g, "veredicto": "quitar", "motivos": [], "stats": {}}
    pocos = []
    for h in (12, 24):
        s = estadisticas(res, v, g, h)
        d["stats"][h] = s
        if s is None or s.get("insuficiente"):
            pocos.append(f"a {h} meses hay pocas fechas con resultado ({(s or {}).get('n_fechas', 0)})")
            continue
        if s["ex_bench"] <= 0:
            d["motivos"].append(f"a {h} meses NO supera al {NOMBRE_BENCH[g]} ({s['ex_bench'] * 100:+.1f} pp)")
        if s["ex_todas"] <= 0:
            d["motivos"].append(f"a {h} meses NO supera a elegir sin criterio en el mismo grupo ({s['ex_todas'] * 100:+.1f} pp)")
    if d["motivos"]:
        d["veredicto"] = "quitar"
    elif pocos:
        d["veredicto"], d["motivos"] = "sin_datos", pocos
    else:
        d["veredicto"] = "mantener"
    return d


def por_epoca(res: Resultado, v: str, g: str, h: int = 12) -> list[dict]:
    d = res.picks[(res.picks["variante"] == v) & (res.picks["grupo"] == g)] if not res.picks.empty else pd.DataFrame()
    if d.empty:
        return []
    c = d.groupby("fecha")[f"r{h}"].mean().dropna()
    ref = _ref(res, g).reindex(c.index)
    out = []
    for nombre, a, b in (("2015-2019", 2015, 2019), ("2020-2022", 2020, 2022), ("2023 en adelante", 2023, 2100)):
        m = (c.index.year >= a) & (c.index.year <= b) & ref[f"bench_{h}"].notna().to_numpy()
        if m.sum() < 6:
            continue
        out.append({"epoca": nombre, "n": int(m.sum()), "media": float(c[m].mean()),
                    "bench": float(ref[f"bench_{h}"][m].mean()), "todas": float(ref[f"todas_{h}"][m].mean())})
    return out


def concentracion(res: Resultado, v: str, g: str, h: int = 12):
    d = res.picks[(res.picks["variante"] == v) & (res.picks["grupo"] == g)].dropna(subset=[f"r{h}", f"b{h}"]) if not res.picks.empty else pd.DataFrame()
    if d.empty:
        return None
    d = d.copy()
    d["n"] = d.groupby("fecha")["ticker"].transform("size")
    d["aporte"] = (d[f"r{h}"] - d[f"b{h}"]) / d["n"]
    fechas = d["fecha"].nunique()
    por_t = (d.groupby("ticker")["aporte"].sum() / fechas).sort_values(ascending=False)
    mejor = por_t.index[0]
    sin = d[d["ticker"] != mejor].groupby("fecha").apply(lambda x: (x[f"r{h}"] - x[f"b{h}"]).mean())
    return {"top": [(t, float(x)) for t, x in por_t.head(5).items()], "mejor": mejor,
            "ex_sin_mejor": float(sin.mean()) if len(sin) else np.nan, "n_empresas": int(d["ticker"].nunique())}


# --------------------------------------------------------------------------- #
# Informe
# --------------------------------------------------------------------------- #

def _p(x, dec=1):
    return "—" if x is None or not np.isfinite(x) else f"{x * 100:.{dec}f} %"


def _pp(x, dec=1):
    return "—" if x is None or not np.isfinite(x) else f"{x * 100:+.{dec}f} pp"


def _ic(ic):
    return "—" if not ic else f"[{ic[0] * 100:+.1f}, {ic[1] * 100:+.1f}]"


def _r(x):
    return "—" if x is None or not np.isfinite(x) else f"{x:.2f}"


def informe_md(res: Resultado, desde: str, n_univ: int, n_sec: int) -> str:
    L: list[str] = []
    A = L.append
    A("# ¿Habría batido al mercado el filtro Peter Lynch?\n")
    A("Prueba con las cuentas **tal como se conocían en cada fecha** (SEC) y el mismo código del bot (`lynch.py`). "
      f"Cada fin de mes desde {desde[:7]} se eligen las {N_ELEGIDAS} con más puntos (máx. {MAX_SECTOR} por sector) y se "
      "mantienen 12, 24 o 36 meses. Rentabilidades totales **brutas** (dividendos incluidos; sin impuestos ni comisiones).\n")
    A(f"Universo: empresas que hoy están en el S&P 500, S&P 400, S&P 600, Russell 2000 o Nasdaq-100 ({n_univ}; {n_sec} con "
      "cuentas utilizables en la SEC). En cada fecha se separan por tamaño: **grandes** = las 1.000 mayores (frente al S&P 500) "
      "y **pequeñas** = el resto con más de 300 M$ (frente al Russell 2000). **Sesgo de supervivencia:** faltan las que quebraron "
      "o desaparecieron; por eso se compara también con «cualquier empresa del mismo grupo a partes iguales».\n")
    A("**Regla de decisión (fijada antes de ver los resultados):** una variante vale en un grupo solo si a 12 y a 24 meses su "
      "rentabilidad media supera al índice **y** a la media del grupo. Aprobar no demuestra nada; suspender es una señal clara.\n")
    if not res.n_grupo.empty:
        ng = res.n_grupo.groupby("grupo").median(numeric_only=True)
        A("Empresas por fecha (mediana): " + " · ".join(
            f"{NOMBRE_GRUPO[g]}: {ng.loc[g, 'n']:.0f}, pasan Lynch {ng.loc[g, 'n_lynch']:.0f}, Lynch + cribado {ng.loc[g, 'n_lynch_cribado']:.0f}"
            for g in GRUPOS if g in ng.index) + "\n")

    decs = {(v, g): decidir(res, v, g) for v in VARIANTES for g in GRUPOS}
    A("## Veredicto\n")
    A("| Variante | Grupo | Veredicto | Por qué |")
    A("|---|---|---|---|")
    for (v, g), d in decs.items():
        et = {"mantener": "✅ aprueba (en prueba)", "quitar": "❌ suspende", "sin_datos": "❔ sin datos suficientes"}[d["veredicto"]]
        A(f"| {NOMBRE_VAR[v]} | {NOMBRE_GRUPO[g]} | {et} | "
          f"{'supera al índice y al grupo a 12 y 24 meses' if d['veredicto'] == 'mantener' else '; '.join(d['motivos'])} |")
    A("")
    A("## ¿Cuántas veces acierta? (lo que decía Lynch)\n")
    A("«Si eres bueno, aciertas seis veces de cada diez» — y unas pocas muy buenas compensan las que fallan. Aquí, empresa a empresa: "
      "% de compras que baten a su índice, cuánto le sacan las que aciertan y cuánto pierden frente a él las que fallan. "
      "La columna «sin criterio» es lo mismo para cualquier empresa del grupo (la base con la que comparar).\n")
    A("| Variante | Grupo | Horizonte | Compras | Aciertan | Sin criterio | Ganan (media) | Pierden (media) | Ganancia/pérdida | Sin criterio |")
    A("|---|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for v in VARIANTES:
        for g in GRUPOS:
            for h in HORIZONTES:
                s = estadisticas(res, v, g, h)
                if not s or s.get("insuficiente"):
                    continue
                A(f"| {NOMBRE_VAR[v]} | {NOMBRE_GRUPO[g]} | {h} m | {s['n_compras']} | {_p(s['acierto'], 0)} | {_p(s['acierto_base'], 0)} | "
                  f"{_pp(s['gana_media'])} | {_pp(s['pierde_media'])} | {_r(s['ratio'])} | {_r(s['ratio_base'])} |")
    A("")
    for v in VARIANTES:
        for g in GRUPOS:
            d = decs[(v, g)]
            A(f"## {NOMBRE_VAR[v]} · {NOMBRE_GRUPO[g]}\n")
            A(f"| Horizonte | Fechas | Nº medio | Media elegidas | {NOMBRE_BENCH[g]} | Grupo (iguales) | Exceso vs índice [IC 90 %] | Exceso vs grupo [IC 90 %] | Meses que ganan al índice |")
            A("|---|---:|---:|---:|---:|---:|---|---|---:|")
            for h in HORIZONTES:
                s = estadisticas(res, v, g, h)
                if not s or s.get("insuficiente"):
                    A(f"| {h} m | {(s or {}).get('n_fechas', 0)} | | pocas fechas | | | | | |")
                    continue
                A(f"| {h} m | {s['n_fechas']} | {s['n_medio']:.1f} | {_p(s['media'])} | {_p(s['bench'])} | {_p(s['todas'])} | "
                  f"{_pp(s['ex_bench'])} {_ic(s['ic_bench'])} | {_pp(s['ex_todas'])} {_ic(s['ic_todas'])} | {_p(s['meses_gana'], 0)} |")
            s36 = estadisticas(res, v, g, 36)
            if s36 and not s36.get("insuficiente"):
                A(f"\nA 3 años: el {_p(s36.get('dobla'), 0)} de las compras **duplicó** su valor (sin criterio: {_p(s36.get('dobla_base'), 0)}) "
                  f"y el {_p(s36.get('hunde'), 0)} perdió más de la mitad (sin criterio: {_p(s36.get('hunde_base'), 0)}).")
            ep = por_epoca(res, v, g)
            if ep:
                A("\n**Por época** (a 12 meses):\n")
                A(f"| Época | Fechas | Elegidas | {NOMBRE_BENCH[g]} | Grupo |")
                A("|---|---:|---:|---:|---:|")
                for e in ep:
                    A(f"| {e['epoca']} | {e['n']} | {_p(e['media'])} | {_p(e['bench'])} | {_p(e['todas'])} |")
            c = concentracion(res, v, g)
            if c:
                A(f"\n**¿Depende de una empresa?** Más aportan a 12 m: " + ", ".join(f"{t} ({x * 100:+.1f} pp)" for t, x in c["top"])
                  + f". Sin {c['mejor']}, el exceso medio sobre el índice queda en {_pp(c['ex_sin_mejor'])} ({c['n_empresas']} empresas distintas).")
            txt = {"mantener": "aprueba (en prueba)", "quitar": "suspende", "sin_datos": "sin datos suficientes"}[d["veredicto"]]
            A(f"\n**Decisión:** {txt}." + ("" if d["veredicto"] == "mantener" else " " + "; ".join(d["motivos"]) + ".") + "\n")
    A("---\n")
    A("**Cómo leerlo.** Lo que se prueba son las reglas numéricas de Lynch; su ventaja real venía también de visitar empresas, "
      "hablar con directivos y conocer los productos, que no se puede reconstruir. Faltan las empresas que quebraron (sesgo a favor).")
    return "\n".join(L)


def decisiones_json(res: Resultado) -> dict:
    out = {}
    for v in VARIANTES:
        for g in GRUPOS:
            d = decidir(res, v, g)
            out[f"{v}:{g}"] = {"veredicto": d["veredicto"], "motivos": d["motivos"]}
    return out


# --------------------------------------------------------------------------- #
# Ejecución con datos reales
# --------------------------------------------------------------------------- #

def universo(ruta: Path = RUTA_MERCADOS) -> list[str]:
    js = json.loads(ruta.read_text(encoding="utf-8"))
    out: set[str] = set()
    for cod in ("sp500", "sp400", "sp600", "r2000", "ndx"):
        out.update((js["mercados"].get(cod) or {}).get("tickers") or [])
    return sorted(t for t in out if t.replace("-", "").isalpha())


def sectores_gics() -> dict[str, str]:
    """Sector de cada empresa: S&P 500 (CSV), S&P 400/600 (Wikipedia) y Russell 2000 (iShares)."""
    from .dividendos import URL_SP400, URL_SP600, _lista_wikipedia, _norm, _tabla_ishares, _ticker_valido
    from .precios import universo_sp500_con_sector
    sec: dict[str, str] = {}
    try:
        sec.update(universo_sp500_con_sector()[1])
    except Exception as e:  # noqa: BLE001
        print(f"Aviso: sin sectores del S&P 500 ({type(e).__name__})")
    for url, m in ((URL_SP400, 300), (URL_SP600, 400)):
        try:
            for f in _lista_wikipedia(url, m):
                if f["sector"]:
                    sec.setdefault(f["ticker"], f["sector"])
        except Exception as e:  # noqa: BLE001
            print(f"Aviso: sin sectores de {url[-30:]} ({type(e).__name__})")
    try:
        df = _tabla_ishares()
        for _, row in df.iterrows():
            t = _norm(row.get("Ticker"))
            if _ticker_valido(t) and str(row.get("Sector") or "").strip():
                sec.setdefault(t, str(row.get("Sector")).strip())
    except Exception as e:  # noqa: BLE001
        print(f"Aviso: sin sectores del Russell 2000 ({type(e).__name__})")
    return sec


def main() -> None:
    ap = argparse.ArgumentParser(description="Prueba histórica point-in-time del filtro Lynch.")
    ap.add_argument("--salida", default="resultados_lynch")
    ap.add_argument("--cache", default=".cache_pit")
    ap.add_argument("--desde-precios", default="2008-01-01")
    ap.add_argument("--inicio", default="2015-01-01")
    ap.add_argument("--max-empresas", type=int, default=0)
    args = ap.parse_args()

    from .dividendos import descargar_dividendos

    tickers = universo()
    if args.max_empresas:
        tickers = tickers[:args.max_empresas]
    print(f"Universo: {len(tickers)} empresas (S&P 500, 400, 600, Russell 2000 y Nasdaq-100 de hoy).")
    sectores = sectores_gics()
    print(f"Sectores conocidos: {sum(1 for t in tickers if sectores.get(t))}/{len(tickers)}")
    pit = FuentePIT.descargar(tickers, Path(args.cache))
    print(f"Cuentas de la SEC utilizables: {len(pit.datos)}/{len(tickers)}")
    if len(pit.datos) < 0.6 * len(tickers):
        raise RuntimeError("La SEC ha devuelto cuentas de menos del 60 % de las empresas: resultado poco fiable.")
    usables = sorted(pit.datos)
    print("Descargando precios (cierre y cierre ajustado)...")
    datos = descargar_dividendos(sorted(set(usables) | set(BENCH.values())), args.desde_precios)
    for b in BENCH.values():
        if b not in datos:
            raise RuntimeError(f"No se pudo descargar {b}: Yahoo está bloqueando o caído.")
    cierre = pd.concat({t: d["close"] for t, d in datos.items()}, axis=1).sort_index()
    ajust = pd.concat({t: d["adj"] for t, d in datos.items()}, axis=1).sort_index()
    cierre, ajust = cierre.where(cierre > 0), ajust.where(ajust > 0)
    ajust = ajust.ffill(limit=5)
    cierre = cierre.reindex(index=ajust.index, columns=ajust.columns).ffill(limit=5)
    cov = len(set(ajust.columns) & set(usables)) / max(1, len(usables))
    print(f"Precios: {len(ajust.columns)} columnas, cobertura {cov:.0%}, {ajust.index[0]:%Y-%m-%d} → {ajust.index[-1]:%Y-%m-%d}")
    if cov < 0.8:
        raise RuntimeError(f"Solo hay precios del {cov:.0%} de las empresas: resultado poco fiable.")

    acciones = [c for c in ajust.columns if c not in BENCH.values() and c in pit.datos]
    print("Reconstruyendo, mes a mes, lo que habría elegido el filtro Lynch...")
    res = calcular(ajust[acciones + [b for b in BENCH.values()]], pit, sectores, inicio=args.inicio, crudo=cierre[acciones])
    md = informe_md(res, args.inicio, len(tickers), len(pit.datos))
    out = Path(args.salida)
    out.mkdir(parents=True, exist_ok=True)
    (out / "informe.md").write_text(md, encoding="utf-8")
    res.picks.to_csv(out / "elegidas.csv", index=False)
    res.aptas.to_csv(out / "aptas.csv.gz", index=False, compression="gzip")
    res.ref.to_csv(out / "referencias.csv", index=False)
    (out / "decisiones.json").write_text(json.dumps(decisiones_json(res), ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + md)


if __name__ == "__main__":
    warnings.filterwarnings("ignore")
    main()
