"""
Backtest del módulo mensual: ¿habría compensado esta estrategia frente a la
alternativa aburrida de aportar lo mismo cada mes a un ETF del S&P 500?

    python -m mensual.backtest                         # valores por defecto
    python -m mensual.backtest --inicio 2014-01-01 --aportacion 500 --montecarlo 200

Qué hace, mes a mes (último día hábil de cada mes):
  1. Con los precios hasta el día ANTERIOR, calcula qué empresas están
     baratas frente a sí mismas (estrategia.py).
  2. Vende lo que esté claramente caro; con ese dinero + la aportación + el
     efectivo que estuviera esperando compra las mejores candidatas (máx. N,
     máx. 2 por sector, con tope por empresa).
  3. MODELO REAL: solo acciones. Lo que no se pueda colocar (no hay candidatas,
     o topes) se queda en efectivo, rindiendo un tipo bajo, esperando la
     siguiente oportunidad. No hay ETF de relleno: tú pones el dinero a mano y
     solo en lo que la herramienta te diga.
  4. Ejecuta al cierre de ese día: no hay "mirar el futuro".

Y lo compara con TRES referencias, porque un solo número engaña:
  A. Aportar cada mes SOLO al S&P 500 (la alternativa real y aburrida).
  B. CONTROL de supervivencia: aportar cada mes a partes iguales a TODAS las
     empresas del universo. Si la estrategia no bate a esto, su "ventaja" es
     solo que el universo está formado por las ganadoras de hoy.
  C. CONTROL de azar: la misma estrategia (mismos meses, mismo número de
     compras, mismas ventas) pero eligiendo las empresas AL AZAR, muchas veces.
     Si la estrategia no queda claramente por encima del azar, la señal de
     "barata frente a sí misma" no aporta nada.

Además, como DIAGNÓSTICO (no entra en el veredicto), repite la estrategia con el
efectivo parado invertido en el S&P 500: separa «¿elige bien las acciones?» de
«¿cuánto cuesta esperar en efectivo?».

El veredicto usa criterios FIJADOS DE ANTEMANO sobre una variante elegida
antes de ver resultados (venta='cara', con espera). Las demás variantes se
muestran como sensibilidad, no para escoger la mejor: con 8 variantes, la
que sale primera lo suele ser por casualidad.
"""

from __future__ import annotations

import argparse
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .estrategia import (
    Params, aplicar_tope_sector, decidir_ventas, elegir_candidatas, repartir, senales,
)

BENCHMARK = "SPY"
VARIANTES = [(v, e) for v in ("cara", "mediana", "mitad", "nunca") for e in (True, False)]
DEFECTO = ("cara", True)          # elegida ANTES de ver resultados

# Criterios del veredicto (fijados de antemano; editables, pero no tras ver el resultado)
UMBRAL_VS_SPY_PP = 1.5            # ≥ +1,5 puntos/año sobre el S&P 500, tras impuestos
UMBRAL_VS_EW_PP = 0.5             # y ≥ +0,5 sobre el control de supervivencia
UMBRAL_PERCENTIL_AZAR = 0.90      # y por encima del 90 % de las carteras al azar
UMBRAL_DD_PP = 5.0                # y caída máxima no peor que la del S&P 500 en > 5 puntos


# --------------------------------------------------------------------------- #
# Panel de datos (se calcula una vez y se reutiliza en todas las variantes)
# --------------------------------------------------------------------------- #

@dataclass
class Panel:
    tickers: list[str]
    fechas: list[pd.Timestamp]          # fecha de EJECUCIÓN de cada mes
    px: np.ndarray                      # (meses, empresas) precio en esa fecha
    spy: np.ndarray                     # (meses,)
    sen: list[dict[str, np.ndarray]]    # señales calculadas con datos hasta el día anterior
    sector: np.ndarray | None = None    # código de sector por empresa (-1 = desconocido)


def fechas_de_ejecucion(idx: pd.DatetimeIndex, inicio: str) -> list[pd.Timestamp]:
    """Último día hábil de cada mes (dentro de los datos), desde `inicio`.
    Se descarta el mes en curso si aún no ha terminado."""
    s = pd.Series(idx, index=idx)
    ultimos = s.groupby([idx.year, idx.month]).last()
    fechas = [pd.Timestamp(d) for d in ultimos.values]
    hoy = pd.Timestamp.today()
    if fechas and (fechas[-1].year, fechas[-1].month) == (hoy.year, hoy.month):
        fechas = fechas[:-1]
    return [d for d in fechas if d >= pd.Timestamp(inicio)]


def construir_panel(precios: pd.DataFrame, p: Params, inicio: str,
                    sectores: dict[str, str] | None = None) -> Panel:
    if BENCHMARK not in precios.columns:
        raise ValueError(f"Faltan los precios de {BENCHMARK} (el ETF de referencia).")
    acciones = precios.drop(columns=[BENCHMARK])
    idx = precios.index
    fechas = fechas_de_ejecucion(idx, inicio)

    px, spy, sen, fechas_ok = [], [], [], []
    for d in fechas:
        pos = idx.get_loc(d)
        if pos < 1:
            continue
        s = senales(acciones, idx[pos - 1], p)      # SOLO datos hasta el día anterior
        if s["descuento"].notna().sum() == 0:
            continue                                # aún sin 5 años de historia
        px.append(acciones.loc[d].to_numpy(dtype=float))
        spy.append(float(precios.loc[d, BENCHMARK]))
        sen.append({c: s[c].to_numpy(dtype=float) for c in s.columns})
        fechas_ok.append(d)

    if len(fechas_ok) < 24:
        raise ValueError(
            f"Solo hay {len(fechas_ok)} meses utilizables; hacen falta al menos 24. "
            "Adelanta la fecha de descarga o retrasa --inicio."
        )
    cols = list(acciones.columns)
    codigo = None
    if sectores:
        nombres = sorted({v for v in sectores.values() if v})
        mapa = {n: k for k, n in enumerate(nombres)}
        codigo = np.array([mapa.get(sectores.get(t) or "", -1) for t in cols], dtype=int)
    return Panel(cols, fechas_ok, np.array(px), np.array(spy), sen, codigo)


# --------------------------------------------------------------------------- #
# Métricas
# --------------------------------------------------------------------------- #

def xirr(flujos: list[tuple[pd.Timestamp, float]]) -> float:
    """Rentabilidad anual ponderada por dinero (la que de verdad te ha tocado
    a ti, con aportaciones mes a mes). Misma convención que la función XIRR
    de Excel (días/365), así puedes contrastar cifras. Bisección: sin
    dependencias."""
    if not flujos:
        return float("nan")
    t0 = flujos[0][0]

    def npv(r: float) -> float:
        return sum(cf / (1.0 + r) ** ((d - t0).days / 365.0) for d, cf in flujos)

    lo, hi = -0.95, 5.0
    flo, fhi = npv(lo), npv(hi)
    if not (np.isfinite(flo) and np.isfinite(fhi)) or flo * fhi > 0:
        return float("nan")
    for _ in range(200):
        mid = (lo + hi) / 2
        fm = npv(mid)
        if flo * fm <= 0:
            hi = mid
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2


def _impuesto_marginal(pnl_previo: float, delta: float, tipo: float) -> float:
    """Impuesto extra (o devolución, si es negativo) por sumar `delta` al
    resultado realizado del año: las pérdidas del año compensan ganancias del
    mismo año. No se arrastran pérdidas entre años (aproximación prudente)."""
    return tipo * (max(0.0, pnl_previo + delta) - max(0.0, pnl_previo))


@dataclass
class Resultado:
    nombre: str
    fechas: list[pd.Timestamp]
    valor: np.ndarray                   # valor de la cartera tras operar, cada mes
    aportado: np.ndarray                # aportado acumulado
    r_mensual: np.ndarray               # rentabilidad mensual ponderada por tiempo
    valor_final: float
    impuesto_pagado: float
    impuesto_latente: float
    xirr_bruto: float
    xirr_neto: float
    max_dd: float
    extra: dict = field(default_factory=dict)

    @property
    def valor_final_neto(self) -> float:
        return self.valor_final - self.impuesto_latente


def _resultado(nombre, fechas, valor, r, aportacion, imp_pagado, imp_latente, extra=None) -> Resultado:
    n = len(fechas)
    indice = np.cumprod(1.0 + r)
    dd = float((indice / np.maximum.accumulate(indice) - 1.0).min())
    flujos = [(f, -aportacion) for f in fechas]
    vf = float(valor[-1])
    return Resultado(
        nombre=nombre, fechas=fechas, valor=valor,
        aportado=aportacion * np.arange(1, n + 1), r_mensual=r,
        valor_final=vf, impuesto_pagado=imp_pagado, impuesto_latente=imp_latente,
        xirr_bruto=xirr(flujos + [(fechas[-1], vf)]),
        xirr_neto=xirr(flujos + [(fechas[-1], vf - imp_latente)]),
        max_dd=dd, extra=extra or {},
    )


# --------------------------------------------------------------------------- #
# Referencias A y B
# --------------------------------------------------------------------------- #

def dca_spy(panel: Panel, aportacion: float, p: Params) -> Resultado:
    """A. Aportar cada mes solo al ETF."""
    M = len(panel.fechas)
    acc = coste = prev = 0.0
    valor, r = np.zeros(M), np.zeros(M)
    for m in range(M):
        spy = panel.spy[m]
        if prev > 0:
            r[m] = acc * spy / prev - 1.0
        acc += aportacion * (1 - p.comision) / spy
        coste += aportacion
        prev = acc * spy
        valor[m] = prev
    latente = _impuesto_marginal(0.0, prev - coste, p.tipo_impuesto)
    return _resultado("Aportar solo al S&P 500", panel.fechas, valor, r, aportacion, 0.0, latente)


def dca_ew(panel: Panel, aportacion: float, p: Params) -> Resultado:
    """B. Control de supervivencia: cada mes, a partes iguales en TODAS las
    empresas del universo con historia suficiente. Sin ventas."""
    M, T = panel.px.shape
    acc = np.zeros(T)
    ultimo = np.full(T, np.nan)
    caja = coste = prev = 0.0
    valor, r = np.zeros(M), np.zeros(M)
    for m in range(M):
        px = panel.px[m]
        ultimo = np.where(np.isnan(px), ultimo, px)
        pre = float((acc * np.nan_to_num(ultimo)).sum()) + caja
        if prev > 0:
            r[m] = pre / prev - 1.0
        elegibles = ~np.isnan(panel.sen[m]["descuento"]) & ~np.isnan(px)
        n = int(elegibles.sum())
        if n:
            acc[elegibles] += (aportacion / n) * (1 - p.comision) / px[elegibles]
        else:
            caja += aportacion
        coste += aportacion
        prev = float((acc * np.nan_to_num(ultimo)).sum()) + caja
        valor[m] = prev
    latente = _impuesto_marginal(0.0, prev - coste, p.tipo_impuesto)
    return _resultado("Control: todas las empresas a partes iguales", panel.fechas, valor, r,
                      aportacion, 0.0, latente)


# --------------------------------------------------------------------------- #
# La estrategia (y su versión "al azar")
# --------------------------------------------------------------------------- #

def simular(panel: Panel, p: Params, aportacion: float, selector: str = "senal",
            n_por_mes: np.ndarray | None = None,
            rng: np.random.Generator | None = None,
            nombre: str = "Estrategia mensual") -> Resultado:
    """selector='senal'    → las candidatas que marcan las reglas.
       selector='aleatorio' → el MISMO número de candidatas que tuvo la
                              estrategia cada mes (n_por_mes), pero elegidas
                              al azar entre las que tienen datos (con el mismo
                              tope por sector).

       p.relleno='efectivo' → lo que no se invierte espera en efectivo (modelo
                              real). 'etf' → va al S&P 500 (solo diagnóstico)."""
    M, T = panel.px.shape
    acc = np.zeros(T)
    coste = np.zeros(T)
    f_compra = np.full(T, -1)
    mitad = np.zeros(T, dtype=bool)
    ultimo = np.full(T, np.nan)
    etf_acc = etf_coste = 0.0
    caja = 0.0
    factor = (1.0 + p.rend_efectivo) ** (1.0 / 12.0)
    pnl_anio: dict[int, float] = {}
    imp_pagado = 0.0
    prev = 0.0

    valor, r = np.zeros(M), np.zeros(M)
    n_cands = np.zeros(M, dtype=int)
    expo, frac_caja = np.zeros(M), np.zeros(M)
    compras: list[tuple[int, int, float]] = []
    ventas: list[tuple[int, int, float, float, int]] = []   # mes, i, fracción, resultado, meses

    for m in range(M):
        px, spy, s = panel.px[m], panel.spy[m], panel.sen[m]
        ultimo = np.where(np.isnan(px), ultimo, px)
        anio = panel.fechas[m].year

        if m > 0:
            caja *= factor                       # el efectivo que espera rinde algo
        val_pos = acc * np.nan_to_num(ultimo)
        if prev > 0:
            r[m] = (val_pos.sum() + etf_acc * spy + caja) / prev - 1.0

        # --- ventas ------------------------------------------------------- #
        vendido = 0.0
        if p.venta != "nunca" or p.vender_por_tiempo:
            meses_t = np.where(f_compra >= 0, m - f_compra, 0)
            frac = decidir_ventas(acc > 0, s["precio"], s["mediana"], s["percentil"],
                                  mitad, meses_t, p)
            for i in np.flatnonzero(frac > 0):
                if np.isnan(px[i]):
                    continue
                f = float(frac[i])
                neto = f * acc[i] * px[i] * (1 - p.comision)
                gan = neto - f * coste[i]
                prev_pnl = pnl_anio.get(anio, 0.0)
                imp = _impuesto_marginal(prev_pnl, gan, p.tipo_impuesto)
                pnl_anio[anio] = prev_pnl + gan
                imp_pagado += imp
                vendido += neto - imp
                ventas.append((m, int(i), f, float(gan), int(meses_t[i])))
                acc[i] *= 1 - f
                coste[i] *= 1 - f
                if f < 1.0:
                    mitad[i] = True
                else:
                    f_compra[i], mitad[i] = -1, False

        # --- compras ------------------------------------------------------ #
        disponible = caja + aportacion + vendido      # (en modo 'etf', caja es siempre 0)
        caja = 0.0
        val_pos = acc * np.nan_to_num(ultimo)

        if selector == "senal":
            idx = elegir_candidatas(s["descuento"], s["percentil"], s["rebote"], p, panel.sector)
        else:
            validos = np.flatnonzero(~np.isnan(s["descuento"]) & ~np.isnan(px))
            k = min(int(n_por_mes[m]), len(validos))
            idx = (aplicar_tope_sector(rng.permutation(validos), panel.sector, p.n_max_sector, k)
                   if k > 0 else np.array([], dtype=int))
        idx = np.array([i for i in idx if not np.isnan(px[i])], dtype=int)

        asignado, sobrante = repartir(disponible, idx, val_pos, p)
        for i, importe in asignado.items():
            if acc[i] == 0:
                f_compra[i], mitad[i] = m, False
            acc[i] += importe * (1 - p.comision) / px[i]
            coste[i] += importe
            compras.append((m, i, float(importe)))
        if sobrante > 0:
            if p.relleno == "etf":
                etf_acc += sobrante * (1 - p.comision) / spy
                etf_coste += sobrante
            else:
                caja = sobrante

        n_cands[m] = len(idx)
        val_pos = acc * np.nan_to_num(ultimo)
        prev = float(val_pos.sum() + etf_acc * spy + caja)
        valor[m] = prev
        expo[m] = val_pos.sum() / prev if prev > 0 else 0.0
        frac_caja[m] = caja / prev if prev > 0 else 0.0

    # Valor latente: lo que se pagaría de impuestos si hoy se vendiera todo.
    val_pos = acc * np.nan_to_num(ultimo)
    no_realizado = float((etf_acc * panel.spy[-1] - etf_coste) + (val_pos - coste).sum())
    anio_final = panel.fechas[-1].year
    latente = _impuesto_marginal(pnl_anio.get(anio_final, 0.0), no_realizado, p.tipo_impuesto)

    perdidas = [v for v in ventas if v[3] < 0]
    extra = {
        "n_cands": n_cands, "expo": expo, "compras": compras, "ventas": ventas,
        "n_compras": len(compras), "n_ventas": len(ventas),
        "pct_ventas_perdida": (len(perdidas) / len(ventas)) if ventas else float("nan"),
        "meses_con_candidatas": float((n_cands > 0).mean()),
        "expo_media": float(expo.mean()),
        "caja_media": float(frac_caja.mean()),
        "tenencia_media": float(np.mean([v[4] for v in ventas])) if ventas else float("nan"),
        "abiertas": int((acc > 0).sum()),
    }
    return _resultado(nombre, panel.fechas, valor, r, aportacion, imp_pagado, latente, extra)


def montecarlo(panel: Panel, p: Params, aportacion: float, n_por_mes: np.ndarray,
               n_sim: int, semilla: int = 12345) -> np.ndarray:
    """XIRR neto de `n_sim` carteras que repiten la estrategia pero con las
    empresas elegidas al azar."""
    rng = np.random.default_rng(semilla)
    return np.array([
        simular(panel, p, aportacion, "aleatorio", n_por_mes, rng).xirr_neto
        for _ in range(n_sim)
    ])


# --------------------------------------------------------------------------- #
# Veredicto
# --------------------------------------------------------------------------- #

def veredicto(est: Resultado, spy: Resultado, ew: Resultado, azar: np.ndarray):
    pct_azar = float(np.mean(azar < est.xirr_neto)) if len(azar) else float("nan")
    d_spy = (est.xirr_neto - spy.xirr_neto) * 100
    d_ew = (est.xirr_neto - ew.xirr_neto) * 100
    d_dd = (spy.max_dd - est.max_dd) * 100        # >0 = la estrategia cae más

    def ok(cond) -> bool:
        return bool(cond) if not (isinstance(cond, float) and np.isnan(cond)) else False

    criterios = [
        (f"Bate al S&P 500 en ≥ {_d(UMBRAL_VS_SPY_PP, 1, False)} puntos/año tras impuestos",
         ok(d_spy >= UMBRAL_VS_SPY_PP), f"{_d(d_spy)} puntos/año"),
        (f"Bate al control de supervivencia en ≥ {_d(UMBRAL_VS_EW_PP, 1, False)} puntos/año",
         ok(d_ew >= UMBRAL_VS_EW_PP), f"{_d(d_ew)} puntos/año"),
        (f"Queda por encima del {UMBRAL_PERCENTIL_AZAR * 100:.0f} % de las carteras al azar",
         ok(pct_azar >= UMBRAL_PERCENTIL_AZAR), f"por encima del {pct_azar * 100:.0f} %" if azar.size else "sin datos"),
        (f"No cae más de {_d(UMBRAL_DD_PP, 0, False)} puntos peor que el S&P 500 en su peor racha",
         ok(d_dd <= UMBRAL_DD_PP), f"{_d(d_dd, 1)} puntos"),
    ]
    return all(c[1] for c in criterios), criterios, pct_azar


# --------------------------------------------------------------------------- #
# Informe
# --------------------------------------------------------------------------- #

def _n(x: float, dec: int = 0) -> str:
    s = f"{x:,.{dec}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def _d(x: float, dec: int = 2, signo: bool = True) -> str:
    """Número con decimales en coma, con signo opcional (para diferencias)."""
    if x is None or not np.isfinite(x):
        return "—"
    return (f"{x:+.{dec}f}" if signo else f"{x:.{dec}f}").replace(".", ",")


def _pc(x: float, dec: int = 1) -> str:
    return "—" if x is None or not np.isfinite(x) else f"{x * 100:.{dec}f} %".replace(".", ",")


def _por_anio(res: Resultado) -> dict[int, float]:
    out: dict[int, float] = {}
    for f, rr in zip(res.fechas, res.r_mensual):
        out[f.year] = (1.0 + out.get(f.year, 0.0)) * (1.0 + rr) - 1.0
    return out


def informe_md(panel: Panel, p: Params, aportacion: float, n_sim: int, base: dict,
               variantes: list[dict], veredicto_ok: bool, criterios, pct_azar: float) -> str:
    est, spy, ew = base["est"], base["spy"], base["ew"]
    est_etf = base["est_etf"]
    azar = base["azar"]
    L: list[str] = []
    A = L.append

    A("# Backtest del módulo mensual\n")
    A(f"**Periodo:** {panel.fechas[0]:%m/%Y} → {panel.fechas[-1]:%m/%Y} "
      f"({len(panel.fechas)} meses) · **Aportación:** {_n(aportacion)} $/mes · "
      f"**Universo:** {len(panel.tickers)} empresas (S&P 500 actual) · "
      f"**Impuesto supuesto:** {_pc(p.tipo_impuesto, 0)} sobre ganancias · "
      f"**Comisión:** {_pc(p.comision)} por operación\n")
    A(f"**Modelo:** solo acciones, máx. {p.n_max} por mes y {p.n_max_sector} por sector. Cada mes "
      f"aportas {_n(aportacion)} $; lo que no se invierte espera en efectivo "
      f"({_pc(p.rend_efectivo)} neto al año) hasta que haya candidatas. Sin ETF de relleno.\n")

    A("## Veredicto\n")
    A(("### ✅ Pasa los criterios fijados de antemano\n\nEso significa solo que NO queda "
       "descartada: el backtest solo puede decir «no», nunca «sí» (ver «Cómo leerlo»). "
       "El siguiente paso sería probarla en paper trading.\n") if veredicto_ok else
      ("### ❌ No pasa los criterios fijados de antemano\n\nNo compensa construir el resto "
       "del módulo con estas reglas: ni con datos que favorecen a la estrategia consigue "
       "una ventaja clara sobre aportar al S&P 500.\n"))
    for texto, ok, detalle in criterios:
        A(f"- {'✅' if ok else '❌'} {texto}: **{detalle}**")
    A("")

    A(f"## Resultado de la variante elegida de antemano ({p.etiqueta()})\n")
    A("| | Aportado | Valor final | Valor tras impuestos | Rentab. anual (tras imp.) | Peor caída |")
    A("|---|---:|---:|---:|---:|---:|")
    filas = [est, spy, ew]
    for r in filas:
        A(f"| {r.nombre} | {_n(r.aportado[-1])} $ | {_n(r.valor_final)} $ | "
          f"{_n(r.valor_final_neto)} $ | **{_pc(r.xirr_neto)}** | {_pc(r.max_dd)} |")
    if azar.size:
        A(f"| Control de azar (mediana de {n_sim} carteras) | — | — | — | "
          f"{_pc(float(np.median(azar)))} | — |")
    A(f"| *Diagnóstico:* misma estrategia con el efectivo parado en el S&P 500 | "
      f"{_n(est_etf.aportado[-1])} $ | {_n(est_etf.valor_final)} $ | {_n(est_etf.valor_final_neto)} $ | "
      f"{_pc(est_etf.xirr_neto)} | {_pc(est_etf.max_dd)} |")
    A("")
    e = est.extra
    A(f"Había candidatas en {_pc(e['meses_con_candidatas'], 0)} de los meses. La estrategia hizo "
      f"{e['n_compras']} compras y {e['n_ventas']} ventas "
      f"({_pc(e['pct_ventas_perdida'], 0)} de ellas con pérdida), mantuvo de media "
      f"{_n(e['tenencia_media'], 1)} meses cada posición vendida, y tuvo de media "
      f"{_pc(e['caja_media'], 0)} de la cartera en efectivo esperando. "
      f"Impuestos pagados en ventas: {_n(est.impuesto_pagado)} $.\n")
    cuesta_esperar = (est.xirr_neto - est_etf.xirr_neto) * 100
    seleccion = (est_etf.xirr_neto - spy.xirr_neto) * 100
    A("**¿De dónde sale la diferencia con el S&P 500?** Hay dos efectos mezclados:\n")
    A(f"- *Elegir las acciones* (con el efectivo en el S&P 500, para no penalizar la espera): "
      f"**{_d(seleccion)} puntos/año** frente al S&P 500.")
    A(f"- *Esperar en efectivo y desplegarlo de golpe cuando aparecen candidatas*: "
      f"**{_d(cuesta_esperar)} puntos/año** adicionales (negativo = esperar cuesta rentabilidad, "
      f"el precio de no invertir cuando nada está barato; positivo = concentrar el dinero "
      f"acumulado en las caídas salió bien).\n")
    A("El veredicto de arriba usa el resultado REAL (con la espera); el diagnóstico solo ayuda a "
      "entender por qué.\n")

    A("## Sensibilidad: las 8 variantes\n")
    A("Para ver si el resultado depende de un detalle concreto. **No escojas la mejor:** con 8 "
      "intentos, la que sale primera suele serlo por casualidad. Lo que importa es si TODAS "
      "quedan por encima o por debajo.\n")
    A("| Venta | Espera a que deje de caer | Rentab. anual | vs S&P 500 (puntos) | vs control superv. (puntos) | "
      "Por encima del % de azar | Ventas | Efectivo medio | Solo la selección (efectivo en S&P, puntos) |")
    A("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for v in variantes:
        r = v["res"]
        A(f"| {v['venta']} | {'sí' if v['estab'] else 'no'} | {_pc(r.xirr_neto)} | "
          f"{_d((r.xirr_neto - spy.xirr_neto) * 100)} | {_d((r.xirr_neto - ew.xirr_neto) * 100)} | "
          f"{_pc(v['pct_azar'], 0)} | {r.extra['n_ventas']} | {_pc(r.extra['caja_media'], 0)} | "
          f"{_d((v['res_etf'].xirr_neto - spy.xirr_neto) * 100)} |")
    A("")

    A("## Año a año (rentabilidad ponderada por tiempo)\n")
    A("| Año | Estrategia | S&P 500 | Control superv. | Diferencia vs S&P 500 (puntos) |")
    A("|---|---:|---:|---:|---:|")
    ye, ys, yw = _por_anio(est), _por_anio(spy), _por_anio(ew)
    ganados = 0
    for y in sorted(ye):
        d = ye[y] - ys[y]
        ganados += d > 0
        A(f"| {y} | {_pc(ye[y])} | {_pc(ys[y])} | {_pc(yw[y])} | {_d(d * 100, 1)} |")
    A(f"\nLa estrategia fue mejor que el S&P 500 en {ganados} de {len(ye)} años.\n")

    A("## Cómo leerlo (y qué NO demuestra)\n")
    A("- **Sesgo de supervivencia.** El universo son las empresas que HOY están en el S&P 500. "
      "Las que quebraron o se hundieron no aparecen, y eso favorece a cualquier estrategia que "
      "compre caídas. Por eso existen los dos controles: si la estrategia no los supera, su "
      "ventaja es solo ese sesgo.")
    A("- **El efectivo parado pesa.** Como tú pones el dinero a mano y solo en lo que la herramienta "
      "señale, los meses sin candidatas el dinero espera (a un tipo bajo) mientras el mercado suele "
      "subir. Eso es un coste real del método y por eso el veredicto lo incluye; el diagnóstico de "
      "arriba te dice cuánto de la diferencia viene de elegir mal/bien y cuánto de esperar.")
    A("- **Sin filtro de calidad.** Aquí solo se mide la parte de PRECIO. En la versión real, "
      "las candidatas pasarían además el cribado fundamental del bot de largo plazo.")
    A("- **Un solo camino histórico.** Es una única secuencia de años; no hay forma de saber si "
      "se repetiría. Un resultado bueno es necesario, no suficiente.")
    A("- **Impuestos aproximados.** Tipo fijo, pérdidas compensadas dentro del mismo año, sin "
      "arrastre entre años ni fiscalidad de dividendos ni cambio dólar/euro.")
    A("- **Ejecución idealizada.** Precio de cierre del último día hábil, comisión fija y sin "
      "deslizamiento. En acciones muy líquidas del S&P 500 es razonable.")
    return "\n".join(L)


def _grafico(base: dict, ruta: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    est, spy, ew = base["est"], base["spy"], base["ew"]
    fig, ax = plt.subplots(figsize=(9, 4.8), dpi=130)
    ax.plot(est.fechas, est.valor, label="Estrategia mensual", linewidth=2)
    ax.plot(spy.fechas, spy.valor, label="Aportar solo al S&P 500", linewidth=2)
    ax.plot(ew.fechas, ew.valor, label="Control de supervivencia", linewidth=1.3, alpha=0.8)
    if "est_etf" in base:
        ax.plot(base["est_etf"].fechas, base["est_etf"].valor, ":", linewidth=1.3,
                label="Diagnóstico: efectivo en S&P 500")
    ax.plot(est.fechas, est.aportado, "--", color="gray", label="Dinero aportado", linewidth=1)
    ax.set_title("Valor de la cartera (antes de impuestos)")
    ax.set_ylabel("$")
    ax.grid(alpha=0.25)
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(ruta)
    plt.close(fig)


# --------------------------------------------------------------------------- #
# Orquestación
# --------------------------------------------------------------------------- #

def ejecutar(precios: pd.DataFrame, inicio: str, aportacion: float, n_sim: int,
             salida: Path, impuesto: float = 0.21, verbose: bool = True,
             sectores: dict[str, str] | None = None) -> dict:
    salida.mkdir(parents=True, exist_ok=True)
    p0 = Params(tipo_impuesto=impuesto)
    panel = construir_panel(precios, p0, inicio, sectores)
    if verbose:
        print(f"Panel listo: {len(panel.fechas)} meses × {len(panel.tickers)} empresas.")

    spy = dca_spy(panel, aportacion, p0)
    ew = dca_ew(panel, aportacion, p0)

    variantes: list[dict] = []
    base: dict = {}
    for venta, estab in VARIANTES:
        p = dataclasses.replace(p0, venta=venta, estabilizacion=estab)
        est = simular(panel, p, aportacion)
        est_etf = simular(panel, dataclasses.replace(p, relleno="etf"), aportacion,
                          nombre="Diagnóstico: efectivo en S&P 500")
        azar = montecarlo(panel, p, aportacion, est.extra["n_cands"], n_sim) if n_sim else np.array([])
        pct = float(np.mean(azar < est.xirr_neto)) if azar.size else float("nan")
        variantes.append({"venta": venta, "estab": estab, "res": est, "res_etf": est_etf,
                          "pct_azar": pct})
        if (venta, estab) == DEFECTO:
            base = {"est": est, "est_etf": est_etf, "spy": spy, "ew": ew, "azar": azar, "p": p}
        if verbose:
            print(f"  variante {venta:8} espera={'sí' if estab else 'no':2} → "
                  f"{est.xirr_neto * 100:5.2f} %/año (S&P 500: {spy.xirr_neto * 100:5.2f} %)")

    ok, criterios, pct_azar = veredicto(base["est"], spy, ew, base["azar"])
    md = informe_md(panel, base["p"], aportacion, n_sim, base, variantes, ok, criterios, pct_azar)
    (salida / "informe.md").write_text(md, encoding="utf-8")

    curvas = pd.DataFrame({
        "fecha": base["est"].fechas, "aportado": base["est"].aportado,
        "estrategia": base["est"].valor, "sp500": spy.valor, "control_supervivencia": ew.valor,
    })
    curvas.to_csv(salida / "curvas.csv", index=False)

    filas = []
    for m, i, importe in base["est"].extra["compras"]:
        filas.append({"fecha": panel.fechas[m].date(), "operacion": "compra",
                      "ticker": panel.tickers[i], "importe": round(importe, 2), "resultado": ""})
    for m, i, f, gan, meses in base["est"].extra["ventas"]:
        filas.append({"fecha": panel.fechas[m].date(), "operacion": f"venta {f:.0%}",
                      "ticker": panel.tickers[i], "importe": "", "resultado": round(gan, 2)})
    pd.DataFrame(filas).sort_values("fecha").to_csv(salida / "operaciones.csv", index=False)

    try:
        _grafico(base, salida / "curvas.png")
    except Exception as e:  # noqa: BLE001  (el gráfico es un extra; no tumba el informe)
        print(f"Aviso: no se pudo dibujar el gráfico ({e}).")

    return {"ok": ok, "criterios": criterios, "informe": md, "base": base, "variantes": variantes}


def main() -> None:
    from .precios import cargar_precios, universo_sp500_con_sector

    ap = argparse.ArgumentParser(description="Backtest del módulo mensual de oportunidades.")
    ap.add_argument("--inicio", default="2012-01-01", help="primer mes simulado")
    ap.add_argument("--aportacion", type=float, default=1000.0, help="aportación mensual ($)")
    ap.add_argument("--montecarlo", type=int, default=100, help="nº de carteras al azar (0 = sin control de azar)")
    ap.add_argument("--impuesto", type=float, default=0.21, help="tipo sobre ganancias (0.21 = 21 %%)")
    ap.add_argument("--salida", default="resultados", help="carpeta de salida")
    args = ap.parse_args()

    desde = f"{int(args.inicio[:4]) - 6}-01-01"        # 5 años de ventana + margen
    tickers, sectores = universo_sp500_con_sector()
    precios = cargar_precios(tickers, desde)
    res = ejecutar(precios, args.inicio, args.aportacion, args.montecarlo,
                   Path(args.salida), args.impuesto, sectores=sectores)
    print("\n" + res["informe"])


if __name__ == "__main__":
    main()
