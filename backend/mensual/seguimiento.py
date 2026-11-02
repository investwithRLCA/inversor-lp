"""
Seguimiento automático — la única forma honesta de saber si los paquetes
funcionan: apuntar lo que se habría elegido y mirar qué pasó DESPUÉS.

Cada informe mensual guarda, para cada paquete, las empresas elegidas ese mes
(información pública de empresas: nada de tu cartera). Los informes
siguientes calculan, con los precios de hoy, cuánto habrían rendido esas
elecciones desde el día en que se hicieron, y lo comparan con:

  · el S&P 500 (SPY) en las mismas fechas, y
  · el «resto del universo»: todas las empresas que ese mes eran elegibles
    (pasaron el cribado), repartidas a partes iguales. Si los paquetes baten
    al S&P 500 pero NO al resto del universo, lo que funciona es el universo
    (empresas de calidad), no la selección.

Reglas para que no se pueda hacer trampa a uno mismo:
  · Solo cuenta el PRIMER informe de cada mes (el que sale el día 1, sin ver
    cómo evoluciona); repetir el informe hasta que salga uno bonito no vale.
  · Los precios de entrada y de salida salen de la MISMA descarga (precios
    ajustados por dividendos y splits), así que incluyen dividendos y no se
    descuadran con el tiempo.
  · Compra a partes iguales y mantener: sin vender nunca, sin recolocar.
  · Las empresas que ya no tienen precio (compradas, deslistadas) se cuentan
    aparte, no se ignoran en silencio.

Con pocos meses esto es ruido. Se dice claramente en el propio informe.
"""

from __future__ import annotations

import math

import pandas as pd

MIN_DIAS_RESUMEN = 90        # una cohorte con menos días no entra en el resumen
MIN_UNIVERSO = 30            # el control «resto del universo» exige al menos 30 empresas
PAQUETES = ("corto", "medio", "largo")
BENCHMARK = "SPY"


def yahoo(ticker: str) -> str:
    return ticker.strip().upper().replace(".", "-")


def primeros_por_mes(filas: list[dict]) -> list[dict]:
    """De las filas de `mensual_informes` ({creado_el, mes, informe}) se queda
    con el PRIMER informe de cada mes, de más antiguo a más reciente."""
    por_mes: dict[str, dict] = {}
    for f in sorted(filas, key=lambda x: x.get("creado_el") or ""):
        mes = f.get("mes") or (f.get("informe") or {}).get("mes")
        if mes and mes not in por_mes and isinstance(f.get("informe"), dict):
            por_mes[mes] = f
    return [por_mes[m] for m in sorted(por_mes)]


def _picks(inf: dict) -> dict[str, list[dict]]:
    """{paquete: [{ticker, tier}]} de un informe, tolerando el formato antiguo
    (solo `candidatas`, que era el paquete corto)."""
    paq = inf.get("paquetes")
    if isinstance(paq, dict) and paq:
        return {k: [{"ticker": c["ticker"], "tier": c.get("tier")} for c in (v.get("candidatas") or [])]
                for k, v in paq.items() if k in PAQUETES}
    return {"corto": [{"ticker": c["ticker"], "tier": c.get("tier")} for c in (inf.get("candidatas") or [])]}


def tickers_necesarios(filas: list[dict], max_informes_universo: int = 12) -> set[str]:
    """Tickers (formato Yahoo) que hay que descargar para valorar los informes
    previos: sus elegidas y, de los últimos informes, su universo de control."""
    out: set[str] = set()
    informes = primeros_por_mes(filas)
    for i, f in enumerate(informes):
        inf = f["informe"]
        for lista in _picks(inf).values():
            out.update(yahoo(c["ticker"]) for c in lista)
        if i >= len(informes) - max_informes_universo:
            out.update(yahoo(t) for t in (inf.get("universo") or []))
    return out


def _precio_en(serie: pd.Series, fecha: pd.Timestamp):
    """Último precio con fecha <= `fecha` (o None). La serie va ordenada."""
    s = serie.loc[:fecha].dropna()
    if s.empty:
        return None
    # no aceptamos un precio demasiado viejo respecto a la fecha pedida
    if (fecha - s.index[-1]).days > 7:
        return None
    return float(s.iloc[-1])


def _rent(precios: pd.DataFrame, col: str, ini: pd.Timestamp, fin: pd.Timestamp):
    if col not in precios.columns:
        return None
    a, b = _precio_en(precios[col], ini), _precio_en(precios[col], fin)
    if a is None or b is None or a <= 0:
        return None
    return b / a - 1.0


def _media(xs):
    xs = [x for x in xs if x is not None and math.isfinite(x)]
    return sum(xs) / len(xs) if xs else None


def calcular(filas: list[dict], precios: pd.DataFrame, hasta: pd.Timestamp) -> dict:
    """Seguimiento de todos los informes previos con precios hasta `hasta`.

    `filas`: filas de `mensual_informes`. `precios`: columnas por ticker de
    Yahoo más SPY. Devuelve el dict que se incrusta en el informe."""
    hasta = pd.Timestamp(hasta)
    cohortes: list[dict] = []

    for f in primeros_por_mes(filas):
        inf = f["informe"]
        try:
            ini = pd.Timestamp(inf["datos_a"])
        except (KeyError, ValueError, TypeError):
            continue
        dias = (hasta - ini).days
        if dias < 1:
            continue                     # el propio informe de hoy no se evalúa
        spy = _rent(precios, BENCHMARK, ini, hasta)
        universo = [yahoo(t) for t in (inf.get("universo") or [])]
        rend_univ = [_rent(precios, t, ini, hasta) for t in universo]
        n_univ = sum(1 for x in rend_univ if x is not None)
        pool = _media(rend_univ) if n_univ >= MIN_UNIVERSO else None

        for paquete, lista in _picks(inf).items():
            if not lista:
                continue
            filas_pick = []
            sin_precio = []
            for c in lista:
                r = _rent(precios, yahoo(c["ticker"]), ini, hasta)
                if r is None:
                    sin_precio.append(c["ticker"])
                else:
                    filas_pick.append([c["ticker"], round(r, 4), c.get("tier")])
            rent = _media([x[1] for x in filas_pick])
            cohortes.append({
                "mes": inf.get("mes") or f.get("mes"),
                "datos_a": inf["datos_a"],
                "paquete": paquete,
                "n": len(lista), "n_sin_precio": len(sin_precio), "sin_precio": sin_precio,
                "dias": dias,
                "rent": None if rent is None else round(rent, 4),
                "spy": None if spy is None else round(spy, 4),
                "pool": None if pool is None else round(pool, 4),
                "exceso": None if rent is None or spy is None else round(rent - spy, 4),
                "exceso_pool": None if rent is None or pool is None else round(rent - pool, 4),
                "picks": filas_pick,
            })

    resumen = {p: _resumen([c for c in cohortes if c["paquete"] == p]) for p in PAQUETES}
    return {
        "hasta": f"{hasta:%Y-%m-%d}",
        "cohortes": cohortes,
        "resumen": resumen,
        "aviso": _aviso(resumen),
    }


def _resumen(cohortes: list[dict]) -> dict:
    """Resumen de un paquete sobre las cohortes con al menos MIN_DIAS_RESUMEN."""
    maduras = [c for c in cohortes if c["dias"] >= MIN_DIAS_RESUMEN and c["exceso"] is not None]
    out = {
        "cohortes": len(cohortes),
        "cohortes_maduras": len(maduras),
        "exceso_medio": None, "exceso_medio_pool": None,
        "superan_spy": None, "superan_pool": None,
        "rent_media": None, "spy_media": None,
        "tier_a": None, "tier_b": None,
    }
    if not maduras:
        return out
    out["exceso_medio"] = round(_media([c["exceso"] for c in maduras]), 4)
    out["rent_media"] = round(_media([c["rent"] for c in maduras]), 4)
    out["spy_media"] = round(_media([c["spy"] for c in maduras]), 4)
    out["superan_spy"] = sum(1 for c in maduras if c["exceso"] > 0)
    con_pool = [c for c in maduras if c["exceso_pool"] is not None]
    if con_pool:
        out["exceso_medio_pool"] = round(_media([c["exceso_pool"] for c in con_pool]), 4)
        out["superan_pool"] = sum(1 for c in con_pool if c["exceso_pool"] > 0)
        out["cohortes_con_pool"] = len(con_pool)
    # ¿aporta algo la tesis de la IA? Rentabilidad media por empresa elegida, según tuviera
    # análisis de IA (A) o solo cribado cuantitativo (B).
    for tier, clave in (("A", "tier_a"), ("B", "tier_b")):
        rs = [p[1] for c in maduras for p in c["picks"] if p[2] == tier]
        if rs:
            out[clave] = {"n": len(rs), "rent_media": round(_media(rs), 4)}
    return out


def _aviso(resumen: dict) -> str:
    maduras = max((r["cohortes_maduras"] for r in resumen.values()), default=0)
    if maduras == 0:
        return ("Todavía no hay ninguna cohorte con 3 meses de vida: el seguimiento empieza a "
                "dar cifras a partir del tercer informe.")
    if maduras < 12:
        return (f"Solo {maduras} mes(es) con al menos 3 meses de recorrido: es muy pronto para "
                "sacar conclusiones. Con menos de 12-24 cohortes la diferencia con el S&P 500 es "
                "casi todo azar.")
    return ("Con más de un año de cohortes ya se puede empezar a mirar, pero las cohortes "
            "se solapan en el tiempo (no son independientes): sé prudente.")
