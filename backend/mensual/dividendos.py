"""
Small caps con dividendo que NO se recorta — sección «Dividendos» del mensual.

Idea (la tuya): en empresas pequeñas (Russell 2000) que pagan dividendo, lo que
da seguridad no es el porcentaje (que sube y baja con el precio de la acción)
sino que el DINERO que pagan por acción se mantenga o suba año tras año: quien
no recorta, apuesta por sí mismo. Las recompras de acciones propias y la falta de
dilución son otra buena señal. Y la rentabilidad anual real de una empresa así es:

        rentabilidad total = lo que sube el precio + lo que paga de dividendo

así que cada candidata muestra las dos partes por separado (y frente al Russell
2000, ETF IWM).

CÓMO SE ELIGE (de lo más duro a lo más blando):

  1. Dividendos históricos de Yahoo (por acción, ajustados por splits). Se quitan
     los pagos especiales (un pago único muy superior al normal) para no
     confundirlos con una subida ni con un recorte.
  2. Puertas duras de dividendo: paga ahora, lleva ≥ 5 años pagando, NINGÚN pago
     regular por debajo del anterior (más de un 5 %) en los últimos 5 años, y un
     dividendo ni ridículo (< 0,8 %) ni sospechoso (> 9 %: casi siempre es que el
     mercado descuenta un recorte).
  3. Puertas duras de empresa (SEC, igual que el resto del sistema): tamaño de
     small cap (300–8.000 M$), el dividendo cubierto por la caja libre (o por el
     beneficio/caja operativa en bancos y REITs), caja libre positiva, deuda
     razonable y sin diluir a los accionistas.
  4. Puntuación 0–100: constancia y crecimiento del dividendo (35 %), seguridad
     (30 %), trato al accionista: recompras y dilución (15 %), nivel de
     rentabilidad por dividendo (10 %) y salud del precio (10 %).
  5. Máx. 2 por sector. El resto queda «en espera».

LO QUE ESTO NO ES: no está probado en el pasado (no tenemos gratis los datos de
cada fecha ni las empresas que quebraron); los números históricos de rentabilidad
de cada candidata son de empresas que HOY siguen en el índice, o sea, de
supervivientes. Un dividendo alto con el precio cayendo es una trampa clásica.
El juez es el seguimiento en vivo (ver `seguimiento.py`): cada mes apunta qué se
habría elegido y lo compara con el Russell 2000 (IWM).

Todo lo de decisión son funciones puras (sin red) para poder probarlas.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from .paquetes import esc, media_ponderada

log = logging.getLogger(__name__)

IWM = "IWM"
URL_IWM = ("https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/"
           "1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund")
URL_IWM_PRODUCTO = "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf"
URL_SP600 = "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies"
URL_SP400 = "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) inversor-lp/1.0"}
FRECUENCIAS = (1, 2, 4, 12)

INFO = {
    "nombre": "Small caps con dividendo creciente",
    "horizonte": "3 años o más",
    "etiqueta": "Dividendos",
    "descripcion": (
        "Empresas pequeñas (tipo Russell 2000) que pagan dividendo y NO lo recortan: el dinero por "
        "acción se mantiene o sube año tras año. Se exige que la caja cubra el dividendo, poca "
        "deuda y que no diluyan; las recompras de acciones suman. La rentabilidad anual es lo que "
        "sube el precio más lo que paga de dividendo."),
}


@dataclass(frozen=True)
class ReglasDividendos:
    # tamaño (millones de dólares)
    cap_min: float = 300.0
    cap_max: float = 8000.0
    # dividendo
    anios_pagando_min: float = 5.0
    sin_recorte_min: float = 5.0         # años sin ningún recorte
    tolerancia_recorte: float = 0.05     # un pago regular < 95 % del anterior = recorte
    yield_min: float = 0.8               # %
    yield_max: float = 9.0               # % (por encima, casi siempre es trampa)
    caida_max: float = 0.50              # no más de un 50 % bajo su máximo de 52 semanas
    # empresa
    cobertura_max: float = 100.0         # dividendo / caja libre (%), duro
    cobertura_aviso: float = 75.0        # por encima, aviso
    deuda_max: float = 3.5               # deuda neta / EBITDA (no financieras)
    dilucion_max: float = 3.0            # % de acciones en 5 años
    fcf_anios_min: int = 3
    # selección
    n: int = 8
    n_max_sector: int = 2
    max_fundamentos: int = 300           # cuántas se miran en la SEC (las mejor puntuadas por dividendo)
    cobertura_datos_min: float = 0.60
    pesos: tuple = (0.35, 0.30, 0.15, 0.10, 0.10)   # dividendo, seguridad, accionista, nivel, precio


# --------------------------------------------------------------------------- #
# 1. Dividendos: limpieza y análisis
# --------------------------------------------------------------------------- #

def limpiar_pagos(div: pd.Series) -> tuple[pd.Series, int]:
    """Devuelve (pagos regulares, nº de pagos especiales quitados).

    · Se descartan duplicados (mismo importe con menos de 10 días de diferencia,
      un fallo típico de Yahoo).
    · Un pago > 1,6× la mediana de los 4 anteriores es ESPECIAL si el siguiente
      vuelve al nivel normal (o, si es el último, si supera 2×): ni cuenta como
      subida ni hace que el siguiente parezca un recorte."""
    if div is None or len(div) == 0:
        return pd.Series(dtype=float), 0
    s = div.dropna()
    s = s[s > 0].sort_index()
    if s.empty:
        return s, 0
    fechas, vals = list(s.index), [float(x) for x in s.to_numpy()]
    # duplicados
    f2, v2 = [fechas[0]], [vals[0]]
    for d, a in zip(fechas[1:], vals[1:]):
        if (d - f2[-1]).days < 10 and abs(a - v2[-1]) <= 0.01 * v2[-1]:
            continue
        f2.append(d)
        v2.append(a)
    fechas, vals = f2, v2
    # especiales
    kept_f, kept_v, especiales = [], [], 0
    for i, (d, a) in enumerate(zip(fechas, vals)):
        if len(kept_v) >= 3:
            ref = float(np.median(kept_v[-4:]))
            if a > 1.6 * ref:
                sig = vals[i + 1] if i + 1 < len(vals) else None
                if (sig is not None and sig <= 1.3 * ref) or (sig is None and a > 2.0 * ref):
                    especiales += 1
                    continue
        kept_f.append(d)
        kept_v.append(a)
    return pd.Series(kept_v, index=pd.DatetimeIndex(kept_f)), especiales


def _frecuencia(reg: pd.Series) -> int | None:
    """Pagos por año (1, 2, 4 o 12) según los últimos 24 meses de pagos de la empresa
    (contados hasta su último pago, así también se reconoce a la que ya dejó de pagar)."""
    if len(reg) == 0:
        return None
    ultimo = reg.index[-1]
    ult = reg.loc[reg.index > ultimo - pd.Timedelta(days=730)]
    historia = (ultimo - reg.index[0]).days
    por_anio = len(ult) / 2.0 if historia >= 700 else len(ult) / max(historia / 365.25, 0.5)
    return min(FRECUENCIAS, key=lambda f: abs(f - por_anio))


def _suma_ventana(reg: pd.Series, hasta: pd.Timestamp, freq: int) -> float | None:
    """Suma de los `freq` últimos pagos con fecha <= hasta. None si esos pagos no
    abarcan un año razonable (cambio de frecuencia) o no hay suficientes."""
    sub = reg.loc[:hasta]
    if len(sub) < freq:
        return None
    ult = sub.iloc[-freq:]
    span = (ult.index[-1] - ult.index[0]).days
    if span > 365.0 * (freq - 1) / freq + 70:
        return None
    return float(ult.sum())


def _cagr(fin: float | None, ini: float | None, anios: float) -> float | None:
    if fin is None or ini is None or ini <= 0 or fin <= 0 or anios <= 0:
        return None
    return ((fin / ini) ** (1.0 / anios) - 1.0) * 100.0


def analizar_dividendo(div: pd.Series, precio: float | None, hoy, reglas: ReglasDividendos | None = None) -> dict:
    """Todo lo que se puede saber del dividendo por acción a partir de sus pagos."""
    r = reglas or ReglasDividendos()
    hoy = pd.Timestamp(hoy)
    out = {"paga": False}
    reg, especiales = limpiar_pagos(div)
    if len(reg) < 4:
        return out
    freq = _frecuencia(reg)
    if freq is None:
        return out
    ultimo = reg.index[-1]
    dias = (hoy - ultimo).days
    pagando = dias <= 365.0 / freq * 1.5 + 15
    primero = reg.index[0]
    anios_pagando = (hoy - primero).days / 365.25

    # Recortes y subidas entre pagos regulares consecutivos. Sólo cuenta si coinciden dos
    # lecturas: el importe del pago y su equivalente anual según el hueco entre pagos
    # (así un paso de pago semestral a trimestral con el mismo importe anual no es un recorte).
    v = reg.to_numpy()
    dias_hueco = np.diff(reg.index.to_numpy().astype("datetime64[D]").astype(int)).astype(float)
    f_loc = np.array([min(FRECUENCIAS, key=lambda f: abs(f - 365.0 / max(g, 1.0))) for g in dias_hueco])
    f_loc = np.r_[f_loc[0], f_loc]
    tasa = v * f_loc
    r_pago = v[1:] / v[:-1]
    r_anual = tasa[1:] / tasa[:-1]
    fechas = reg.index[1:]
    es_corte = (r_pago < 1.0 - r.tolerancia_recorte) & (r_anual < 1.0 - r.tolerancia_recorte)
    es_subida = (r_pago > 1.01) & (r_anual > 1.01)
    cortes = [d for d, c in zip(fechas, es_corte) if c]
    ultimo_recorte = cortes[-1] if cortes else None
    anios_sin_recorte = ((hoy - ultimo_recorte).days / 365.25) if ultimo_recorte is not None else anios_pagando
    hace5 = hoy - pd.DateOffset(years=5)
    subidas5 = int(sum(1 for d, u in zip(fechas, es_subida) if u and d >= hace5))
    recortes5 = int(sum(1 for d in cortes if d >= hace5))

    dps = _suma_ventana(reg, hoy, freq)
    dps5 = _suma_ventana(reg, hoy - pd.DateOffset(years=5), freq)
    dps3 = _suma_ventana(reg, hoy - pd.DateOffset(years=3), freq)
    # sólo vale el crecimiento si el pago de hace N años existía de verdad (historia suficiente)
    hist5 = primero <= hoy - pd.DateOffset(years=5) + pd.Timedelta(days=60)
    hist3 = primero <= hoy - pd.DateOffset(years=3) + pd.Timedelta(days=60)
    cagr5 = _cagr(dps, dps5, 5.0) if hist5 else None
    cagr3 = _cagr(dps, dps3, 3.0) if hist3 else None

    out.update({
        "paga": True, "pagando": bool(pagando), "frecuencia": int(freq),
        "n_pagos": int(len(reg)), "especiales": int(especiales),
        "primer_pago": f"{primero:%Y-%m-%d}", "ultimo_pago": f"{ultimo:%Y-%m-%d}",
        "dias_desde_ultimo": int(dias),
        "anios_pagando": round(anios_pagando, 1),
        "anios_sin_recorte": round(anios_sin_recorte, 1),
        "recortes_5a": recortes5, "subidas_5a": subidas5,
        "ultimo_recorte": None if ultimo_recorte is None else f"{ultimo_recorte:%Y-%m-%d}",
        "dps": None if dps is None else round(dps, 4),
        "dps_hace_5a": None if dps5 is None else round(dps5, 4),
        "cagr_5a": None if cagr5 is None else round(cagr5, 2),
        "cagr_3a": None if cagr3 is None else round(cagr3, 2),
        "rentab_div": None,
    })
    if dps is not None and precio and precio > 0:
        out["rentab_div"] = round(dps / precio * 100.0, 2)
    return out


# --------------------------------------------------------------------------- #
# 2. Rentabilidad: precio + dividendo
# --------------------------------------------------------------------------- #

def _valor_en(s: pd.Series, fecha: pd.Timestamp):
    sub = s.loc[:fecha].dropna()
    if sub.empty or (fecha - sub.index[-1]).days > 10:
        return None
    return float(sub.iloc[-1])


def rendimientos(close: pd.Series, adj: pd.Series, hoy, anios=(1, 3, 5)) -> dict:
    """Para cada horizonte: variación del PRECIO, rentabilidad TOTAL (con los
    dividendos reinvertidos, serie ajustada de Yahoo) y lo que aportan los
    dividendos. A 1 año es el total del año; a 3 y 5, anualizado."""
    hoy = pd.Timestamp(hoy)
    out: dict = {}
    c1, a1 = _valor_en(close, hoy), _valor_en(adj, hoy)
    if c1 is None or a1 is None:
        return out
    primero = min(close.dropna().index[0], adj.dropna().index[0])
    for n in anios:
        ini = hoy - pd.DateOffset(years=n)
        if primero > ini + pd.Timedelta(days=30):
            continue
        c0, a0 = _valor_en(close, ini), _valor_en(adj, ini)
        if not c0 or not a0:
            continue
        pr, to = c1 / c0, a1 / a0
        if n == 1:
            p, t = (pr - 1.0) * 100.0, (to - 1.0) * 100.0
        else:
            p, t = (pr ** (1.0 / n) - 1.0) * 100.0, (to ** (1.0 / n) - 1.0) * 100.0
        out[f"{n}a"] = {"precio": round(p, 2), "total": round(t, 2), "div": round(t - p, 2)}
    return out


def caida_52s(close: pd.Series) -> float | None:
    c = close.dropna()
    if len(c) < 120:
        return None
    return float(c.iloc[-1] / c.iloc[-252:].max() - 1.0)


# --------------------------------------------------------------------------- #
# 3. Empresa: cobertura, deuda, recompras (Fundamentals de la SEC)
# --------------------------------------------------------------------------- #

def _suma(xs, k=3):
    xs = [x for x in (xs or [])[-k:] if x is not None]
    return sum(xs) if xs else None


def evaluar_empresa(f, m, dps: float | None, reglas: ReglasDividendos, especiales: int = 0) -> dict:
    """`f` = providers.Fundamentals, `m` = quant_filter.Metrics (o dict con los mismos
    campos). Devuelve las cifras de seguridad y los motivos de veto."""
    g = (lambda k: getattr(m, k, None)) if not isinstance(m, dict) else (lambda k: m.get(k))
    veto: list[str] = []
    aviso: list[str] = []
    financiera = bool(getattr(f, "is_financial", False))
    mcap = f.market_cap
    if mcap is None and f.price and f.shares_diluted:
        mcap = f.price * f.shares_diluted[-1]

    div_cash = _suma(f.dividends_paid)
    k_anios = min(3, len(f.dividends_paid or [])) or 3
    if especiales and dps and f.shares_diluted:
        # el efectivo pagado incluye los especiales: se mide la cobertura sólo del dividendo regular
        div_cash = dps * f.shares_diluted[-1] * k_anios
        aviso.append("Ha pagado dividendos especiales: la cobertura se mide solo con el dividendo regular.")
    elif (not div_cash) and dps and f.shares_diluted:
        div_cash = dps * f.shares_diluted[-1] * 3   # aprox. de 3 años
        aviso.append("La SEC no detalla el dividendo pagado: cobertura estimada con el dividendo por acción.")

    fcf_serie = list(g("fcf_serie") or [])
    fcf3 = _suma(fcf_serie)
    ni3 = _suma(f.net_income)
    cfo3 = _suma(f.cfo)
    payout_fcf = payout_ni = payout_cfo = None
    if div_cash:
        if fcf3 and fcf3 > 0:
            payout_fcf = div_cash / fcf3 * 100.0
        if ni3 and ni3 > 0:
            payout_ni = div_cash / ni3 * 100.0
        if cfo3 and cfo3 > 0:
            payout_cfo = div_cash / cfo3 * 100.0
    if financiera:
        cands = [x for x in (payout_ni, payout_cfo) if x is not None]
        payout = min(cands) if cands else None
        tipo = "beneficio/caja operativa (banco, seguro o REIT)"
    else:
        payout = payout_fcf
        tipo = "caja libre"

    if mcap is None:
        veto.append("capitalización no disponible")
    elif mcap < reglas.cap_min:
        veto.append(f"demasiado pequeña ({mcap:,.0f} M$)")
    elif mcap > reglas.cap_max:
        veto.append(f"ya no es small cap ({mcap:,.0f} M$)")

    if payout is None:
        veto.append("no se puede comprobar que la caja cubra el dividendo")
    elif payout > reglas.cobertura_max:
        veto.append(f"el dividendo no lo cubre la {'caja' if not financiera else 'caja/beneficio'} ({payout:.0f} %)")
    elif payout > reglas.cobertura_aviso:
        aviso.append(f"Reparte el {payout:.0f} % de su {tipo}: poco margen si el negocio flojea.")

    fcf_pos = int(g("fcf_positive_years") or 0)
    deuda = g("net_debt_ebitda")
    if not financiera:
        if fcf_pos < reglas.fcf_anios_min:
            veto.append(f"caja libre positiva solo {fcf_pos} años")
        if deuda is not None and deuda > reglas.deuda_max:
            veto.append(f"deuda neta/EBITDA {deuda:.1f}x")
    dilucion = g("share_change_5y")
    if dilucion is not None and dilucion > reglas.dilucion_max:
        veto.append(f"diluye a los accionistas ({dilucion:+.0f} % de acciones)")

    recompra = f.buybacks[-1] if f.buybacks else None
    bb_yield = (recompra / mcap * 100.0) if (recompra and mcap) else None
    return {
        "financiera": financiera, "cap": None if mcap is None else round(mcap, 0),
        "payout": None if payout is None else round(payout, 1), "payout_base": tipo,
        "deuda": None if (deuda is None or financiera) else round(deuda, 2),
        "fcf_anios": fcf_pos, "dilucion_5a": None if dilucion is None else round(dilucion, 1),
        "recompra_efectivo": None if recompra is None else round(recompra, 1),
        "recompra_rentab": None if bb_yield is None else round(bb_yield, 2),
        "veto": veto, "aviso": aviso,
    }


# --------------------------------------------------------------------------- #
# 4. Puntuación
# --------------------------------------------------------------------------- #

def _triangulo(y: float | None) -> float | None:
    """Rentabilidad por dividendo: 3-6 % es lo mejor; muy bajo no aporta y muy alto huele a trampa."""
    if y is None:
        return None
    if y <= 1.0:
        return 0.2
    if y < 3.0:
        return 0.2 + 0.8 * (y - 1.0) / 2.0
    if y <= 6.0:
        return 1.0
    return max(0.2, 1.0 - 0.8 * (y - 6.0) / 3.0)


def puntuar(an: dict, emp: dict | None, rend: dict, caida: float | None, reglas: ReglasDividendos):
    """→ (puntos 0–100 | None, partes {nombre: 0–100 | None}, cobertura de datos)."""
    cagr = an.get("cagr_5a") if an.get("cagr_5a") is not None else an.get("cagr_3a")
    dividendo, _ = media_ponderada([
        (esc(an.get("anios_sin_recorte"), 5, 10), 0.35),
        (esc(an.get("subidas_5a"), 0, 4), 0.25),
        (esc(cagr, 0, 8), 0.40),
    ])
    if emp is None:
        seguridad = accionista = None
    else:
        seguridad, _ = media_ponderada([
            (esc(emp.get("payout"), 90, 30), 0.50),
            (None if emp.get("financiera") else esc(emp.get("deuda"), 3.5, 0.5), 0.30),
            (None if emp.get("financiera") else esc(emp.get("fcf_anios"), 3, 5), 0.20),
        ])
        accionista, _ = media_ponderada([
            (esc(emp.get("dilucion_5a"), 3, -10), 0.70),
            (esc(emp.get("recompra_rentab"), 0, 3), 0.30),
        ])
    nivel = _triangulo(an.get("rentab_div"))
    r1 = (rend.get("1a") or {}).get("total")
    precio, _ = media_ponderada([
        (esc(r1, -30, 15), 0.5),
        (esc(None if caida is None else caida * 100.0, -45, 0), 0.5),
    ])
    pesos = reglas.pesos
    partes = [(dividendo, pesos[0]), (seguridad, pesos[1]), (accionista, pesos[2]), (nivel, pesos[3]), (precio, pesos[4])]
    media, cob = media_ponderada(partes)

    def p100(x):
        return None if x is None else round(100.0 * x, 1)

    nombres = dict(dividendo=dividendo, seguridad=seguridad, accionista=accionista, nivel=nivel, precio=precio)
    return (None if media is None else round(100.0 * media, 1)), {k: p100(v) for k, v in nombres.items()}, cob


# --------------------------------------------------------------------------- #
# 5. Puertas de dividendo y construcción de la sección
# --------------------------------------------------------------------------- #

def categoria(motivo: str) -> str:
    """Motivo de descarte sin cifras, para contarlos: «deuda neta/EBITDA 4.2x» → «deuda neta/EBITDA»."""
    return re.split(r"[\d(+\-]", motivo)[0].strip() or motivo


def puerta_dividendo(an: dict, caida: float | None, reglas: ReglasDividendos):
    """(pasa, motivo si no)."""
    if not an.get("paga"):
        return False, "no paga dividendo regular"
    if not an.get("pagando"):
        return False, "ha dejado de pagar"
    if an["anios_pagando"] < reglas.anios_pagando_min:
        return False, f"solo {an['anios_pagando']:.1f} años pagando"
    if an["recortes_5a"] > 0 or an["anios_sin_recorte"] < reglas.sin_recorte_min:
        return False, "ha recortado el dividendo en los últimos 5 años"
    y = an.get("rentab_div")
    if y is None:
        return False, "sin precio para calcular la rentabilidad por dividendo"
    if y < reglas.yield_min:
        return False, f"dividendo irrelevante ({y:.1f} %)"
    if y > reglas.yield_max:
        return False, f"rentabilidad por dividendo sospechosa ({y:.1f} %)"
    if caida is not None and caida < -reglas.caida_max:
        return False, f"cae un {abs(caida) * 100:.0f} % desde su máximo anual"
    return True, ""


def razones(an: dict, emp: dict, rend: dict, bench: dict) -> list[str]:
    out: list[str] = []
    r5 = rend.get("5a") or rend.get("3a")
    lab = "5 años" if rend.get("5a") else "3 años"
    n_sub = an["subidas_5a"]
    out.append(f"Lleva {an['anios_pagando']:.0f} años pagando dividendo y {an['anios_sin_recorte']:.0f} sin recortarlo"
               + (f"; lo ha subido {n_sub} {'vez' if n_sub == 1 else 'veces'} en 5 años." if n_sub else "; lo mantiene estable."))
    cg = an.get("cagr_5a") if an.get("cagr_5a") is not None else an.get("cagr_3a")
    if cg is not None:
        out.append(f"El dividendo por acción crece un {cg:+.1f} % anual.")
    if emp.get("payout") is not None:
        out.append(f"Reparte el {emp['payout']:.0f} % de su {emp['payout_base']}.")
    if emp.get("dilucion_5a") is not None:
        if emp["dilucion_5a"] < -1:
            out.append(f"Recompra acciones: hay un {abs(emp['dilucion_5a']):.0f} % menos que hace 5 años.")
        elif emp["dilucion_5a"] <= 1:
            out.append("El número de acciones apenas ha cambiado en 5 años.")
        else:
            out.append(f"Ha emitido un {emp['dilucion_5a']:.0f} % más de acciones en 5 años.")
    if r5:
        b = (bench.get("rend") or {}).get("5a" if rend.get("5a") else "3a") if bench else None
        t = f"En {lab}: precio {r5['precio']:+.1f} %/año + dividendo {r5['div']:+.1f} = {r5['total']:+.1f} %/año"
        if b:
            t += f" (Russell 2000: {b['total']:+.1f} %)"
        out.append(t + ".")
    return out


def avisos_tarjeta(an: dict, emp: dict, rend: dict, caida: float | None, reglas: ReglasDividendos) -> list[str]:
    out = list(emp.get("aviso") or [])
    y = an.get("rentab_div")
    if y is not None and y > 6.0:
        out.append(f"Rentabilidad por dividendo alta ({y:.1f} %): comprueba que no sea el precio hundiéndose.")
    if caida is not None and caida < -0.30:
        out.append(f"Cotiza un {abs(caida) * 100:.0f} % por debajo de su máximo de 52 semanas.")
    r5 = rend.get("5a")
    if r5 and r5["precio"] < 0 < r5["div"]:
        out.append("En 5 años el precio ha bajado y el dividendo solo ha compensado en parte.")
    if an.get("especiales"):
        out.append(f"Ha pagado {an['especiales']} dividendo(s) especial(es); no cuentan en las cifras.")
    return out


def _fila_ref(rend: dict) -> dict:
    return {"ticker": IWM, "nombre": "Russell 2000 (IWM)", "rend": rend}


def generar_seccion(
    universo: list[dict],
    datos: dict[str, dict],
    hoy,
    *,
    f_empresa=None,                # (ticker, precio) -> (Fundamentals, Metrics) | None
    reglas: ReglasDividendos | None = None,
    fuente: str = "",
) -> dict:
    """`universo`: filas {ticker, nombre, sector}. `datos[ticker]` = {close, adj, div}
    (Series); debe incluir IWM como referencia."""
    r = reglas or ReglasDividendos()
    hoy = pd.Timestamp(hoy)
    avisos: list[str] = []
    bench = datos.get(IWM)
    ref_rend = rendimientos(bench["close"], bench["adj"], hoy) if bench else {}
    ref = _fila_ref(ref_rend)

    motivos: dict[str, int] = {}
    base = []
    estado: dict[str, dict] = {}       # todas las que pagan: precio y si han recortado o parado (para vigilar tu cartera)
    n_con_datos = n_paga = 0
    for fila in universo:
        t = fila["ticker"]
        d = datos.get(t)
        if not d or d.get("close") is None or len(d["close"].dropna()) < 250:
            continue
        n_con_datos += 1
        close = d["close"].dropna()
        precio = float(close.iloc[-1])
        an = analizar_dividendo(d.get("div"), precio, hoy, r)
        if an.get("paga"):
            n_paga += 1
            estado[t] = {"n": fila.get("nombre") or "", "s": fila.get("sector") or "", "p": round(precio, 2),
                         "f": "parado" if not an["pagando"] else ("recorte" if an["recortes_5a"] > 0 else "ok")}
        caida = caida_52s(close)
        ok, motivo = puerta_dividendo(an, caida, r)
        if not ok:
            motivos[categoria(motivo)] = motivos.get(categoria(motivo), 0) + 1
            continue
        rend = rendimientos(close, d["adj"], hoy)
        pts, partes, cob = puntuar(an, None, rend, caida, r)
        base.append({"fila": fila, "an": an, "rend": rend, "caida": caida, "precio": precio, "pre": pts or 0.0})

    base.sort(key=lambda x: -x["pre"])
    n_base = len(base)
    revisar = base[:r.max_fundamentos]
    if n_base > r.max_fundamentos:
        avisos.append(f"Pasaron la puerta del dividendo {n_base} empresas; se revisan en la SEC las {r.max_fundamentos} mejores.")

    elegibles, vetadas_emp = [], 0
    sin_datos = 0
    for x in revisar:
        t = x["fila"]["ticker"]
        res = None
        if f_empresa is not None:
            try:
                res = f_empresa(t, x["precio"])
            except Exception as e:  # noqa: BLE001
                log.info("Sin fundamentales de %s: %s", t, e)
        if res is None:
            sin_datos += 1
            continue
        f, m = res
        emp = evaluar_empresa(f, m, x["an"].get("dps"), r, x["an"].get("especiales", 0))
        if emp["veto"]:
            vetadas_emp += 1
            for v in emp["veto"]:
                motivos[categoria(v)] = motivos.get(categoria(v), 0) + 1
            continue
        pts, partes, cob = puntuar(x["an"], emp, x["rend"], x["caida"], r)
        if pts is None or cob < r.cobertura_datos_min:
            continue
        elegibles.append({**x, "emp": emp, "puntos": pts, "partes": partes})

    elegibles.sort(key=lambda x: -x["puntos"])
    if sin_datos:
        avisos.append(f"{sin_datos} empresas sin datos de la SEC utilizables (emisores extranjeros, datos incompletos…).")

    def tarjeta(x):
        fila, an, emp = x["fila"], x["an"], x["emp"]
        return {
            "ticker": fila["ticker"], "nombre": fila.get("nombre") or "", "sector": fila.get("sector") or "",
            "precio": round(x["precio"], 2), "cap": emp["cap"],
            "puntos": x["puntos"], "partes": x["partes"],
            "dividendo": {k: an.get(k) for k in (
                "dps", "rentab_div", "frecuencia", "anios_pagando", "anios_sin_recorte", "subidas_5a",
                "cagr_5a", "cagr_3a", "especiales", "ultimo_pago")},
            "empresa": {k: emp.get(k) for k in (
                "payout", "payout_base", "deuda", "fcf_anios", "dilucion_5a", "recompra_efectivo",
                "recompra_rentab", "financiera")},
            "rend": x["rend"], "caida_52s": None if x["caida"] is None else round(x["caida"], 3),
            "razones": razones(an, emp, x["rend"], ref),
            "avisos": avisos_tarjeta(an, emp, x["rend"], x["caida"], r),
        }

    # selección con tope por sector
    cand, espera, por_sector = [], [], {}
    for x in elegibles:
        sec = (x["fila"].get("sector") or "").strip() or "?"
        if len(cand) < r.n and por_sector.get(sec, 0) < r.n_max_sector:
            cand.append(tarjeta(x))
            por_sector[sec] = por_sector.get(sec, 0) + 1
        elif len(espera) < 12:
            espera.append(tarjeta(x))

    # contexto: cómo le fue en 5 años a todo el grupo elegible (mediana) frente al Russell 2000
    def mediana(clave, h):
        v = [x["rend"][h][clave] for x in elegibles if h in x["rend"]]
        return round(float(np.median(v)), 2) if len(v) >= 10 else None

    grupo = {h: {"precio": mediana("precio", h), "total": mediana("total", h), "n": sum(1 for x in elegibles if h in x["rend"])}
             for h in ("3a", "5a")}

    return {
        "version": 1, "nombre": INFO["nombre"], "horizonte": INFO["horizonte"], "etiqueta": INFO["etiqueta"],
        "descripcion": INFO["descripcion"], "criterios": criterios(r), "fuente": fuente,
        "resumen": {"n_universo": len(universo), "n_con_datos": n_con_datos, "n_pagan": n_paga,
                    "n_puerta_dividendo": n_base, "n_revisadas": len(revisar), "n_elegibles": len(elegibles)},
        "descartes": dict(sorted(motivos.items(), key=lambda kv: -kv[1])[:12]),
        "referencia": ref, "grupo": grupo,
        "candidatas": cand, "en_espera": espera,
        "universo": sorted(x["fila"]["ticker"] for x in elegibles),
        "estado": estado,
        "avisos": avisos,
    }


def limpiar_json(x):
    """Tipos de numpy → Python y NaN/inf → None, para que el informe sea JSON estricto
    (Postgres rechaza NaN)."""
    if isinstance(x, dict):
        return {str(k): limpiar_json(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [limpiar_json(v) for v in x]
    if isinstance(x, (np.bool_, bool)):
        return bool(x)
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating, float)):
        v = float(x)
        return v if np.isfinite(v) else None
    return x


def volcar_estado(inf: dict) -> int:
    """Mueve `dividendos.estado` a `inf["empresas"]` (campo `dv`: ok | recorte | parado) para que
    el Vigía pueda valorar y vigilar las acciones de dividendos que tengas, aunque ya no sean
    candidatas. Devuelve cuántas empresas se añadieron o marcaron."""
    sec = inf.get("dividendos")
    if not isinstance(sec, dict):
        return 0
    estado = sec.pop("estado", None) or {}
    emp = inf.setdefault("empresas", {})
    por_yahoo = {_norm(k): k for k in emp}
    cand = {c["ticker"] for c in sec.get("candidatas", [])}
    n = 0
    for t, x in estado.items():
        clave = por_yahoo.get(t, t)
        e = emp.get(clave)
        if e is None:
            e = emp[clave] = {"n": x["n"], "v": None, "sec": x["s"], "p": x["p"], "e": "normal"}
        e["dv"] = x["f"]
        if t in cand:
            e["pk"] = sorted(set(e.get("pk") or []) | {"dividendos"})
        n += 1
    return n


def criterios(r: ReglasDividendos) -> list[str]:
    return [
        f"Small cap: entre {r.cap_min:,.0f} y {r.cap_max:,.0f} millones de dólares de capitalización.",
        f"Paga dividendo ahora y lleva al menos {r.anios_pagando_min:.0f} años pagando.",
        f"Ningún pago regular por debajo del anterior (más de un {r.tolerancia_recorte * 100:.0f} %) en los últimos 5 años; "
        "los pagos especiales no cuentan.",
        f"Rentabilidad por dividendo entre {r.yield_min} % y {r.yield_max:.0f} % (más es casi siempre una trampa).",
        f"La caja libre cubre el dividendo (reparte como mucho el {r.cobertura_max:.0f} %; aviso desde el {r.cobertura_aviso:.0f} %); "
        "en bancos, seguros y REITs se mide con beneficio o caja operativa.",
        f"Caja libre positiva al menos {r.fcf_anios_min} años, deuda neta/EBITDA ≤ {r.deuda_max}x y sin diluir "
        f"(≤ {r.dilucion_max:.0f} % más acciones en 5 años).",
        f"No cae más de un {r.caida_max * 100:.0f} % desde su máximo de 52 semanas.",
        f"Máx. {r.n} empresas y {r.n_max_sector} por sector. Se ordenan por constancia y crecimiento del dividendo, "
        "seguridad, recompras, nivel del dividendo y salud del precio.",
    ]


# --------------------------------------------------------------------------- #
# 6. Datos reales: universo, precios, dividendos, SEC
# --------------------------------------------------------------------------- #

def _norm(x) -> str:
    return str(x).strip().upper().replace(".", "-").replace(" ", "")


def _ticker_valido(t: str) -> bool:
    return bool(t) and t != "-" and t.replace("-", "").isalpha() and len(t) <= 6


def _tabla_ishares() -> pd.DataFrame:
    """Tabla de posiciones del ETF IWM desde iShares. La URL de descarga cambia de vez en
    cuando: si la directa devuelve la página del producto en vez del CSV, se busca ahí el
    enlace actual de «descargar posiciones»."""
    import html as _html
    import requests
    cab = {**UA, "Accept": "text/csv,text/html;q=0.9,*/*;q=0.8", "Referer": URL_IWM_PRODUCTO}

    def pedir(url: str) -> str:
        r = requests.get(url, headers=cab, timeout=90)
        r.raise_for_status()
        return r.text.lstrip("\ufeff")

    def tabla(texto: str):
        lineas = texto.splitlines()
        ini = next((i for i, l in enumerate(lineas) if l.replace('"', "").startswith("Ticker,")), None)
        if ini is None:
            return None
        df = pd.read_csv(io.StringIO("\n".join(lineas[ini:])), on_bad_lines="skip")
        df.columns = [str(c).strip() for c in df.columns]
        if "Asset Class" in df.columns:
            df = df[df["Asset Class"].astype(str).str.strip().str.lower() == "equity"]
        return df

    texto = pedir(URL_IWM)
    df = tabla(texto)
    if df is not None and len(df):
        return df
    base = "https://www.ishares.com"
    candidatos: list[str] = []
    for fuente in (texto, None):
        if fuente is None:
            fuente = pedir(URL_IWM_PRODUCTO)
        for m in re.finditer(r'href="([^"]*\.ajax\?[^"]*fileType=csv[^"]*)"', fuente):
            u = _html.unescape(m.group(1))
            u = u if u.startswith("http") else base + u
            if "IWM" in u.upper() and u not in candidatos and u != URL_IWM:
                candidatos.append(u)
        if candidatos:
            break
    for u in candidatos[:3]:
        df = tabla(pedir(u))
        if df is not None and len(df):
            print(f"  (enlace de descarga encontrado en la página del producto)")
            return df
    raise ValueError(f"iShares no devolvió el CSV (respuesta de {len(texto)} bytes; enlaces probados: {len(candidatos)})")



def _lista_wikipedia(url: str, minimo: int) -> list[dict]:
    """Tabla de componentes de un índice de Wikipedia → filas {ticker, nombre, sector}."""
    import requests
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    for t in pd.read_html(io.StringIO(r.text)):
        col = next((c for c in t.columns if str(c).strip().lower() in ("symbol", "ticker symbol", "ticker")), None)
        if col is None or len(t) < minimo:
            continue
        nom = next((c for c in t.columns if str(c).strip().lower() in ("security", "company")), None)
        sec = next((c for c in t.columns if "gics sector" in str(c).strip().lower()), None)
        filas, vistos = [], set()
        for _, row in t.iterrows():
            tk = _norm(row[col])
            if not _ticker_valido(tk) or tk in vistos:
                continue
            vistos.add(tk)
            filas.append({"ticker": tk, "nombre": str(row[nom]) if nom else "", "sector": str(row[sec]) if sec else ""})
        if len(filas) >= minimo:
            return filas
    raise ValueError("no se encontró la tabla de componentes")


def universo_russell2000() -> tuple[list[dict], str]:
    """Componentes del ETF IWM (iShares). Si no se puede, S&P SmallCap 600 (Wikipedia).
    Devuelve (filas {ticker, nombre, sector}, fuente)."""
    import requests
    try:
        df = _tabla_ishares()
        filas, vistos = [], set()
        for _, row in df.iterrows():
            t = _norm(row.get("Ticker"))
            if not _ticker_valido(t) or t in vistos:
                continue
            vistos.add(t)
            filas.append({"ticker": t, "nombre": str(row.get("Name") or "").strip().title(),
                          "sector": str(row.get("Sector") or "").strip()})
        if len(filas) >= 800:
            print(f"Russell 2000: {len(filas)} empresas (iShares IWM).")
            return filas, "Russell 2000 (componentes de IWM)"
        print(f"Aviso: el fichero de iShares trae solo {len(filas)} empresas; se prueba con el S&P 600.")
    except Exception as e:  # noqa: BLE001
        print(f"Aviso: no se pudo leer la lista de IWM en iShares ({type(e).__name__}: {e}); se prueba con el S&P 600.")
    filas, vistos, partes = [], set(), []
    for nombre, url, minimo in (("S&P 600", URL_SP600, 400), ("S&P 400", URL_SP400, 300)):
        try:
            nuevas = _lista_wikipedia(url, minimo)
        except Exception as e:  # noqa: BLE001
            print(f"Aviso: no se pudo leer el {nombre} ({type(e).__name__}: {e}).")
            continue
        n0 = len(filas)
        for f in nuevas:
            if f["ticker"] not in vistos:
                vistos.add(f["ticker"])
                filas.append(f)
        partes.append(nombre)
        print(f"{nombre}: {len(filas) - n0} empresas (Wikipedia).")
    if len(filas) >= 400:
        print(f"Lista final: {len(filas)} empresas de {' + '.join(partes)} (sustituto del Russell 2000; el filtro de tamaño se aplica después).")
        return filas, f"{' + '.join(partes)} (sustituto del Russell 2000)"
    raise RuntimeError("No se pudo obtener ninguna lista de small caps (iShares ni Wikipedia).")


def _extraer(data: pd.DataFrame, tickers: list[str]) -> dict[str, dict]:
    salida: dict[str, dict] = {}
    if data is None or len(data) == 0:
        return salida
    multi = isinstance(data.columns, pd.MultiIndex)
    nivel0 = set(data.columns.get_level_values(0)) if multi else set()
    for t in tickers:
        try:
            if multi:
                sub = data[t] if t in nivel0 else None
                if sub is None:
                    continue
            elif len(tickers) == 1:
                sub = data
            else:
                continue
            if "Close" not in sub.columns:
                continue
            close = sub["Close"].dropna()
            if close.empty:
                continue
            adj = sub["Adj Close"].dropna() if "Adj Close" in sub.columns else close
            div = sub["Dividends"] if "Dividends" in sub.columns else pd.Series(dtype=float)
            div = div.dropna()
            div = div[div > 0]
            for s in (close, adj, div):
                if getattr(s.index, "tz", None) is not None:
                    s.index = s.index.tz_localize(None)
            salida[t] = {"close": close, "adj": adj, "div": div}
        except (KeyError, ValueError):
            continue
    return salida


def descargar_dividendos(tickers: list[str], desde: str, lote: int = 40, pausa: float = 2.5,
                         reintentos: int = 3) -> dict[str, dict]:
    import yfinance as yf
    datos: dict[str, dict] = {}
    for i in range(0, len(tickers), lote):
        parte = tickers[i:i + lote]
        for intento in range(1, reintentos + 1):
            try:
                data = yf.download(parte, start=desde, auto_adjust=False, actions=True,
                                   progress=False, group_by="ticker", threads=False)
                enc = _extraer(data, parte)
                datos.update(enc)
                if enc or intento == reintentos:
                    break
            except Exception as e:  # noqa: BLE001
                log.warning("Lote %d intento %d/%d falló: %s", i // lote + 1, intento, reintentos, e)
            time.sleep(pausa * intento)
        if (i // lote) % 5 == 4 or i + lote >= len(tickers):
            print(f"  descargados {min(i + lote, len(tickers))}/{len(tickers)} (con datos: {len(datos)})")
        time.sleep(pausa)
    return datos


def fundamentos_sec():
    """Devuelve f_empresa(ticker, precio) que usa la SEC (sin Finnhub: el precio ya lo
    tenemos de Yahoo y la capitalización sale de precio × acciones)."""
    from providers import SecEdgarProvider
    from quant_filter import compute_metrics
    prov = SecEdgarProvider(years=5)

    def f_empresa(ticker: str, precio: float):
        f = prov.fetch(ticker)
        f.price = precio
        if f.shares_diluted:
            f.market_cap = precio * f.shares_diluted[-1]
        return f, compute_metrics(f)

    return f_empresa


def generar(hoy: date | None = None, *, max_empresas: int = 0, reglas: ReglasDividendos | None = None,
            con_sec: bool = True) -> dict:
    """Todo el proceso con datos reales. Lanza excepción si Yahoo no responde."""
    hoy = hoy or date.today()
    universo, fuente = universo_russell2000()
    if max_empresas:
        universo = universo[:max_empresas]
    tickers = sorted({f["ticker"] for f in universo} | {IWM})
    print(f"Descargando precios y dividendos de {len(tickers)} empresas...")
    datos = descargar_dividendos(tickers, f"{hoy.year - 15}-01-01")
    if IWM not in datos:
        raise RuntimeError("No se pudo descargar IWM: Yahoo Finance está bloqueando o caído.")
    cobertura = (len(datos) - 1) / max(1, len(universo))
    print(f"Cobertura de precios: {cobertura:.0%}")
    if cobertura < 0.6:
        raise RuntimeError(f"Solo se descargó el {cobertura:.0%} de las empresas: resultado poco fiable.")
    return limpiar_json(generar_seccion(universo, datos, hoy, f_empresa=fundamentos_sec() if con_sec else None,
                                        reglas=reglas, fuente=fuente))


# --------------------------------------------------------------------------- #
# 7. Informe en texto
# --------------------------------------------------------------------------- #

def _n(x, dec=1, suf=""):
    return "—" if x is None else f"{x:.{dec}f}{suf}"


def informe_md(sec: dict) -> str:
    L: list[str] = []
    A = L.append
    r = sec["resumen"]
    A(f"# {sec['nombre']}\n")
    A(f"_{sec['descripcion']}_\n")
    A(f"Universo: {sec['fuente']} · {r['n_universo']} empresas · {r['n_con_datos']} con datos · {r['n_pagan']} pagan dividendo regular · "
      f"{r['n_puerta_dividendo']} superan la puerta del dividendo · {r['n_revisadas']} revisadas en la SEC · "
      f"**{r['n_elegibles']} elegibles**.\n")
    A("**Criterios**\n")
    for c in sec["criterios"]:
        A(f"- {c}")
    A("")
    ref = (sec.get("referencia") or {}).get("rend") or {}
    gr = sec.get("grupo") or {}

    def fila(nombre, rend, h):
        x = (rend or {}).get(h)
        return f"{x['precio']:+.1f} % + {x['div']:+.1f} = **{x['total']:+.1f} %**" if x else "—"

    A("**Rentabilidad anual = precio + dividendo** (medias anuales)\n")
    A("| | 3 años | 5 años |")
    A("|---|---|---|")
    A(f"| Russell 2000 (IWM) | {fila('', ref, '3a')} | {fila('', ref, '5a')} |")
    g3, g5 = gr.get("3a") or {}, gr.get("5a") or {}
    A(f"| Mediana de las {r['n_elegibles']} elegibles | "
      f"{_n(g3.get('precio'), 1, ' %')} precio → **{_n(g3.get('total'), 1, ' %')}** total | "
      f"{_n(g5.get('precio'), 1, ' %')} precio → **{_n(g5.get('total'), 1, ' %')}** total |")
    A("\n_Ojo: son empresas que hoy siguen en el índice (supervivientes) y que ya han pasado el filtro; "
      "dicen lo que pasó, no lo que pasará._\n")
    A("## Candidatas\n")
    A("| # | Empresa | Precio $ | Cap. M$ | Div. $/acc | Rent. div. | Crec. div. | Años sin recorte | Reparte | Acciones 5a | 5a: precio + div = total | Pts |")
    A("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|")
    for k, c in enumerate(sec["candidatas"], 1):
        d, e = c["dividendo"], c["empresa"]
        r5 = (c["rend"] or {}).get("5a") or (c["rend"] or {}).get("3a")
        cg = d.get("cagr_5a") if d.get("cagr_5a") is not None else d.get("cagr_3a")
        A(f"| {k} | **{c['ticker']}** {c['nombre'][:28]} | {c['precio']:.2f} | {_n(c['cap'], 0)} | {_n(d['dps'], 2)} | "
          f"{_n(d['rentab_div'], 1, ' %')} | {_n(cg, 1, ' %')} | {_n(d['anios_sin_recorte'], 0)} | "
          f"{_n(e['payout'], 0, ' %')} | {_n(e['dilucion_5a'], 0, ' %')} | "
          f"{('%+.1f + %+.1f = %+.1f' % (r5['precio'], r5['div'], r5['total'])) if r5 else '—'} | {c['puntos']:.0f} |")
    A("")
    for c in sec["candidatas"]:
        A(f"### {c['ticker']} — {c['nombre']} ({c['sector']})")
        for t in c["razones"]:
            A(f"- {t}")
        for t in c["avisos"]:
            A(f"- ⚠️ {t}")
        A("")
    if sec["en_espera"]:
        A("**En espera:** " + ", ".join(f"{c['ticker']} ({c['puntos']:.0f})" for c in sec["en_espera"]) + "\n")
    if sec.get("descartes"):
        A("**Por qué se descartó el resto:** " + "; ".join(f"{k} ({v})" for k, v in sec["descartes"].items()) + "\n")
    for a in sec.get("avisos", []):
        A(f"_Aviso: {a}_")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="Small caps con dividendo que no se recorta.")
    ap.add_argument("--salida", default="resultados_dividendos", help="carpeta de salida")
    ap.add_argument("--max-empresas", type=int, default=0, help="solo para pruebas")
    ap.add_argument("--sin-sec", action="store_true", help="no consultar la SEC (solo dividendos y precio)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    from pathlib import Path
    sec = generar(max_empresas=args.max_empresas, con_sec=not args.sin_sec)
    Path(args.salida).mkdir(parents=True, exist_ok=True)
    md = informe_md(sec)
    Path(args.salida, "informe.md").write_text(md, encoding="utf-8")
    Path(args.salida, "dividendos.json").write_text(json.dumps(sec, ensure_ascii=False), encoding="utf-8")
    print("\n" + md)


if __name__ == "__main__":
    main()
