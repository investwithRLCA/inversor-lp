"""
Estudio de eventos — «¿qué pasa DESPUÉS de que una acción cae a un valle?»

Pregunta de partida: en el S&P 500 y el Nasdaq-100, cuando una acción se hunde,
¿sigue cayendo o acaba subiendo? Y si la media de lo que sube después supera al
índice, ¿quiere decir que merece la pena comprar valles?

Esa última frase es el punto de partida correcto pero NO basta. Este estudio
mide, además de la media, las cuatro cosas que pueden hacer que una media
bonita no signifique nada:

  1. PÁNICOS DE MERCADO. Tras marzo de 2020 o 2022 casi todo rebotó, también las
     acciones que no se habían hundido. Eso no es mérito de comprar valles. Por
     eso cada rentabilidad se compara con (a) el índice, (b) el resto del
     universo en las mismas fechas (todas las empresas a partes iguales) y
     (c) se separan los momentos en que el propio mercado estaba hundido de
     los momentos de mercado normal.
  2. SUPERVIVENCIA. Solo existen en el listado las empresas que HOY siguen en el
     índice. Las que se hundieron y quebraron (o salieron del índice) no
     aparecen, así que las caídas «que no se recuperaron» están infrarrepresentadas
     justo en este estudio. Se calcula cuántas caídas ausentes que lo perdieran
     todo bastarían para borrar la ventaja.
  3. CONCENTRACIÓN. Una media puede estar tirada por dos o tres casos enormes:
     se da también la mediana, la media recortada y el % de casos que ganan.
  4. SOLAPE. Un mismo mes aporta decenas de eventos que no son independientes
     (todas cayeron por lo mismo). Se calcula también el promedio MES a MES y un
     intervalo de confianza remuestreando meses, no eventos.

Reglas para no engañarse con las definiciones: se usan TRES definiciones de
«valle» (no la que mejor salga), se mira a fin de mes (como el módulo mensual),
y una empresa cuenta una sola vez cada 12 meses.

Todo son funciones puras sobre una tabla de precios: se prueban sin red.
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .estrategia import Params, senales

log = logging.getLogger(__name__)

HORIZONTES = (3, 6, 12, 24, 36)           # meses
COOLDOWN = 12                              # una empresa cuenta una vez cada 12 meses
ESTRES_MERCADO = -0.15                     # índice ≥15 % bajo su máximo de 12 meses = mercado en pánico
EPOCAS = (("2005-2012", "2005-01-01", "2013-01-01"), ("2013-2019", "2013-01-01", "2020-01-01"),
          ("2020-2022", "2020-01-01", "2023-01-01"), ("2023 en adelante", "2023-01-01", "2100-01-01"))
DEFINICIONES = {
    "caida30": "Cae un 30 % o más desde su máximo de 12 meses",
    "caida50": "Cae un 50 % o más desde su máximo de 12 meses",
    "valle": "«Valle» del módulo mensual: 15-60 % bajo su mediana de 5 años, en el 20 % inferior de su "
             "rango y rebotando ≥ 3 % desde su mínimo del último mes",
}


# --------------------------------------------------------------------------- #
# Núcleo
# --------------------------------------------------------------------------- #

@dataclass
class Estudio:
    eventos: pd.DataFrame                       # un evento por fila
    base: dict = field(default_factory=dict)    # {h: DataFrame(fecha → universo, indice)} sin condicionar
    n_empresas: int = 0
    desde: str = ""
    hasta: str = ""
    bench: str = ""


def _fines_de_mes(idx: pd.DatetimeIndex) -> np.ndarray:
    """Posiciones de la última sesión de cada mes."""
    m = idx.year * 100 + idx.month
    return np.flatnonzero(np.r_[m[1:] != m[:-1], True])


def estudiar(precios: pd.DataFrame, bench: str, p: Params | None = None,
             inicio_eventos: str = "2005-01-01", horizontes=HORIZONTES,
             definiciones=None) -> Estudio:
    """`precios`: cierres diarios ajustados, una columna por empresa más la del
    índice `bench`. Devuelve los eventos de cada definición con sus rentabilidades
    futuras y todo lo necesario para compararlas."""
    p = p or Params()
    definiciones = tuple(definiciones or DEFINICIONES)
    if bench not in precios.columns:
        raise ValueError(f"Falta la columna del índice {bench!r}.")
    acciones = precios.drop(columns=[c for c in ("SPY", "QQQ", bench) if c in precios.columns])
    idx = precios.index
    me = _fines_de_mes(idx)
    K, N = len(me), acciones.shape[1]
    A = acciones.to_numpy(dtype=float)
    P = A[me]                                           # precios a fin de mes (K × N)
    Bd = precios[bench].to_numpy(dtype=float)
    B = Bd[me]

    H252 = acciones.rolling(252, min_periods=200).max().to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        dd = P / H252[me] - 1.0                         # caída desde el máximo de 12 meses
    Hb = pd.Series(Bd, index=idx).rolling(252, min_periods=200).max().to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        dd_b = B / Hb[me] - 1.0
    estres = dd_b <= ESTRES_MERCADO                     # el índice estaba hundido ese mes

    # --- condiciones de «valle» a fin de cada mes --------------------------------
    cond = {d: np.zeros((K, N), dtype=bool) for d in definiciones}
    with np.errstate(invalid="ignore"):
        if "caida30" in cond:
            cond["caida30"] = dd <= -0.30
        if "caida50" in cond:
            cond["caida50"] = dd <= -0.50
    if "valle" in cond:
        for k in range(K):
            s = senales(acciones, idx[me[k]], p)
            with np.errstate(invalid="ignore"):
                cond["valle"][k] = ((s["descuento"].to_numpy() >= p.min_descuento)
                                    & (s["descuento"].to_numpy() <= p.max_descuento)
                                    & (s["percentil"].to_numpy() <= p.max_percentil)
                                    & (s["rebote"].to_numpy() >= p.rebote_min))
    validos = np.isfinite(P)
    inicio = pd.Timestamp(inicio_eventos)

    # --- rentabilidades futuras a fin de mes (sin condicionar a nada) ---------------
    fwd, fwd_i, base = {}, {}, {}
    for h in horizontes:
        f = np.full((K, N), np.nan)
        fi = np.full(K, np.nan)
        if K > h:
            with np.errstate(invalid="ignore", divide="ignore"):
                f[:K - h] = P[h:] / P[:K - h] - 1.0
                fi[:K - h] = B[h:] / B[:K - h] - 1.0
        fwd[h], fwd_i[h] = f, fi
        hay = np.isfinite(f)
        n_ok = hay.sum(axis=1)
        uni = np.where(n_ok >= 20, np.nansum(np.where(hay, f, 0.0), axis=1) / np.maximum(n_ok, 1), np.nan)
        base[h] = pd.DataFrame({"universo": uni, "indice": fi, "estres": estres},
                               index=idx[me]).loc[inicio:]

    # --- eventos ---------------------------------------------------------------------
    filas = []
    cols = list(acciones.columns)
    for d in definiciones:
        ultimo = np.full(N, -10_000)
        for k in range(K):
            nuevo = cond[d][k] & validos[k] & ((k - ultimo) >= COOLDOWN)
            if idx[me[k]] < inicio:
                ultimo[nuevo] = k                       # se registran, pero no cuentan antes del inicio
                continue
            for j in np.flatnonzero(nuevo):
                ultimo[j] = k
                i = me[k]
                fila = {"def": d, "ticker": cols[j], "fecha": idx[i], "k": k, "precio": float(P[k, j]),
                        "caida": float(dd[k, j]) if np.isfinite(dd[k, j]) else np.nan,
                        "estres": bool(estres[k])}
                for h in horizontes:
                    r = fwd[h][k, j]
                    fila[f"r{h}"] = r
                    fila[f"i{h}"] = fwd_i[h][k]
                    fila[f"u{h}"] = base[h]["universo"].get(idx[i], np.nan)
                # ¿siguió cayendo? mínimo de los 252 días siguientes frente al precio del evento
                if i + 252 < len(idx):
                    v = A[i + 1:i + 253, j]
                    fila["caida_adicional_12m"] = float(np.nanmin(v) / P[k, j] - 1.0) if np.isfinite(v).any() else np.nan
                else:
                    fila["caida_adicional_12m"] = np.nan
                # ¿recuperó el nivel que tenía antes de caer (su máximo de 12 meses) en 3 años?
                if i + 756 < len(idx) and np.isfinite(H252[i, j]):
                    v = A[i + 1:i + 757, j]
                    fila["recupera_36m"] = bool(np.nanmax(v) >= H252[i, j]) if np.isfinite(v).any() else np.nan
                else:
                    fila["recupera_36m"] = np.nan
                filas.append(fila)
    ev = pd.DataFrame(filas)
    return Estudio(eventos=ev, base=base, n_empresas=N, desde=f"{idx[0]:%Y-%m-%d}",
                   hasta=f"{idx[-1]:%Y-%m-%d}", bench=bench)


# --------------------------------------------------------------------------- #
# Estadísticas
# --------------------------------------------------------------------------- #

def _recortada(x: np.ndarray, q: float = 0.05) -> float:
    x = np.sort(x[np.isfinite(x)])
    if len(x) < 20:
        return float(np.mean(x)) if len(x) else np.nan
    a = int(len(x) * q)
    return float(np.mean(x[a:len(x) - a]))


def _ic_meses(ex: pd.Series, fechas: pd.Series, n_boot: int = 2000, seed: int = 0):
    """Intervalo al 90 % de la media del exceso remuestreando MESES (no eventos),
    con la media de cada mes como unidad: así el solape de un mismo pánico no
    cuenta decenas de veces."""
    por_mes = ex.groupby(fechas).mean().to_numpy()
    if len(por_mes) < 6:
        return (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    medias = rng.choice(por_mes, size=(n_boot, len(por_mes)), replace=True).mean(axis=1)
    return (float(np.percentile(medias, 5)), float(np.percentile(medias, 95)))


def estadisticas(ev: pd.DataFrame, h: int) -> dict | None:
    """Resumen de los eventos de una definición a `h` meses."""
    c = f"r{h}"
    d = ev.dropna(subset=[c, f"i{h}", f"u{h}"])
    if d.empty:
        return None
    r, i, u = d[c].to_numpy(), d[f"i{h}"].to_numpy(), d[f"u{h}"].to_numpy()
    ex_i, ex_u = r - i, r - u
    por_mes_i = pd.Series(ex_i, index=d["fecha"].to_numpy()).groupby(level=0).mean()
    por_mes_u = pd.Series(ex_u, index=d["fecha"].to_numpy()).groupby(level=0).mean()
    m, ri = float(np.mean(r)), float(np.mean(i))
    out = {
        "n": len(d), "n_empresas": int(d["ticker"].nunique()), "n_meses": int(d["fecha"].nunique()),
        "media": m, "mediana": float(np.median(r)), "recortada": _recortada(r),
        "indice": ri, "universo": float(np.mean(u)),
        "exceso_indice": float(np.mean(ex_i)), "exceso_indice_mediana": float(np.median(ex_i)),
        "exceso_universo": float(np.mean(ex_u)), "exceso_universo_mediana": float(np.median(ex_u)),
        "supera_indice": float(np.mean(ex_i > 0)), "supera_universo": float(np.mean(ex_u > 0)),
        "pierde": float(np.mean(r < 0)), "p10": float(np.percentile(r, 10)), "p90": float(np.percentile(r, 90)),
        "exceso_mes_indice": float(por_mes_i.mean()), "exceso_mes_universo": float(por_mes_u.mean()),
        "ic_universo": _ic_meses(pd.Series(ex_u, index=d.index), d["fecha"]),
        "ic_indice": _ic_meses(pd.Series(ex_i, index=d.index), d["fecha"]),
        # fracción de caídas AUSENTES (quebradas, -100 %) que haría falta para que la media cayera al nivel del índice
        "supervivencia_ruptura": max(0.0, (m - ri) / (1.0 + m)) if m > ri else 0.0,
    }
    for nombre, filtro in (("estres", d["estres"].to_numpy()), ("normal", ~d["estres"].to_numpy())):
        if filtro.sum() >= 5:
            out[f"n_{nombre}"] = int(filtro.sum())
            out[f"exceso_indice_{nombre}"] = float(np.mean(ex_i[filtro]))
            out[f"exceso_universo_{nombre}"] = float(np.mean(ex_u[filtro]))
            out[f"media_{nombre}"] = float(np.mean(r[filtro]))
            out[f"indice_{nombre}"] = float(np.mean(i[filtro]))
        else:
            out[f"n_{nombre}"] = int(filtro.sum())
    # concentración: cuánto pesan los 3 mejores casos
    top = np.sort(r)[::-1][:3]
    out["top3"] = [float(x) for x in top]
    out["media_sin_top1pct"] = float(np.mean(np.sort(r)[:max(1, int(len(r) * 0.99))]))
    return out


def seguimiento_caidas(ev: pd.DataFrame) -> dict:
    """¿Siguió cayendo antes de subir? Y ¿recuperó lo perdido?"""
    a = ev["caida_adicional_12m"].dropna().to_numpy()
    rec = ev["recupera_36m"].dropna().astype(bool).to_numpy()
    out = {"n_12m": len(a), "n_36m": len(rec)}
    if len(a):
        out.update({"mediana_caida_adicional": float(np.median(a)),
                    "sigue_cayendo_10": float(np.mean(a <= -0.10)),
                    "sigue_cayendo_20": float(np.mean(a <= -0.20)),
                    "sigue_cayendo_30": float(np.mean(a <= -0.30))})
    if len(rec):
        out["recupera_36m"] = float(np.mean(rec))
    return out


def base_incondicional(estudio: Estudio, h: int) -> dict:
    """Sin condicionar a nada: lo que rindió de media cualquier empresa del listado a `h` meses,
    frente al índice. Es el sesgo «de fábrica» del listado (supervivencia + igual ponderación)."""
    b = estudio.base[h].dropna(subset=["universo", "indice"])
    if b.empty:
        return {}
    return {"universo": float(b["universo"].mean()), "indice": float(b["indice"].mean()),
            "n_meses": len(b)}


# --------------------------------------------------------------------------- #
# Informe
# --------------------------------------------------------------------------- #

def _p(x, dec=1, signo=False):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "—"
    return f"{x * 100:+.{dec}f} %" if signo else f"{x * 100:.{dec}f} %"


def _pp(x, dec=1):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "—"
    return f"{x * 100:+.{dec}f} pp"


def informe_md(estudios: dict[str, Estudio], nombres_indice: dict[str, str]) -> str:
    L: list[str] = []
    A = L.append
    A("# ¿Qué pasa después de que una acción cae a un valle?\n")
    A("Estudio de eventos sobre los **componentes actuales** de cada índice, con precios ajustados por "
      "dividendos y splits (Yahoo). Se mira a fin de mes; cada empresa cuenta como mucho una vez cada 12 meses. "
      "Rentabilidades **brutas** (sin impuestos ni comisiones) y sin reinvertir lo que se vende: comprar y mantener.\n")
    A("**Cómo leer las comparaciones:** «índice» es el propio índice en las mismas fechas; «universo» es "
      "el resto de empresas del listado, a partes iguales, en las mismas fechas (controla que casi todo "
      "rebota tras un pánico). *Exceso* = rentabilidad del evento menos la de la referencia, en puntos "
      "porcentuales (pp).\n")
    A("**Definiciones de «valle»:**\n")
    for k, v in DEFINICIONES.items():
        A(f"- `{k}`: {v}")
    A("")

    for clave, est in estudios.items():
        nombre = nombres_indice.get(clave, clave)
        ev = est.eventos
        A(f"\n---\n\n## {nombre}\n")
        A(f"{est.n_empresas} empresas (componentes actuales), precios del {est.desde} al {est.hasta}. "
          f"Referencia: {est.bench}.\n")
        if ev.empty:
            A("_Sin eventos._\n")
            continue
        # 1. sesgo de fábrica
        A("### 0. El sesgo «de fábrica» del listado\n")
        A("Sin condicionar a ningún valle: rentabilidad media de **cualquier** empresa del listado frente al índice "
          "(todos los meses). Si el listado ya gana al índice por sí solo, hay que descontarlo.\n")
        A("| Horizonte | Empresa media del listado | Índice | Diferencia |")
        A("|---|---:|---:|---:|")
        for h in HORIZONTES:
            b = base_incondicional(est, h)
            if b:
                A(f"| {h} meses | {_p(b['universo'])} | {_p(b['indice'])} | {_pp(b['universo'] - b['indice'])} |")
        A("")

        for d, desc in DEFINICIONES.items():
            e = ev[ev["def"] == d]
            A(f"### Definición `{d}` — {desc}\n")
            if e.empty:
                A("_Sin eventos._\n")
                continue
            A(f"{len(e)} eventos de {e['ticker'].nunique()} empresas en {e['fecha'].nunique()} meses distintos.\n")
            A("| Tras… | N | Media | Mediana | Índice | Universo | Exceso vs índice (media / mediana) | Exceso vs universo | Superan al índice | Pierden dinero | Peor 10 % |")
            A("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
            S = {h: estadisticas(e, h) for h in HORIZONTES}
            for h in HORIZONTES:
                s = S[h]
                if not s:
                    continue
                A(f"| {h} m | {s['n']} | {_p(s['media'])} | {_p(s['mediana'])} | {_p(s['indice'])} | {_p(s['universo'])} | "
                  f"{_pp(s['exceso_indice'])} / {_pp(s['exceso_indice_mediana'])} | {_pp(s['exceso_universo'])} | "
                  f"{_p(s['supera_indice'], 0)} | {_p(s['pierde'], 0)} | {_p(s['p10'])} |")
            A("")
            A("**Quitando los pánicos de mercado** (índice ≥15 % bajo su máximo de 12 meses) y ponderando "
              "**mes a mes** (para que un solo pánico no cuente decenas de veces):\n")
            A("| Tras… | Mercado normal: N · exceso vs índice · vs universo | Mercado en pánico: N · exceso vs índice · vs universo | Mes a mes: exceso vs índice · vs universo | IC 90 % del exceso vs universo (meses) |")
            A("|---|---|---|---|---|")
            for h in (6, 12, 24, 36):
                s = S[h]
                if not s:
                    continue
                nor = (f"{s['n_normal']} · {_pp(s.get('exceso_indice_normal'))} · {_pp(s.get('exceso_universo_normal'))}"
                       if s.get("exceso_indice_normal") is not None else f"{s['n_normal']} (pocos)")
                est_ = (f"{s['n_estres']} · {_pp(s.get('exceso_indice_estres'))} · {_pp(s.get('exceso_universo_estres'))}"
                        if s.get("exceso_indice_estres") is not None else f"{s['n_estres']} (pocos)")
                lo, hi = s["ic_universo"]
                A(f"| {h} m | {nor} | {est_} | {_pp(s['exceso_mes_indice'])} · {_pp(s['exceso_mes_universo'])} | "
                  f"[{_pp(lo)}, {_pp(hi)}] |")
            A("")
            A("**Por época** (a 12 meses; con pocos casos o un solo pánico, la cifra no es fiable):\n")
            A("| Época | N | Media | Índice | Exceso vs índice | Exceso vs universo |")
            A("|---|---:|---:|---:|---:|---:|")
            for nom, a_, b_ in EPOCAS:
                x = e[(e["fecha"] >= a_) & (e["fecha"] < b_)]
                sx = estadisticas(x, 12) if len(x) else None
                if sx:
                    A(f"| {nom} | {sx['n']} | {_p(sx['media'])} | {_p(sx['indice'])} | "
                      f"{_pp(sx['exceso_indice'])} | {_pp(sx['exceso_universo'])} |")
            A("")
            A("**¿Una media tirada por pocos casos?** (a 12 meses)\n")
            s = S[12]
            if s:
                A(f"- Media {_p(s['media'])}; mediana {_p(s['mediana'])}; media recortada (sin el 5 % mejor y peor) "
                  f"{_p(s['recortada'])}; media sin el 1 % mejor {_p(s['media_sin_top1pct'])}.")
                A(f"- Los 3 mejores casos: {', '.join(_p(x, 0, True) for x in s['top3'])}.")
            sc = seguimiento_caidas(e)
            if sc.get("n_12m"):
                A(f"\n**¿Siguió cayendo antes de subir?** En los 12 meses siguientes, el precio llegó a estar de media "
                  f"un {abs(sc['mediana_caida_adicional']) * 100:.0f} % por debajo del de compra (mediana); el "
                  f"{sc['sigue_cayendo_10'] * 100:.0f} % cayó otro 10 % o más, el {sc['sigue_cayendo_20'] * 100:.0f} % otro 20 % o más "
                  f"y el {sc['sigue_cayendo_30'] * 100:.0f} % otro 30 % o más.")
            if sc.get("n_36m"):
                A(f"A 3 años, el {sc['recupera_36m'] * 100:.0f} % había vuelto al nivel que tenía antes de caer ({sc['n_36m']} casos con 3 años de recorrido).")
            A("")
            A("**Trampa de supervivencia.** Las empresas que cayeron y desaparecieron no están en este listado. "
              "Si hubiera casos ausentes que lo perdieron todo (−100 %), ¿cuántos bastarían para borrar la ventaja sobre el índice?\n")
            fila = []
            for h in (12, 24, 36):
                s = S[h]
                if s:
                    fila.append(f"a {h} m: bastaría con que de cada 100 caídas reales, {s['supervivencia_ruptura'] * 100:.0f} "
                                f"hubieran acabado en cero y no estén en el listado (media {_p(s['media'])} vs índice {_p(s['indice'])})")
            A("- " + "; ".join(fila) + ".\n")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# Universos
# --------------------------------------------------------------------------- #

def universo_nasdaq100() -> list[str]:
    """Componentes actuales del Nasdaq-100 (formato Yahoo). Prueba Wikipedia y, si
    no, la API pública de Nasdaq. Devuelve [] si no puede (el estudio sigue sin él)."""
    import requests
    ua = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) estudio-eventos/1.0"}
    try:
        r = requests.get("https://en.wikipedia.org/wiki/Nasdaq-100", headers=ua, timeout=30)
        r.raise_for_status()
        for t in pd.read_html(io.StringIO(r.text)):
            col = next((c for c in t.columns if str(c).strip().lower() in ("ticker", "symbol")), None)
            if col is not None and 80 <= len(t) <= 130:
                out = sorted({str(x).strip().upper().replace(".", "-") for x in t[col] if isinstance(x, str) and x.strip()})
                if len(out) >= 80:
                    print(f"Nasdaq-100: {len(out)} tickers (Wikipedia).")
                    return out
    except Exception as e:  # noqa: BLE001
        print(f"Aviso: Wikipedia no disponible para el Nasdaq-100 ({e}).")
    try:
        r = requests.get("https://api.nasdaq.com/api/quote/list-type/nasdaq100", headers=ua, timeout=30)
        r.raise_for_status()
        filas = r.json()["data"]["data"]["rows"]
        out = sorted({str(x["symbol"]).strip().upper().replace(".", "-") for x in filas})
        if len(out) >= 80:
            print(f"Nasdaq-100: {len(out)} tickers (API de Nasdaq).")
            return out
    except Exception as e:  # noqa: BLE001
        print(f"Aviso: API de Nasdaq no disponible para el Nasdaq-100 ({e}).")
    return []


def _descargar(tickers: list[str], benches: list[str], desde: str) -> pd.DataFrame:
    from .precios import descargar_precios, limpiar
    ys = sorted(set(tickers) | set(benches))
    print(f"Descargando {len(ys)} tickers desde {desde}...")
    bruto = descargar_precios(ys, desde)
    faltan = [b for b in benches if bruto.empty or b not in bruto.columns]
    if faltan:
        raise RuntimeError(f"No se pudo descargar {', '.join(faltan)}: Yahoo está bloqueando. Reintenta más tarde.")
    return limpiar(bruto)


def main() -> None:
    ap = argparse.ArgumentParser(description="¿Qué pasa tras un valle? Estudio de eventos S&P 500 / Nasdaq-100.")
    ap.add_argument("--desde", default="2000-01-01", help="inicio de la descarga de precios")
    ap.add_argument("--inicio-eventos", default="2005-01-01", help="primer mes en que se cuentan eventos")
    ap.add_argument("--salida", default="resultados_eventos", help="carpeta de salida")
    ap.add_argument("--sin-nasdaq", action="store_true")
    ap.add_argument("--max-empresas", type=int, default=0, help="solo para pruebas rápidas")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    from pathlib import Path
    from .precios import universo_sp500
    Path(args.salida).mkdir(parents=True, exist_ok=True)

    estudios: dict[str, Estudio] = {}
    nombres = {"sp500": "S&P 500 (componentes actuales)", "ndx": "Nasdaq-100 (componentes actuales)"}

    sp = universo_sp500()
    ndx = [] if args.sin_nasdaq else universo_nasdaq100()
    if args.max_empresas:
        sp, ndx = sp[:args.max_empresas], ndx[:args.max_empresas]
    precios = _descargar(sorted(set(sp) | set(ndx)), ["SPY", "QQQ"], args.desde)

    for clave, lista, bench in (("sp500", sp, "SPY"), ("ndx", ndx, "QQQ")):
        if not lista:
            print(f"Aviso: sin lista para {clave}; se omite.")
            continue
        cols = [c for c in lista if c in precios.columns]
        cobertura = len(cols) / len(lista)
        if cobertura < 0.8:
            print(f"Aviso: {clave}: solo se descargó el {cobertura:.0%} de los tickers; se omite por poco fiable.")
            continue
        print(f"{clave}: {len(cols)} de {len(lista)} empresas con datos; estudiando...")
        estudios[clave] = estudiar(precios[cols + [bench]], bench, inicio_eventos=args.inicio_eventos)
    if not estudios:
        raise RuntimeError("Ningún índice tuvo datos suficientes.")

    md = informe_md(estudios, nombres)
    Path(args.salida, "informe.md").write_text(md, encoding="utf-8")
    for k, e in estudios.items():
        e.eventos.to_csv(Path(args.salida, f"eventos_{k}.csv"), index=False)
    print("\n" + md)


if __name__ == "__main__":
    main()
