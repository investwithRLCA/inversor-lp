"""
Recomendador mensual — la parte «de verdad» del módulo mensual.

    python -m mensual.recomendador                       # informe completo (con IA)
    python -m mensual.recomendador --sin-ia --sin-supabase --salida informe.json

Una vez al mes (GitHub Actions) y cuando tú quieras a mano:

  1. Toma TODAS las empresas que han pasado el cribado cuantitativo del bot
     (calidad, caja, solvencia: cientos, no solo las que tienen análisis de
     IA). Las que la IA ya analizó y rechazó (NO_INVERTIBLE, DESCARTE) se
     quitan; a las que no tienen análisis aún se les avisa de ello («solo
     cribado»).
  2. Descarga sus precios (Yahoo, gratis) y arma los paquetes que están ACTIVOS
     (`paquetes.ACTIVOS`). Las pruebas históricas (workflows «Prueba histórica de
     los paquetes» y «… del filtro Lynch», con el precio corregido por splits)
     dejaron solo uno:
       · corto   (1–3 años): baratas frente a su propio rango de 5 años.   [desactivado]
       · medio   (3–5 años): calidad a precio razonable.                    [desactivado]
       · largo   (5+ años):  compounders (rentabilidad, crecimiento, caja). [desactivado]
       · lynch_g Peter Lynch en grandes (> 5.000 M$).                       [desactivado]
       · lynch_p Peter Lynch en pequeñas (300–5.000 M$) + cribado.          [ACTIVO]
     Aparte, la sección de small caps con dividendo (`dividendos.py`).
  3. Pasa las finalistas por frenos que SOLO pueden quitar o avisar, nunca
     añadir:  (a) precio por encima del rango de valor de la tesis,
     (b) chequeo del último trimestre publicado (ingresos y margen),
     (c) solo si «corto» está activo: la IA lee los titulares y dice si la caída
     parece temporal o estructural (veta solo «estructural + confianza alta»).
  4. SEGUIMIENTO: calcula cuánto habrían rendido las elecciones de los
     informes anteriores frente al S&P 500 y al resto del universo.
  5. Guarda el informe en Supabase (tabla `mensual_informes`, migración 006)
     y escribe un resumen en Markdown para el Issue mensual.

LO QUE NO HACE, a propósito:
  · No sabe qué tienes en cartera ni cuánto dinero vas a meter: eso es privado,
    se queda en tu móvil (pestaña «Este mes» del Vigía) y allí se calcula cuánto
    comprar de cada una y qué revisar.
  · No manda vender por precio. El backtest mostró que las tres reglas de venta
    por precio probadas pierden frente al S&P 500; solo se marca como «muy
    cara» a título informativo. Se vende por tesis rota (decisión tuya).

La lógica de decisión es una función pura (`generar_informe`) que recibe los
datos y las funciones externas por parámetro, así se prueba sin red.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd

from . import paquetes as pq
from lynch import con_precio as lynch_con_precio
from . import seguimiento as sg
from . import autocorreccion as ac
from .estrategia import Params, decidir_ventas, elegir_candidatas, senales
from .paquetes import ReglasPaquetes

log = logging.getLogger(__name__)

BENCHMARK = "SPY"
ELEGIBLES = ("COMPRA_FUERTE", "COMPRA", "VIGILAR")      # análisis de IA favorable (nivel A)
RECHAZADAS = ("NO_INVERTIBLE", "DESCARTE")               # la IA ya las descartó: fuera
MAX_EN_ESPERA = 12                                       # cuántas «en espera» se listan por paquete
TESIS_ANTIGUA_DIAS = 200          # el bot refresca a los ~180; más allá, tesis vieja
DIAS_SPY = 780                    # ~3 años de cierres diarios del S&P 500 para el marcador
DIAS_PRECIO_VIEJO = 6             # si el último precio es más viejo, aviso


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #

def yahoo(ticker: str) -> str:
    """Formato de Yahoo: BRK.B → BRK-B."""
    return ticker.strip().upper().replace(".", "-")


def primera_frase(md: str | None, largo: int = 280) -> str:
    """Primera frase útil de un informe Markdown (sin cabeceras ni la línea de
    veredicto). Igual que `primeraFrase` del dashboard."""
    if not md:
        return ""
    t = md.strip()
    if t.startswith("```"):
        nl = t.find("\n")
        t = t[nl + 1:] if nl >= 0 else t
        t = t.rstrip("`").strip()
    t = re.sub(r"^#.*$", "", t, flags=re.M)
    for parrafo in re.split(r"\n\s*\n", t):
        p = parrafo.strip()
        if p and not p.startswith("**Veredicto"):
            return re.sub(r"\s+", " ", p.replace("**", ""))[:largo]
    return ""


def _num(x, dec: int = 4):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(x) else round(x, dec)


def _dias_desde(iso: str | None, hoy: date) -> int | None:
    if not iso:
        return None
    try:
        return (hoy - datetime.fromisoformat(iso.replace("Z", "+00:00")).date()).days
    except ValueError:
        return None


def _movimientos(serie: pd.Series) -> dict:
    """Variación a 1 y 3 meses y distancia al máximo de 52 semanas."""
    s = serie.dropna()
    if len(s) < 30:
        return {"ret_1m": None, "ret_3m": None, "desde_maximo_52s": None}
    ult = float(s.iloc[-1])

    def ret(n: int):
        return ult / float(s.iloc[-1 - n]) - 1.0 if len(s) > n else None

    return {"ret_1m": ret(21), "ret_3m": ret(63),
            "desde_maximo_52s": ult / float(s.iloc[-252:].max()) - 1.0}


# --------------------------------------------------------------------------- #
# Núcleo: puro, sin red
# --------------------------------------------------------------------------- #

def _tier(fila: dict):
    """Nivel de la empresa: 'A' = con análisis de IA favorable; 'B' = solo ha
    pasado el cribado cuantitativo (aún sin análisis de IA); None = no es
    elegible (la IA la descartó, o ya no pasa el cribado cuantitativo)."""
    if fila.get("pasa_cribado") is False:
        return None
    v = fila.get("veredicto_final")
    if v in ELEGIBLES:
        return "A"
    if v in RECHAZADAS:
        return None
    return "B" if not v else None


def _momento(acciones: pd.DataFrame) -> np.ndarray:
    """Rendimiento de los últimos 12 meses SIN el último (hace 252 → hace 21
    sesiones), la medida clásica de momento que evita comprar lo que cae en
    picado. NaN si no hay historia."""
    a = acciones.to_numpy(dtype=float)
    if len(a) < 253:
        return np.full(a.shape[1], np.nan)
    fin, ini = a[-22], a[-253]
    with np.errstate(invalid="ignore", divide="ignore"):
        m = fin / ini - 1.0
    m[~(np.isfinite(fin) & np.isfinite(ini) & (ini > 0))] = np.nan
    return m


def generar_informe(
    tesis: list[dict],
    precios: pd.DataFrame,
    p: Params,
    hoy: date,
    *,
    usar_ia: bool = True,
    max_ia: int = 8,
    f_salud=None,          # (ticker) -> dict | None
    f_titulares=None,      # (ticker) -> list[dict]
    f_ia=None,             # (ctx: dict) -> dict
    f_tesis_texto=None,    # (ticker) -> str | None
    reglas: ReglasPaquetes | None = None,
    previos: list[dict] | None = None,   # filas de mensual_informes, para el seguimiento
    activos: tuple[str, ...] | None = None,   # paquetes que se ofrecen (por defecto, los de `paquetes.ACTIVOS`)
    validacion: dict | None = None,           # revalidación histórica (autocorreccion.json)
    autocorr_previo: dict | None = None,      # bloque «autocorreccion» del informe anterior
    autocorregir: bool = True,
) -> dict:
    """Construye el informe mensual con sus tres paquetes.

    `tesis` son las empresas del universo: filas con ticker, name, sector,
    veredicto_final (None si aún no tiene análisis de IA), precio_entrada,
    valor_min, valor_max, nota_fundamental, confianza, analizado_el y, para
    los paquetes medio y largo, `metricas` (las del cribado cuantitativo),
    `es_financiera` y `pasa_cribado`. `precios` tiene una columna por ticker
    de Yahoo más SPY."""
    from .explicacion import veta

    reglas = dataclasses.replace(reglas or ReglasPaquetes(), n_corto=p.n_max, n_max_sector=p.n_max_sector)
    activos = tuple(activos if activos is not None else pq.ACTIVOS)
    avisos: list[str] = []
    acciones = precios.drop(columns=[BENCHMARK], errors="ignore")
    if acciones.empty:
        raise ValueError("No hay precios de ninguna empresa.")
    fecha_datos = acciones.index[-1]
    if (pd.Timestamp(hoy) - fecha_datos).days > DIAS_PRECIO_VIEJO:
        avisos.append(f"Los precios más recientes son del {fecha_datos:%d/%m/%Y}: "
                      "mercado cerrado varios días o Yahoo con retraso.")

    # --- autocorrección: revalidación histórica, freno por resultados reales y peso de la IA --- #
    seg_previo = None
    if previos:
        try:
            seg_previo = sg.calcular(previos, precios, fecha_datos)
        except Exception as e:  # noqa: BLE001
            log.exception("Seguimiento no disponible")
            avisos.append(f"No se pudo calcular el seguimiento de informes anteriores: {e}")
    bloque_ac = None
    excluir_v: tuple[str, ...] = ()
    if autocorregir:
        activos, reglas, excluir_v, bloque_ac = ac.decidir(activos, reglas, seg_previo, validacion,
                                                           autocorr_previo, f"{hoy:%Y-%m-%d}")
        avisos.extend(bloque_ac["notas"])

    sen = senales(acciones, fecha_datos, p)
    mom = _momento(acciones)
    cols = list(acciones.columns)
    pos = {c: k for k, c in enumerate(cols)}

    # Datos de la tesis por columna de precios
    por_col: dict[str, dict] = {}
    for fila in tesis:
        y = yahoo(fila["ticker"])
        if y in pos:
            por_col[y] = fila

    sectores = sorted({(f.get("sector") or "").strip() for f in tesis if (f.get("sector") or "").strip()})
    cod = {s: k for k, s in enumerate(sectores)}
    sector_col = np.array(
        [cod.get(((por_col.get(c) or {}).get("sector") or "").strip(), -1) for c in cols], dtype=int)

    desc = sen["descuento"].to_numpy(dtype=float).copy()
    pct = sen["percentil"].to_numpy(dtype=float)
    reb = sen["rebote"].to_numpy(dtype=float)
    tier_col = [_tier(por_col[c]) if c in por_col else None for c in cols]
    elegible = np.array([t is not None for t in tier_col], dtype=bool)
    con_senal = elegible & np.isfinite(desc)

    def base(c: str) -> dict:
        f = por_col[c]
        k = pos[c]
        precio = float(sen["precio"].iloc[k])
        vmin, vmax = _num(f.get("valor_min"), 2), _num(f.get("valor_max"), 2)
        pe = _num(f.get("precio_entrada"), 2)
        mid = (vmin + vmax) / 2 if vmin and vmax else None
        return {
            "ticker": f["ticker"], "nombre": f.get("name") or "", "sector": f.get("sector") or "",
            "veredicto": f.get("veredicto_final") or "SIN_TESIS", "tier": tier_col[k],
            "precio": _num(precio, 2), "mediana": _num(sen["mediana"].iloc[k], 2),
            "descuento": _num(desc[k]), "percentil": _num(pct[k]), "rebote": _num(reb[k]),
            "momento": _num(mom[k], 3),
            "precio_entrada": pe, "valor_min": vmin, "valor_max": vmax,
            "margen_valor": _num(1 - precio / mid) if mid else None,
            "doble_descuento": bool(pe and precio <= pe),
            "nota": _num(f.get("nota_fundamental"), 1), "confianza_tesis": f.get("confianza"),
            "tesis_dias": _dias_desde(f.get("analizado_el"), hoy),
            "quant": _num(f.get("quant_score"), 1),
            "v_ant": f.get("veredicto_anterior"), "v_cambio": f.get("veredicto_cambio_el"),
        }

    def recorrer(paquete, orden, n_max, evals=None, post=None, max_sector=None):
        """Recorre `orden` (mejor a peor) aplicando n_max, tope por sector,
        precio sobre el rango de valor de la tesis y el freno propio del
        paquete (`post`: devuelve el motivo del veto o None)."""
        aceptadas: list[dict] = []
        vetadas: list[dict] = []
        en_espera: list[dict] = []
        cuenta_sector: dict[int, int] = {}
        tope_sector = reglas.n_max_sector if max_sector is None else max_sector
        for k in orden:
            c = cols[int(k)]
            if c not in por_col:
                continue
            reg = base(c)
            reg["paquete"] = paquete
            if evals is not None:
                reg.update(evals[c])
            reg["tesis_antigua"] = bool(reg["tesis_dias"] is not None and reg["tesis_dias"] > TESIS_ANTIGUA_DIAS)

            if len(aceptadas) >= n_max:
                if len(en_espera) < MAX_EN_ESPERA:
                    en_espera.append({"ticker": reg["ticker"], "motivo": f"cumple, pero fuera de las {n_max} mejores"})
                continue
            s_cod = int(sector_col[int(k)])
            if tope_sector > 0 and s_cod >= 0 and cuenta_sector.get(s_cod, 0) >= tope_sector:
                if len(en_espera) < MAX_EN_ESPERA:
                    en_espera.append({"ticker": reg["ticker"],
                                      "motivo": f"cumple, pero ya hay {tope_sector} de su sector"})
                continue
            if reg["valor_max"] and reg["precio"] > reg["valor_max"]:
                vetadas.append({"ticker": reg["ticker"], "motivo":
                                f"precio {reg['precio']:.2f} por encima del rango de valor de su tesis "
                                f"(máx. {reg['valor_max']:.2f})"})
                continue
            motivo = post(reg, c) if post else None
            if motivo:
                vetadas.append({"ticker": reg["ticker"], "motivo": motivo})
                continue
            cuenta_sector[s_cod] = cuenta_sector.get(s_cod, 0) + 1
            aceptadas.append(reg)
        for reg in aceptadas:
            reg["peso"] = round(1.0 / len(aceptadas), 4)
            reg["avisos"] = _avisos_candidata(reg)
        return aceptadas, vetadas, en_espera

    # ===================== paquete CORTO: descuento con tesis ==================== #
    # Regla de precio SIN recorte (n_max y sector se aplican al recorrer, junto con los frenos)
    desc_elegibles = np.where(elegible, desc, np.nan)
    p_sin_recorte = dataclasses.replace(p, n_max=len(cols) or 1, n_max_sector=0)
    ordenadas = elegir_candidatas(desc_elegibles, pct, reb, p_sin_recorte, None)

    estado_ia = {"llamadas": 0, "agotada": False}

    def post_corto(reg, c):
        reg["salud"] = f_salud(reg["ticker"]) if f_salud else None      # chequeo trimestral (SEC, gratis)
        reg["ia"] = None
        if usar_ia and f_ia is not None:
            if estado_ia["llamadas"] < max_ia:
                estado_ia["llamadas"] += 1
                tit = f_titulares(reg["ticker"]) if f_titulares else []
                ctx = {**reg, **_movimientos(acciones[c]),
                       "tesis": f_tesis_texto(reg["ticker"]) if f_tesis_texto else None,
                       "titulares": tit}
                reg["ia"] = f_ia(ctx)
                reg["titulares"] = tit[:6]
                if veta(reg["ia"]):
                    return ("la IA ve un problema estructural con confianza alta: "
                            + (reg["ia"].get("causa") or ""))
            else:
                estado_ia["agotada"] = True
        return None

    if "corto" in activos:
        corto = recorrer("corto", ordenadas, p.n_max, post=post_corto)
    else:
        corto = ([], [], [])        # paquete desactivado por la prueba histórica (ver paquetes.py)

    # ===================== paquetes MEDIO y LARGO ================================ #
    def post_calidad(reg, c):
        reg["salud"] = f_salud(reg["ticker"]) if f_salud else None
        if reg.get("tier") == "A" and f_tesis_texto:
            reg["tesis"] = f_tesis_texto(reg["ticker"])
        return None

    resultado = {"corto": corto} if "corto" in activos else {}
    n_aptas = {"corto": len(ordenadas)} if "corto" in activos else {}
    for paquete in [x for x in ("medio", "largo") if x in activos]:
        evals: dict[str, dict] = {}
        for k, c in enumerate(cols):
            if not con_senal[k] or c not in por_col:
                continue
            f = por_col[c]
            ev = pq.evaluar(
                paquete, f.get("metricas"),
                {"percentil": pct[k], "descuento": desc[k], "mom": mom[k]},
                bool(f.get("es_financiera")), reglas)
            if ev is not None:
                evals[c] = ev
        orden = sorted(evals, key=lambda c: (-evals[c]["puntos"], c))
        n_aptas[paquete] = len(orden)
        resultado[paquete] = recorrer(paquete, [pos[c] for c in orden], reglas.n_de(paquete),
                                      evals=evals, post=post_calidad)

    # ===================== paquetes PETER LYNCH (grandes y pequeñas) ============= #
    def post_lynch(reg, c):
        if reg["veredicto"] in excluir_v:
            return ("la autocorrección deja fuera del reparto las "
                    + ("VIGILAR" if reg["veredicto"] == "VIGILAR" else "«sin IA aún»")
                    + ": en la realidad han rendido menos que las COMPRA")
        if reg["veredicto"] in RECHAZADAS:
            return f"la IA la descartó en su análisis ({reg['veredicto'].replace('_', ' ')})"
        reg["salud"] = f_salud(reg["ticker"]) if f_salud else None
        if reg["salud"] and reg["salud"].get("deterioro"):
            return "último trimestre con deterioro: " + "; ".join(reg["salud"].get("avisos") or [])
        if reg.get("tier") == "A" and f_tesis_texto:
            reg["tesis"] = f_tesis_texto(reg["ticker"])
        return None

    lynch_paqs = [x for x in ("lynch_g", "lynch_p") if x in activos]
    if lynch_paqs:
        evals_l: dict[str, dict[str, dict]] = {"lynch_g": {}, "lynch_p": {}}
        for k, c in enumerate(cols):
            f = por_col.get(c)
            if not f or not f.get("lynch"):
                continue
            if reglas.lynch_con_cribado and not f.get("pasa_cribado"):
                continue                     # la variante probada exige también el cribado de calidad
            precio = float(sen["precio"].iloc[k])
            if not np.isfinite(precio) or precio <= 0:
                continue
            ev = lynch_con_precio(f["lynch"], precio)
            if not ev or not ev["pasa"] or ev.get("cap") is None:
                continue
            grupo = "lynch_g" if ev["cap"] >= reglas.lynch_cap_grande else "lynch_p"
            evals_l[grupo][c] = {
                "puntos": ev["puntos"],
                "lynch": {kk: ev[kk] for kk in ("cat", "per", "peg", "peg_div", "peg_usado", "crec", "div", "cap", "de", "caja")},
                "razones": _razones_lynch(f["lynch"], ev),
            }
        for paquete in lynch_paqs:
            ev_p = evals_l[paquete]
            orden = sorted(ev_p, key=lambda c: (-ev_p[c]["puntos"], c))
            n_aptas[paquete] = len(orden)
            resultado[paquete] = recorrer(paquete, [pos[c] for c in orden], reglas.n_de(paquete),
                                          evals=ev_p, post=post_lynch, max_sector=reglas.sector_de(paquete))

    # ===================== GRANDES · calidad (> 5.000 M$) ======================== #
    if "calidad_g" in activos:
        ev_g: dict[str, dict] = {}
        for k, c in enumerate(cols):
            f = por_col.get(c)
            if not f or not f.get("pasa_cribado"):
                continue
            l = f.get("lynch") or {}
            precio = float(sen["precio"].iloc[k])
            px = l.get("px") or ((l.get("per") or 0) * (l.get("bpa") or 0)) or None
            cap0 = l.get("cap")
            if not px or not cap0:                       # respaldo: precio y capitalización de la ficha del cribado
                px, cap0 = f.get("snap_precio"), f.get("snap_cap")
            try:
                px, cap0 = float(px), float(cap0)
            except (TypeError, ValueError):
                continue
            if not np.isfinite(precio) or precio <= 0 or px <= 0 or cap0 <= 0:
                continue
            ratio = precio / px
            cap = cap0 * ratio
            if cap < reglas.lynch_cap_grande:
                continue
            m = f.get("metricas") or {}
            ev_ebit = m.get("ev_ebit")
            ev_ebit = ev_ebit * ratio if (ev_ebit and ev_ebit > 0) else None
            roic = m.get("roic_medio") if m.get("roic_medio") is not None else m.get("roic")
            ev_g[c] = {"grandes": {"cap": round(cap, 0), "ev_ebit": None if ev_ebit is None else round(ev_ebit, 1),
                                   "roic": None if roic is None else round(roic, 1), "nota": f.get("quant_score")},
                       "clave": pq.metricas_clave(m), "razones": _razones_grandes(m, ev_ebit, f.get("quant_score"))}
            ev_g[c]["clave"]["ev_ebit"] = ev_ebit
        var = reglas.grandes_variante
        if var == "calidad":
            for c in list(ev_g):
                nota = ev_g[c]["grandes"]["nota"]
                if nota is None:
                    del ev_g[c]
                else:
                    ev_g[c]["puntos"] = float(nota)
        else:
            for c in list(ev_g):
                if ev_g[c]["grandes"]["ev_ebit"] is None or (var == "magica" and ev_g[c]["grandes"]["roic"] is None):
                    del ev_g[c]
            if var == "calidad_precio":
                for c in ev_g:
                    ev_g[c]["puntos"] = round(100.0 / ev_g[c]["grandes"]["ev_ebit"], 2)
            else:                            # fórmula mágica: ranking de ROIC + ranking de rentabilidad del beneficio
                ts = list(ev_g)
                r_roic = pd.Series([ev_g[c]["grandes"]["roic"] for c in ts]).rank(ascending=False).to_numpy()
                r_ey = pd.Series([-ev_g[c]["grandes"]["ev_ebit"] for c in ts]).rank(ascending=False).to_numpy()
                n_t = max(1, len(ts))
                for c, a_, b_ in zip(ts, r_roic, r_ey):
                    ev_g[c]["puntos"] = round(100.0 * (1 - (a_ + b_ - 2) / (2 * n_t)), 1)
        orden = sorted(ev_g, key=lambda c: (-ev_g[c]["puntos"], c))
        n_aptas["calidad_g"] = len(orden)
        resultado["calidad_g"] = recorrer("calidad_g", [pos[c] for c in orden], reglas.n_de("calidad_g"),
                                          evals=ev_g, post=post_lynch, max_sector=reglas.sector_de("calidad_g"))

    if "corto" in activos and estado_ia["agotada"]:
        avisos.append(f"Se agotó el límite de {max_ia} análisis de IA: las últimas candidatas del "
                      "paquete corto van sin explicación de la IA.")
    if "corto" in activos and usar_ia and f_ia is None:
        avisos.append("Sin IA en esta ejecución (falta la clave de Gemini): las candidatas del paquete "
                      "corto van sin la explicación de «por qué ha caído».")

    # --- estado de cada empresa, para revisar la cartera ----------------------- #
    estado_corto: dict[str, str] = {r["ticker"]: "candidata" for r in corto[0]}
    estado_corto.update({v["ticker"]: "vetada" for v in corto[1]})
    estado_corto.update({e["ticker"]: "en_espera" for e in corto[2]})
    en_paquetes: dict[str, list[str]] = {}
    for paquete, (acept, _, _) in resultado.items():
        for r in acept:
            en_paquetes.setdefault(r["ticker"], []).append(paquete)

    empresas: dict[str, dict] = {}
    for fila in tesis:
        t = fila["ticker"]
        y = yahoo(t)
        e = {"n": fila.get("name") or "", "v": fila.get("veredicto_final"),
             "pe": _num(fila.get("precio_entrada"), 2), "vmin": _num(fila.get("valor_min"), 2),
             "vmax": _num(fila.get("valor_max"), 2), "sec": fila.get("sector") or "",
             "td": _dias_desde(fila.get("analizado_el"), hoy)}
        if fila.get("veredicto_cambio_el") and fila.get("veredicto_anterior"):
            e["va"], e["vc"] = fila["veredicto_anterior"], str(fila["veredicto_cambio_el"])[:10]
        if fila.get("pasa_cribado") is False:
            e["qp"] = False                      # ya no pasa el cribado cuantitativo
        if fila.get("lynch"):
            e["ly"] = "no"                       # filtro Lynch: ok | precio (solo falla por caro) | no
        k = pos.get(y)
        if k is None or not np.isfinite(sen["precio"].iloc[k]):
            e["e"] = "sin_datos"
        else:
            precio = float(sen["precio"].iloc[k])
            cara = bool(decidir_ventas(
                np.array([True]), np.array([precio]), np.array([float(sen["mediana"].iloc[k])]),
                np.array([float(sen["percentil"].iloc[k])]), np.array([False]), np.array([0.0]),
                dataclasses.replace(p, venta="cara", vender_por_tiempo=False))[0] > 0)
            if fila.get("lynch"):
                ev = lynch_con_precio(fila["lynch"], precio)
                e["ly"] = "no" if ev is None else ("ok" if ev["pasa"] else "precio")
            e.update({"p": _num(precio, 2), "m": _num(sen["mediana"].iloc[k], 2),
                      "d": _num(desc[k], 3), "q": _num(pct[k], 3), "r": _num(reb[k], 3),
                      "c": cara,
                      "sv": bool(e["vmax"] and precio > e["vmax"]),
                      "e": estado_corto.get(t, "normal")})
        if t in en_paquetes:
            e["pk"] = en_paquetes[t]
        empresas[t] = e

    n_lista = int(elegible.sum())
    n_con_senal = int(con_senal.sum())
    if n_lista and n_con_senal < 0.5 * n_lista:
        avisos.append(f"Solo {n_con_senal} de {n_lista} empresas elegibles tienen 5 años de "
                      "precios utilizables; el resto no se puede valorar frente a su historia.")
    n_a = int(sum(1 for t in tier_col if t == "A"))
    n_b = int(sum(1 for t in tier_col if t == "B"))

    spy = precios[BENCHMARK].dropna().iloc[-DIAS_SPY:] if BENCHMARK in precios.columns else pd.Series(dtype=float)

    def paquete_json(clave):
        acept, vet, esp = resultado[clave]
        info = pq.INFO[clave]
        return {"nombre": info["nombre"], "horizonte": info["horizonte"], "etiqueta": info["etiqueta"],
                "descripcion": info["descripcion"], "criterios": pq.criterios(clave, reglas),
                "n_aptas": n_aptas[clave], "candidatas": acept, "vetadas": vet, "en_espera": esp}

    informe = {
        "version": 2,
        "generado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mes": f"{hoy:%Y-%m}",
        "datos_a": f"{fecha_datos:%Y-%m-%d}",
        "parametros": {
            "n_max": p.n_max, "n_max_sector": p.n_max_sector, "tope_posicion": p.tope_posicion,
            "min_descuento": p.min_descuento, "max_descuento": p.max_descuento,
            "max_percentil": p.max_percentil, "rebote_min": p.rebote_min,
            "estabilizacion": p.estabilizacion, "cara_percentil": p.cara_percentil,
            "cara_prima": p.cara_prima,
        },
        "resumen": {"n_lista": n_lista, "n_con_senal": n_con_senal, "n_tier_a": n_a, "n_tier_b": n_b,
                    "n_candidatas": len(corto[0]), "n_vetadas": len(corto[1]),
                    "n_en_espera": len(corto[2]),
                    "n_aptas": n_aptas},
        # Compatibilidad con la versión 1 del informe: lo de arriba es el paquete corto.
        "candidatas": corto[0],
        "vetadas": corto[1],
        "en_espera": corto[2],
        "paquetes": {c: paquete_json(c) for c in activos},
        "paquetes_activos": list(activos),
        "empresas": empresas,
        # Control del seguimiento: todas las elegibles de este mes, a partes iguales.
        "universo": sorted(c for k, c in enumerate(cols) if con_senal[k]),
        "spy": {"f": [d.strftime("%Y-%m-%d") for d in spy.index], "c": [round(float(x), 2) for x in spy]},
        "avisos": avisos,
    }
    if seg_previo is not None:
        informe["seguimiento"] = seg_previo
    if bloque_ac is not None:
        informe["autocorreccion"] = bloque_ac
    return informe


def _razones_grandes(m: dict, ev_ebit, nota) -> list[str]:
    k = pq.metricas_clave(m)
    out = []
    if nota is not None:
        out.append(f"Nota del cribado de calidad: {nota:.0f}/100.")
    if k["roic"] is not None:
        out.append(f"Rentabilidad sobre el capital (ROIC) del {k['roic']:.0f} %.")
    if k["fcf_margen"] is not None:
        out.append(f"Caja libre del {k['fcf_margen']:.0f} % de las ventas.")
    if k["crec"] is not None:
        out.append(f"Ingresos creciendo un {k['crec']:.1f} % al año.")
    if ev_ebit:
        out.append(f"Valor de empresa {ev_ebit:.0f} veces su beneficio operativo (rentabilidad del beneficio del {100 / ev_ebit:.1f} %).")
    return out[:5]


def _razones_lynch(guardado: dict, ev: dict) -> list[str]:
    """Razones de la evaluación guardada, con el PEG y el PER de hoy."""
    cat = {"rapida": "Crecimiento rápido («fast grower»)", "solida": "Empresa sólida («stalwart»)",
           "lenta": "Crecimiento lento con buen dividendo («slow grower»)"}.get(ev["cat"], "")
    peg_nombre = "PEG" if ev["cat"] == "rapida" else "PEG con dividendo"
    out = [f"{cat}: el beneficio por acción ha crecido un {ev['crec']:.0f} % al año, sin años de pérdidas.",
           f"{peg_nombre} {ev['peg_usado']:.2f} al precio de hoy (PER {ev['per']:.0f}): "
           + ("una ganga según Lynch (≤ 0,5)." if ev["peg_usado"] <= 0.5 else "precio razonable para lo que crece (≤ 1).")]
    out += [r for r in (guardado.get("razones") or [])[2:]]
    return out


def _avisos_candidata(reg: dict) -> list[str]:
    a: list[str] = []
    if reg.get("paquete", "").startswith("lynch") and reg["veredicto"] == "SIN_TESIS":
        a.append("Aún sin análisis de IA de su tesis: solo pasa el filtro Lynch (los números). "
                 "Mira qué hace la empresa y por qué crece antes de comprar: Lynch pedía entender la «historia».")
    if reg.get("tier") == "B":
        a.append("Aún sin análisis de IA de su tesis: solo ha pasado el cribado cuantitativo "
                 "(calidad, caja, solvencia). Léela con más cautela y mira su ficha antes de comprar.")
    if reg.get("tesis_antigua"):
        a.append(f"La tesis tiene {reg['tesis_dias']} días: se refrescará en el próximo ciclo; "
                 "mírala antes de comprar.")
    s = reg.get("salud")
    if s and s.get("deterioro"):
        a.append("Último trimestre con deterioro: " + "; ".join(s["avisos"]) + ".")
    if reg["veredicto"] == "VIGILAR" and not reg.get("doble_descuento") and reg.get("precio_entrada"):
        a.append(f"Es VIGILAR y aún está por encima de su precio de entrada ({reg['precio_entrada']:.2f}).")
    ia = reg.get("ia")
    if ia and not ia.get("error") and ia["tipo"] == "estructural":
        a.append("La IA ve un posible problema estructural (confianza " + ia["confianza"] + ").")
    if reg.get("paquete") in ("medio", "largo") and reg.get("percentil") is not None and reg["percentil"] >= 0.80:
        a.append("Está en la parte alta de su rango de 5 años: si compras, mejor escalonado.")
    return a


# --------------------------------------------------------------------------- #
# Markdown para el Issue (solo información pública: nada de tu cartera)
# --------------------------------------------------------------------------- #

def _pc(x, dec: int = 0) -> str:
    return "—" if x is None else f"{x * 100:+.{dec}f} %"


def _pp(x, dec: int = 1) -> str:
    """Diferencia entre dos rentabilidades, en puntos porcentuales."""
    return "—" if x is None else f"{x * 100:+.{dec}f} pp"


def issue_markdown(inf: dict, url_dashboard: str | None = None) -> str:
    L: list[str] = []
    A = L.append
    r = inf["resumen"]
    A(f"## Oportunidades de {inf['mes']}\n")
    A(f"Precios a {inf['datos_a']}. Se han mirado {r['n_lista']} empresas que pasan el cribado "
      f"de calidad ({r.get('n_tier_a', 0)} con análisis de IA favorable y {r.get('n_tier_b', 0)} "
      f"solo con cribado cuantitativo); {r['n_con_senal']} tienen historia suficiente para "
      "compararlas con su propio rango de 5 años.\n")

    paq = inf.get("paquetes") or {}
    cands = inf["candidatas"] if "corto" in paq else []
    if "corto" in paq:
        A("### 📅 1–3 años · Descuento con tesis\n")
    if "corto" in paq and not cands:
        A("**Este mes no hay nada claramente barato frente a su propio historial.** "
          "No hace falta forzar ninguna compra: el dinero puede esperar.\n")
    elif cands:
        A("| Empresa | Precio | Bajo su mediana 5a | Rango de valor (tesis) | Nivel | ¿Por qué ha caído? (IA) |")
        A("|---|---:|---:|---:|---|---|")
        for c in cands:
            rango = (f"{c['valor_min']:.0f}–{c['valor_max']:.0f}" if c["valor_min"] and c["valor_max"] else "—")
            ia = c.get("ia")
            tipo = "—" if not ia or ia.get("error") else f"{ia['tipo']} ({ia['confianza']})"
            estrella = " ⭐" if c.get("doble_descuento") else ""
            nivel = c["veredicto"].replace("_", " ") if c["veredicto"] != "SIN_TESIS" else "solo cribado"
            A(f"| **{c['ticker']}**{estrella} · {c['nombre']} | {c['precio']:.2f} | "
              f"{c['descuento'] * 100:.0f} % | {rango} | {nivel} | {tipo} |")
        A("")
        A("⭐ = además está por debajo del precio de entrada que calculó la tesis (doble descuento).\n")
        for c in cands:
            A(f"#### {c['ticker']} — {c['nombre']}")
            ia = c.get("ia")
            if ia and not ia.get("error"):
                A(f"- **Causa probable:** {ia['causa']}")
                if ia.get("riesgo_clave"):
                    A(f"- **Riesgo clave:** {ia['riesgo_clave']}")
                if ia.get("que_vigilar"):
                    A(f"- **Qué vigilar:** {ia['que_vigilar']}")
            s = c.get("salud")
            if s:
                A(f"- **Último trimestre:** {s['resumen']}")
            for av in c.get("avisos", []):
                A(f"- ⚠️ {av}")
            A("")
    for clave, titulo in (("medio", "### 📈 3–5 años · Calidad a precio razonable\n"),
                          ("largo", "### 🌱 5+ años · Compounders\n")):
        P = paq.get(clave)
        if not P:
            continue
        A(titulo)
        if not P["candidatas"]:
            A(f"Ninguna empresa cumple hoy los criterios de este paquete ({P['n_aptas']} aptas).\n")
            continue
        A("| Empresa | Precio | Puntos | ROIC | Ingresos/año | Caja libre | Precio vs su mediana 5a | Nivel |")
        A("|---|---:|---:|---:|---:|---:|---:|---|")
        for c in P["candidatas"]:
            k = c.get("clave") or {}
            f1 = lambda v, s="": "—" if v is None else f"{v:.0f}{s}"  # noqa: E731
            nivel = c["veredicto"].replace("_", " ") if c["veredicto"] != "SIN_TESIS" else "solo cribado"
            A(f"| **{c['ticker']}** · {c['nombre']} | {c['precio']:.2f} | {c['puntos']:.0f} | "
              f"{f1(k.get('roic'), ' %')} | {f1(k.get('crec'), ' %')} | {f1(k.get('fcf_margen'), ' %')} | "
              f"{_pc(-c['descuento']) if c.get('descuento') is not None else '—'} | {nivel} |")
        A("")
        extra = [f"- ⚠️ **{c['ticker']}**: {av}" for c in P["candidatas"] for av in c.get("avisos", [])
                 if "solo ha pasado el cribado" not in av and "parte alta de su rango" not in av]
        if extra:
            L.extend(extra)
            A("")
        if any("parte alta de su rango" in av for c in P["candidatas"] for av in c.get("avisos", [])):
            A("_Varias cotizan en la parte alta de su rango de 5 años (normal en negocios que crecen "
              "mucho): si compras, mejor escalonado en varios meses._\n")
    for clave, titulo, ref in (("lynch_g", "### 🔭 Peter Lynch · grandes (> 5.000 M$)\n", "S&P 500"),
                               ("lynch_p", "### 🔭 Pequeñas · Peter Lynch + calidad (300–5.000 M$)\n", "Russell 2000")):
        P = paq.get(clave)
        if not P:
            continue
        A(titulo)
        if not P["candidatas"]:
            A(f"Ninguna empresa cumple hoy las reglas de Lynch en este tamaño ({P['n_aptas']} aptas antes de los frenos).\n")
            continue
        A("| Empresa | Precio | Puntos | Tipo | Crec. BPA | PER | PEG | Deuda/patrimonio | Nivel |")
        A("|---|---:|---:|---|---:|---:|---:|---:|---|")
        tipos = {"rapida": "rápida", "solida": "sólida", "lenta": "lenta"}
        for c in P["candidatas"]:
            ly = c.get("lynch") or {}
            nivel = c["veredicto"].replace("_", " ") if c["veredicto"] != "SIN_TESIS" else "sin IA aún"
            de = ly.get("de")
            A(f"| **{c['ticker']}** · {c['nombre']} | {c['precio']:.2f} | {c['puntos']:.0f} | {tipos.get(ly.get('cat'), '—')} | "
              f"{ly.get('crec', 0):.0f} % | {ly.get('per', 0):.0f} | {ly.get('peg_usado', 0):.2f} | "
              f"{'—' if de is None else format(de * 100, '.0f') + ' %'} | {nivel} |")
        A("")
        extra = [f"- ⚠️ **{c['ticker']}**: {av}" for c in P["candidatas"] for av in c.get("avisos", [])
                 if "solo pasa el filtro Lynch" not in av]
        if extra:
            L.extend(extra)
            A("")
        A(f"_Se comparan con el {ref}. Las marcadas «sin IA aún» solo han pasado los números: "
          "mira qué hace la empresa antes de comprar._\n")
    P = paq.get("calidad_g")
    if P:
        A("### 🏛️ Grandes · calidad (> 5.000 M$)\n")
        if not P["candidatas"]:
            A(f"Ninguna empresa grande pasa hoy el cribado con los frenos ({P['n_aptas']} aptas antes de los frenos).\n")
        else:
            A("| Empresa | Precio | Puntos | ROIC | Caja libre | Ingresos/año | EV/EBIT | Nivel |")
            A("|---|---:|---:|---:|---:|---:|---:|---|")
            f1 = lambda v, s_="": "—" if v is None else f"{v:.0f}{s_}"  # noqa: E731
            for c in P["candidatas"]:
                k = c.get("clave") or {}
                nivel = c["veredicto"].replace("_", " ") if c["veredicto"] != "SIN_TESIS" else "solo cribado"
                A(f"| **{c['ticker']}** · {c['nombre']} | {c['precio']:.2f} | {c['puntos']:.0f} | {f1(k.get('roic'), ' %')} | "
                  f"{f1(k.get('fcf_margen'), ' %')} | {f1(k.get('crec'), ' %')} | {f1(k.get('ev_ebit'))} | {nivel} |")
            A("")
            A("_Se comparan con el S&P 500._\n")
    if cands or any((paq.get(k) or {}).get("candidatas") for k in ("medio", "largo", "lynch_g", "lynch_p", "calidad_g")) or (inf.get("dividendos") or {}).get("candidatas"):
        A("**Reparto orientativo:** a partes iguales dentro de cada paquete, con tope por empresa. "
          "Cuánto compras de cada una depende de tu importe y de lo que ya tengas: "
          "se calcula en tu móvil.\n")
    if "corto" in paq and inf["vetadas"]:
        A("**En el paquete de 1–3 años pasaron el filtro de precio pero las frenó otro chequeo:**")
        for v in inf["vetadas"]:
            A(f"- {v['ticker']}: {v['motivo']}")
        A("")

    div = inf.get("dividendos")
    if isinstance(div, dict):
        A("### 💰 Dividendos · small caps que no recortan\n")
        if not div.get("candidatas"):
            A("Ninguna small cap cumple hoy todos los criterios (dividendo sin recortes, cubierto por la caja, sin diluir).\n")
        else:
            A("| Empresa | Precio | Rent. div. | Crec. div. | Años sin recorte | Reparte | Acciones 5a | 5a: precio + div = total |")
            A("|---|---:|---:|---:|---:|---:|---:|---|")
            for c in div["candidatas"]:
                d, e = c["dividendo"], c["empresa"]
                r5 = (c.get("rend") or {}).get("5a") or (c.get("rend") or {}).get("3a")
                cg = d.get("cagr_5a") if d.get("cagr_5a") is not None else d.get("cagr_3a")
                n1 = lambda v, s="": "—" if v is None else f"{v:.0f}{s}"  # noqa: E731
                A(f"| **{c['ticker']}** · {c['nombre']} | {c['precio']:.2f} | "
                  f"{'—' if d.get('rentab_div') is None else format(d['rentab_div'], '.1f') + ' %'} | "
                  f"{'—' if cg is None else format(cg, '+.1f') + ' %'} | {n1(d.get('anios_sin_recorte'))} | "
                  f"{n1(e.get('payout'), ' %')} | {n1(e.get('dilucion_5a'), ' %')} | "
                  f"{('%+.1f + %+.1f = %+.1f %%/año' % (r5['precio'], r5['div'], r5['total'])) if r5 else '—'} |")
            A("")
            ref = ((div.get("referencia") or {}).get("rend") or {}).get("5a")
            if ref:
                A(f"Russell 2000 (IWM) en 5 años: precio {ref['precio']:+.1f} + dividendo {ref['div']:+.1f} = "
                  f"{ref['total']:+.1f} %/año. Son empresas que hoy siguen en el índice (supervivientes): "
                  "lo pasado no es una promesa.\n")
            for c in div["candidatas"]:
                for av in c.get("avisos", []):
                    A(f"- ⚠️ **{c['ticker']}**: {av}")
            A("")

    seg = inf.get("seguimiento")
    if seg:
        A("### 🔎 ¿Están funcionando los paquetes? (seguimiento automático)\n")
        mad = False
        for clave in ("corto", "medio", "largo", "lynch_g", "lynch_p", "calidad_g", "dividendos"):
            s = (seg.get("resumen") or {}).get(clave) or {}
            if s.get("cohortes_maduras"):
                mad = True
                sup = f"{s['superan_spy']}/{s['cohortes_maduras']}"
                etiqueta = "Dividendos" if clave == "dividendos" else pq.INFO[clave]["etiqueta"]
                A(f"- **{etiqueta}**: {s['cohortes_maduras']} cohorte(s) con ≥3 meses; "
                  f"exceso medio sobre {'el Russell 2000' if clave in ('dividendos', 'lynch_p') else 'el S&P 500'} {_pp(s['exceso_medio'])} "
                  f"(superó al índice en {sup})"
                  + (f"; sobre el resto del universo {_pp(s['exceso_medio_pool'])}" if s.get("exceso_medio_pool") is not None else "")
                  + ".")
        if not mad:
            A("- Aún no hay ninguna cohorte con 3 meses de vida.")
        A(f"\n> {seg['aviso']}\n")

    for av in inf["avisos"]:
        A(f"> ⚠️ {av}")
    if inf["avisos"]:
        A("")
    if url_dashboard:
        A(f"👉 [Abrir el Vigía → «Este mes»]({url_dashboard}) para ver cuánto comprar con tu "
          "importe, elegir paquete y revisar tu cartera.\n")
    A("_Información automática, no asesoramiento financiero. Los paquetes que no superaron la prueba "
      "histórica (1–3, 3–5 y 5+ años, y Lynch en grandes) están desactivados; los que quedan se ponen a "
      "prueba en vivo con el seguimiento de arriba. El marcador del Vigía compara tu resultado real con el S&P 500._")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# E/S: Supabase y Yahoo
# --------------------------------------------------------------------------- #

COLUMNAS_CAMBIO = ",veredicto_anterior,veredicto_cambio_el"      # migración 008
COLUMNAS_TESIS = ("ticker,name,sector,veredicto_final,precio_entrada,valor_min,valor_max,"
                  "nota_fundamental,confianza,analizado_el")


def _paginas(consulta, tam: int = 1000):
    """Recorre una consulta paginada de Supabase. `consulta(a, b)` ejecuta el
    rango [a, b] y devuelve la respuesta."""
    a = 0
    while True:
        parte = consulta(a, a + tam - 1).data or []
        yield from parte
        if len(parte) < tam:
            return
        a += tam


def cargar_tesis(repo) -> list[dict]:
    """Empresas con análisis de IA (vista pública `v_tesis_completa`). Con la migración 008,
    también el veredicto anterior y cuándo cambió; sin ella, sigue sin esos campos."""
    try:
        return list(_paginas(lambda a, b: repo.db.table("v_tesis_completa")
                             .select(COLUMNAS_TESIS + COLUMNAS_CAMBIO).range(a, b).execute()))
    except Exception as e:  # noqa: BLE001
        log.warning("Sin veredicto anterior (¿migración 008 sin aplicar?): %s", e)
        return list(_paginas(lambda a, b: repo.db.table("v_tesis_completa").select(COLUMNAS_TESIS).range(a, b).execute()))


def cargar_universo(repo) -> list[dict]:
    """El universo del mes: todas las empresas cuyo ÚLTIMO cribado cuantitativo
    pasó (con sus métricas), más las que tienen análisis de IA aunque ya no
    pasen (para poder revisar una cartera). Cada fila lleva lo de `cargar_tesis`
    (None si no hay análisis) más `metricas`, `es_financiera`, `quant_score` y
    `pasa_cribado`."""
    tesis = {f["ticker"]: f for f in cargar_tesis(repo)}

    # 1) De cada empresa, ¿pasó su cribado MÁS RECIENTE? (consulta ligera)
    ultimas: dict[str, tuple[str, bool]] = {}
    for r in _paginas(lambda a, b: (repo.db.table("quant_screens").select("id,company_id,passed")
                                    .order("created_at", desc=True).order("id").range(a, b).execute())):
        ultimas.setdefault(r["company_id"], (r["id"], bool(r["passed"])))
    ids_vigentes = {i for i, ok in ultimas.values() if ok}

    # 2) Detalle (métricas) solo de las que pasan
    filas: dict[str, dict] = {}

    def detalle(snap: str) -> list[dict]:
        return list(_paginas(lambda a, b: (
            repo.db.table("quant_screens")
            .select("id,score,created_at,breakdown,companies(ticker,name,sector,is_financial,active),"
                    f"fundamentals_snapshots({snap})")
            .eq("passed", True).order("created_at", desc=True).order("id").range(a, b).execute())))
    try:
        # precio y capitalización de la ficha: respaldo para saber el tamaño (grupo de grandes)
        filas_det = detalle("metrics,as_of,price,mcap:raw->market_cap")
    except Exception as e:  # noqa: BLE001
        log.warning("Sin precio/capitalización de las fichas (%s): se sigue sin ellos", e)
        filas_det = detalle("metrics,as_of")
    for r in filas_det:
        if r["id"] not in ids_vigentes:
            continue
        c, s = r.get("companies"), r.get("fundamentals_snapshots")
        if not c or c.get("active") is False or not c.get("ticker"):
            continue
        t = c["ticker"]
        if t in filas:
            continue
        filas[t] = {"ticker": t, "name": c.get("name") or "", "sector": c.get("sector") or "",
                    "es_financiera": bool(c.get("is_financial")), "quant_score": r.get("score"),
                    "metricas": (s or {}).get("metrics"), "pasa_cribado": True,
                    "snap_precio": (s or {}).get("price"), "snap_cap": (s or {}).get("mcap"),
                    "lynch": (r.get("breakdown") or {}).get("lynch"),
                    "veredicto_final": None, "precio_entrada": None, "valor_min": None,
                    "valor_max": None, "nota_fundamental": None, "confianza": None, "analizado_el": None}

    # 2b) Las que pasan el filtro Peter Lynch en su ÚLTIMO cribado aunque no pasen el de calidad
    try:
        for r in _paginas(lambda a, b: (
                repo.db.table("quant_screens")
                .select("id,company_id,score,passed,created_at,breakdown,companies(ticker,name,sector,is_financial,active)")
                .eq("breakdown->lynch->>pasa", "true").order("created_at", desc=True).order("id").range(a, b).execute())):
            c = r.get("companies")
            if not c or c.get("active") is False or not c.get("ticker"):
                continue
            ult = ultimas.get(r.get("company_id")) if r.get("company_id") else None
            if ult is not None and ult[0] != r["id"]:
                continue                     # no es su cribado más reciente
            t = c["ticker"]
            if t in filas:
                continue
            filas[t] = {"ticker": t, "name": c.get("name") or "", "sector": c.get("sector") or "",
                        "es_financiera": bool(c.get("is_financial")), "quant_score": r.get("score"),
                        "metricas": None, "pasa_cribado": bool(r.get("passed")),
                        "lynch": (r.get("breakdown") or {}).get("lynch"),
                        "veredicto_final": None, "precio_entrada": None, "valor_min": None,
                        "valor_max": None, "nota_fundamental": None, "confianza": None, "analizado_el": None}
    except Exception as e:  # noqa: BLE001  (sin datos Lynch: el informe sigue sin esos paquetes)
        log.warning("No se pudieron leer las evaluaciones Lynch: %s", e)

    # 3) Superponer el análisis de IA, y añadir las analizadas que ya no pasan el cribado
    for t, f in tesis.items():
        if t in filas:
            for k in ("veredicto_final", "precio_entrada", "valor_min", "valor_max",
                      "nota_fundamental", "confianza", "analizado_el", "veredicto_anterior", "veredicto_cambio_el"):
                filas[t][k] = f.get(k)
            filas[t]["sector"] = filas[t]["sector"] or f.get("sector") or ""
        else:
            filas[t] = {**f, "es_financiera": False, "quant_score": None, "metricas": None,
                        "pasa_cribado": False}
    return list(filas.values())


def cargar_informes(repo) -> list[dict]:
    """Informes mensuales guardados (para el seguimiento). Si la tabla aún no
    existe (migración 006 sin aplicar) devuelve []."""
    try:
        return list(_paginas(lambda a, b: (repo.db.table("mensual_informes")
                                           .select("creado_el,mes,informe").order("creado_el").range(a, b).execute()),
                             tam=50))
    except Exception as e:  # noqa: BLE001
        log.warning("Sin informes previos (¿migración 006 sin aplicar?): %s", e)
        return []


def texto_tesis(repo, ticker: str) -> str | None:
    try:
        res = (repo.db.table("v_tesis_completa").select("report_md").eq("ticker", ticker).limit(1).execute())
        return primera_frase(res.data[0]["report_md"]) if res.data else None
    except Exception as e:  # noqa: BLE001
        log.warning("Sin texto de tesis para %s: %s", ticker, e)
        return None


def descargar(tickers: list[str], anios: int = 6, min_cobertura: float = 0.70) -> pd.DataFrame:
    from .precios import descargar_precios, limpiar

    ys = sorted({yahoo(t) for t in tickers} | {BENCHMARK})
    desde = f"{date.today().year - anios}-{date.today():%m-%d}"
    bruto = descargar_precios(ys, desde)
    if bruto.empty or BENCHMARK not in bruto.columns:
        raise RuntimeError("No se pudo descargar ni el SPY: Yahoo Finance está bloqueando o caído. "
                           "Reintenta en unas horas.")
    cobertura = len(set(bruto.columns) & set(ys)) / len(ys)
    if cobertura < min_cobertura:
        raise RuntimeError(f"Solo se descargó el {cobertura:.0%} de los precios (mínimo "
                           f"{min_cobertura:.0%}). Un informe con tantos datos a medias no sería fiable.")
    return limpiar(bruto)


def main() -> None:
    ap = argparse.ArgumentParser(description="Recomendador mensual de oportunidades.")
    ap.add_argument("--sin-ia", action="store_true", help="no llamar a Gemini ni a Finnhub (solo se usan si el paquete «corto» está activo)")
    ap.add_argument("--max-ia", type=int, default=8, help="máximo de análisis de IA por ejecución")
    ap.add_argument("--sin-supabase", action="store_true", help="no guardar el informe en la base de datos")
    ap.add_argument("--salida", default="informe_mensual.json", help="dónde escribir el JSON")
    ap.add_argument("--md", default="issue_mensual.md", help="dónde escribir el Markdown del Issue")
    ap.add_argument("--url-dashboard", default=None, help="enlace al Vigía para el Issue")
    ap.add_argument("--sin-dividendos", action="store_true",
                    help="no generar la sección de small caps con dividendo (Yahoo + SEC, unos 15-25 minutos)")
    ap.add_argument("--max-universo", type=int, default=2500,
                    help="tope de empresas del universo cuyo precio se descarga (por seguridad)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from pipeline import Repo
    from .explicacion import explicar_caida, titulares
    from .salud import obtener_salud

    repo = Repo()
    universo = cargar_universo(repo)
    if not universo:
        sys.exit("No hay ninguna empresa analizada en Supabase todavía.")
    elegibles = [f for f in universo if _tier(f) is not None]
    print(f"{len(universo)} empresas en el universo ({len(elegibles)} elegibles: "
          f"{sum(1 for f in elegibles if _tier(f) == 'A')} con IA favorable, "
          f"{sum(1 for f in elegibles if _tier(f) == 'B')} solo con cribado).")
    if len(universo) > args.max_universo:
        # Se descartan primero las menos interesantes: sin cribado vigente y las rechazadas por la IA.
        universo.sort(key=lambda f: (_tier(f) is None, -(f.get("quant_score") or 0)))
        print(f"Aviso: universo recortado a {args.max_universo} empresas.")
        universo = universo[:args.max_universo]

    previos = cargar_informes(repo)
    extra = sg.tickers_necesarios(previos)
    tickers = sorted({yahoo(f["ticker"]) for f in universo} | extra)
    print(f"Descargando precios de {len(tickers)} tickers (incluye {len(extra)} de informes anteriores)...")
    precios = descargar(tickers)

    hay_ia = (not args.sin_ia) and bool(os.getenv("GEMINI_API_KEY") or os.getenv("GEMINI_API_KEYS"))
    inf = generar_informe(
        universo, precios, Params(), date.today(),
        usar_ia=not args.sin_ia, max_ia=args.max_ia,
        f_salud=obtener_salud,
        f_titulares=None if args.sin_ia else titulares,
        f_ia=explicar_caida if hay_ia else None,
        f_tesis_texto=lambda t: texto_tesis(repo, t),
        previos=previos,
        validacion=ac.leer(),
        autocorr_previo=((previos[-1].get("informe") or {}).get("autocorreccion") if previos else None),
    )

    if not args.sin_dividendos:
        # Sección aparte (otro universo: small caps del Russell 2000). Si falla, el resto del
        # informe sigue adelante: sólo se avisa.
        try:
            from . import dividendos as dv
            inf["dividendos"] = dv.generar(date.today())
            dv.volcar_estado(inf)
            d = inf["dividendos"]
            print(f"\nDividendos: {d['resumen']} · elegidas {[c['ticker'] for c in d['candidatas']]}")
        except Exception as e:  # noqa: BLE001
            log.exception("Sección de dividendos no disponible")
            inf["avisos"].append(f"No se pudo generar la sección de dividendos: {e}")

    with open(args.salida, "w", encoding="utf-8") as f:
        json.dump(inf, f, ensure_ascii=False)
    with open(args.md, "w", encoding="utf-8") as f:
        f.write(issue_markdown(inf, args.url_dashboard))

    if not args.sin_supabase:
        repo.db.table("mensual_informes").insert({"mes": inf["mes"], "informe": inf}).execute()
        print("Informe guardado en Supabase (mensual_informes).")

    r = inf["resumen"]
    print(f"\nResumen: {r['n_lista']} elegibles · {r['n_con_senal']} con señal · aptas {r['n_aptas']}")
    for clave, P in inf["paquetes"].items():
        print(f"  [{P['etiqueta']}] {[c['ticker'] for c in P['candidatas']]}")
    if "corto" in inf["paquetes"]:
        print(f"  vetadas (corto): {[v['ticker'] for v in inf['vetadas']]}")
    # Detalle de las elegidas (datos públicos de empresas): sirve para comprobar en el registro
    # de la ejecución que la SEC (último trimestre) y la IA han respondido de verdad.
    for clave, P in inf["paquetes"].items():
        for c in P["candidatas"]:
            s = c.get("salud")
            ia = c.get("ia")
            print(f"    {clave:5} {c['ticker']:6} nivel={c['tier']} precio={c['precio']} "
                  f"salud={'ok' if s else 'n/d'}{' DETERIORO' if s and s.get('deterioro') else ''} "
                  f"ia={('error' if ia.get('error') else ia['tipo']) if ia else '-'}")
    if inf.get("seguimiento"):
        print(f"  seguimiento: {len(inf['seguimiento']['cohortes'])} cohortes; {inf['seguimiento']['aviso']}")
    for a in inf["avisos"]:
        print("AVISO:", a)


if __name__ == "__main__":
    main()
