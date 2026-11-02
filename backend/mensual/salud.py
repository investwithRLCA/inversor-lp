"""
Chequeo de salud TRIMESTRAL — ¿la caída del precio viene acompañada de un
deterioro real del negocio, o es un susto del mercado?

La tesis del bot de largo plazo se apoya en los últimos cuentas ANUALES (10-K),
que pueden tener hasta un año. Para un "bajón" reciente eso es poco: si la
empresa ha empeorado en los últimos 2-3 trimestres, el precio bajo puede estar
justificado (trampa de valor). Esto mira lo último publicado (10-Q) en la
misma API gratuita de la SEC que ya usa el resto del proyecto.

Qué compara (siempre contra el MISMO trimestre del año anterior, para quitar
estacionalidad):
  · ingresos del último trimestre publicado
  · margen operativo (resultado operativo / ingresos)

Es una alerta, no un veto automático: la lee la IA al explicar la caída y
aparece como aviso en el informe. Si la empresa no publica "ingresos" con las
etiquetas habituales (bancos, aseguradoras...) devuelve None y no se avisa de
nada: mejor callar que inventar.

`salud_trimestral(facts)` es una función pura sobre el JSON `companyfacts` de
la SEC (se prueba sin red). `obtener_salud(ticker)` lo descarga.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

log = logging.getLogger(__name__)

TAGS_INGRESOS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
]
TAGS_OPERATIVO = ["OperatingIncomeLoss"]

UMBRAL_INGRESOS_CAEN = -0.10      # ingresos -10 % o peor interanual
UMBRAL_MARGEN_CAE_PP = -5.0       # margen operativo -5 puntos o peor
DIAS_DATOS_VIEJOS = 200           # último trimestre con más de ~6 meses = desfasado


def _d(s: str) -> date:
    return datetime.fromisoformat(s).date()


def _trimestres(facts: dict, tags: list[str]) -> dict[date, float]:
    """{fecha_fin_del_trimestre: valor} para el primer tag con datos.
    Un "trimestre" es un periodo de 80-100 días; el último 'filed' gana
    (reexpresiones)."""
    gaap = facts.get("facts", {}).get("us-gaap", {})
    for tag in tags:
        node = gaap.get(tag)
        if not node:
            continue
        out: dict[date, tuple[str, float]] = {}
        for vals in node.get("units", {}).values():
            for it in vals:
                ini, fin = it.get("start"), it.get("end")
                if not ini or not fin or it.get("val") is None:
                    continue
                try:
                    dias = (_d(fin) - _d(ini)).days
                except ValueError:
                    continue
                if not 80 <= dias <= 100:
                    continue
                f = _d(fin)
                filed = it.get("filed", "")
                prev = out.get(f)
                if prev is None or filed >= prev[0]:
                    out[f] = (filed, float(it["val"]))
        if out:
            return {k: v[1] for k, v in out.items()}
    return {}


def _hace_un_anio(serie: dict[date, float], fin: date) -> date | None:
    """Fecha de la serie más cercana a `fin - 1 año` (±20 días), si la hay."""
    objetivo = fin.replace(year=fin.year - 1) if not (fin.month == 2 and fin.day == 29) \
        else fin.replace(year=fin.year - 1, day=28)
    mejor, mejor_d = None, 21
    for f in serie:
        d = abs((f - objetivo).days)
        if d < mejor_d:
            mejor, mejor_d = f, d
    return mejor


def salud_trimestral(facts: dict, hoy: date | None = None) -> dict | None:
    """Devuelve un resumen del último trimestre frente al mismo del año
    anterior, o None si no hay datos suficientes."""
    hoy = hoy or date.today()
    ing = _trimestres(facts, TAGS_INGRESOS)
    if len(ing) < 2:
        return None
    ultimo = max(ing)
    previo = _hace_un_anio(ing, ultimo)
    if previo is None or ing[previo] <= 0:
        return None

    out: dict = {
        "periodo": ultimo.isoformat(),
        "dias_desde_periodo": (hoy - ultimo).days,
        "ingresos_yoy": ing[ultimo] / ing[previo] - 1.0,
        "margen_operativo": None,
        "margen_operativo_previo": None,
        "delta_margen_pp": None,
    }

    op = _trimestres(facts, TAGS_OPERATIVO)
    if ultimo in op and previo in op and ing[ultimo] > 0:
        m_ahora = op[ultimo] / ing[ultimo]
        m_antes = op[previo] / ing[previo]
        out["margen_operativo"] = m_ahora
        out["margen_operativo_previo"] = m_antes
        out["delta_margen_pp"] = (m_ahora - m_antes) * 100.0

    avisos = []
    if out["ingresos_yoy"] <= UMBRAL_INGRESOS_CAEN:
        avisos.append(f"ingresos {out['ingresos_yoy'] * 100:+.0f} % interanual")
    d = out["delta_margen_pp"]
    if d is not None and d <= UMBRAL_MARGEN_CAE_PP:
        avisos.append(f"margen operativo {d:+.1f} puntos")
    if out["dias_desde_periodo"] > DIAS_DATOS_VIEJOS:
        avisos.append("último trimestre publicado desfasado")
    out["deterioro"] = bool(avisos) and out["dias_desde_periodo"] <= DIAS_DATOS_VIEJOS
    out["avisos"] = avisos

    partes = [f"ingresos {out['ingresos_yoy'] * 100:+.0f} % interanual"]
    if d is not None:
        partes.append(f"margen operativo {out['margen_operativo_previo'] * 100:.0f} % → "
                      f"{out['margen_operativo'] * 100:.0f} %")
    out["resumen"] = f"Trimestre a {out['periodo']}: " + "; ".join(partes)
    return out


def obtener_salud(ticker: str) -> dict | None:
    """Descarga `companyfacts` de la SEC y devuelve `salud_trimestral`. Nunca
    lanza: un fallo de red o un ticker sin datos devuelve None."""
    try:
        from providers import SecEdgarProvider
        sec = SecEdgarProvider()
        cik, _ = sec.resolve_cik(ticker)
        facts = sec._get(f"{sec.BASE}/api/xbrl/companyfacts/CIK{cik}.json")
        return salud_trimestral(facts)
    except Exception as e:  # noqa: BLE001
        log.warning("Salud trimestral no disponible para %s: %s", ticker, e)
        return None
