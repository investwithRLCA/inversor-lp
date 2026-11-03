"""
Autocorrección — el sistema se ajusta solo según se acumulan datos, con reglas
fijadas DE ANTEMANO (aquí abajo). Nada de cambiar el método por una racha de dos
meses: cada ajuste exige un mínimo de datos y que la diferencia sea clara (intervalo
de confianza al 90 % que no cruce el cero). Así, los primeros ajustes por datos reales
llegarán cuando haya suficiente historia (1-2 años); hasta entonces el panel dice
cuánto falta.

Tres mecanismos, revisados en cada informe mensual:

1. REVALIDACIÓN HISTÓRICA (cada mes, workflow «Prueba histórica de los filtros»).
   La prueba point-in-time se repite con los datos al día (cuentas nuevas, precios
   nuevos) y guarda su veredicto en `autocorreccion.json`. Por grupo de tamaño:
     · si el filtro en uso deja de aprobar (no bate al índice y al grupo a 12 y 24
       meses) y otro de los probados sí aprueba → se cambia a ese;
     · si ninguno aprueba → la sección se pone EN PAUSA (no se recomienda nada) hasta
       que alguno vuelva a aprobar;
     · si el en uso sigue aprobando, solo se cambia a otro si le saca al menos
       MARGEN_CAMBIO_PP más frente al índice a 24 meses (evita ir dando bandazos).

2. FRENO POR RESULTADOS REALES (seguimiento en vivo).
   Con al menos MIN_COHORTES_FRENO cohortes de más de un año, si lo elegido rinde
   MENOS que su índice y el intervalo de confianza al 90 % está entero por debajo de
   cero → la sección se pone EN PAUSA. Sale de la pausa cuando el exceso medio vuelve
   a ser ≥ 0.

3. PESO DE LA OPINIÓN DE LA IA (VIGILAR y «sin IA aún»).
   Las elegidas se compran aunque la IA diga VIGILAR (la prueba histórica no usaba la
   IA). Pero se mide: en cada cohorte de más de un año, rentabilidad de las VIGILAR
   menos la de las COMPRA de la misma sección. Con al menos MIN_COHORTES_VEREDICTO
   cohortes, si las VIGILAR rinden menos y el intervalo está entero por debajo de cero
   → dejan de entrar en el reparto (pasan a «en espera»). Lo mismo con las «sin IA aún».
   Se revierte si la diferencia vuelve a ser ≥ 0.

Todo es información pública de empresas: nada de tu cartera.
"""

from __future__ import annotations

import dataclasses
import json
import math
from datetime import date
from pathlib import Path

import numpy as np

RUTA = Path(__file__).resolve().parent / "autocorreccion.json"

MIN_COHORTES_FRENO = 12          # cohortes con ≥ 1 año de vida para poder pausar una sección
MIN_COHORTES_VEREDICTO = 24      # cohortes con ≥ 1 año para dejar fuera las VIGILAR / sin IA
DIAS_COHORTE = 365
MARGEN_CAMBIO_PP = 2.0           # puntos a 24 meses para cambiar un filtro que sigue aprobando
BLOQUE = 12                      # cohortes mensuales de un año se solapan: bootstrap por bloques de 12

# Qué paquete del módulo mensual corresponde a cada filtro probado, por grupo de tamaño.
# (variante de la prueba → (paquete, ajustes de ReglasPaquetes))
MAPA = {
    "pequenas": {
        "lynch_cribado": ("lynch_p", {"lynch_con_cribado": True}),
        "lynch": ("lynch_p", {"lynch_con_cribado": False}),
    },
    "grandes": {
        "calidad": ("calidad_g", {"grandes_variante": "calidad"}),
        "calidad_precio": ("calidad_g", {"grandes_variante": "calidad_precio"}),
        "magica": ("calidad_g", {"grandes_variante": "magica"}),
        "lynch_cribado": ("lynch_g", {"lynch_con_cribado": True}),
        "lynch": ("lynch_g", {"lynch_con_cribado": False}),
    },
}
PAQUETE_DE_GRUPO = {"pequenas": ("lynch_p",), "grandes": ("calidad_g", "lynch_g")}
NOMBRE_GRUPO = {"pequenas": "Pequeñas", "grandes": "Grandes"}
COMPRA = ("COMPRA_FUERTE", "COMPRA")


# --------------------------------------------------------------------------- #
# 1. Revalidación histórica
# --------------------------------------------------------------------------- #

def leer(ruta: Path = RUTA) -> dict:
    try:
        return json.loads(Path(ruta).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def actualizar_validacion(previo: dict, veredictos: dict[str, dict[str, dict]], hoy: str) -> dict:
    """`veredictos[grupo][variante]` = {"veredicto": "mantener"|"quitar"|"sin_datos",
    "ex12": float|None, "ex24": float|None} (excesos frente al índice, en tanto por uno).
    Devuelve el nuevo estado con su historial de cambios."""
    previo = previo or {}
    grupos_prev = previo.get("grupos") or {}
    historial = list(previo.get("historial") or [])
    grupos: dict[str, dict] = {}
    for g, mapa in MAPA.items():
        vs = {v: d for v, d in (veredictos.get(g) or {}).items() if v in mapa}
        aprueban = {v: d for v, d in vs.items() if d.get("veredicto") == "mantener" and d.get("ex24") is not None}
        actual = (grupos_prev.get(g) or {}).get("variante")
        mejor = max(aprueban, key=lambda v: aprueban[v]["ex24"]) if aprueban else None
        if not aprueban:
            nuevo, activo, motivo = actual, False, "ningún filtro probado bate al índice y al grupo a 12 y 24 meses"
        elif actual in aprueban:
            ventaja = (aprueban[mejor]["ex24"] - aprueban[actual]["ex24"]) * 100
            if mejor != actual and ventaja >= MARGEN_CAMBIO_PP:
                nuevo, activo, motivo = mejor, True, f"otro filtro le saca {ventaja:.1f} pp a 24 meses"
            else:
                nuevo, activo, motivo = actual, True, "sigue aprobando"
        else:
            nuevo, activo, motivo = mejor, True, ("el filtro en uso dejó de aprobar" if actual else "primera validación")
        d = vs.get(nuevo) or {}
        grupos[g] = {"variante": nuevo, "activo": activo, "motivo": motivo,
                     "ex12": d.get("ex12"), "ex24": d.get("ex24"),
                     "aprueban": sorted(aprueban)}
        antes = grupos_prev.get(g) or {}
        if antes and (antes.get("variante") != nuevo or bool(antes.get("activo")) != activo):
            historial.append({"fecha": hoy, "grupo": g, "de": antes.get("variante"), "a": nuevo,
                              "activo": activo, "motivo": motivo})
    return {"version": 1, "actualizado": hoy, "grupos": grupos, "historial": historial[-50:]}


# --------------------------------------------------------------------------- #
# 2 y 3. Resultados reales (seguimiento)
# --------------------------------------------------------------------------- #

def _ic(x: list[float], bloque: int = BLOQUE, n_boot: int = 2000, seed: int = 11):
    """IC 90 % de la media con bootstrap de bloques circulares (cohortes solapadas)."""
    a = np.asarray([v for v in x if v is not None and math.isfinite(v)], dtype=float)
    n = len(a)
    if n < 6:
        return None
    rng = np.random.default_rng(seed)
    b = max(1, min(bloque, n))
    nb = -(-n // b)
    ini = rng.integers(0, n, size=(n_boot, nb))
    idx = (ini[:, :, None] + np.arange(b)[None, None, :]) % n
    medias = a[idx].reshape(n_boot, -1)[:, :n].mean(axis=1)
    return float(np.percentile(medias, 5)), float(np.percentile(medias, 95))


def freno_en_vivo(seg: dict | None, paquete: str, en_pausa_antes: bool = False) -> dict:
    """Estado del freno por resultados reales para un paquete."""
    cs = [c for c in ((seg or {}).get("cohortes") or [])
          if c.get("paquete") == paquete and (c.get("dias") or 0) >= DIAS_COHORTE and c.get("exceso") is not None]
    cs.sort(key=lambda c: c.get("datos_a") or "")
    out = {"paquete": paquete, "n": len(cs), "necesarias": MIN_COHORTES_FRENO, "exceso_medio": None,
           "ic": None, "pausa": False, "estado": "acumulando"}
    if not cs:
        return out
    ex = [c["exceso"] for c in cs]
    media = sum(ex) / len(ex)
    out["exceso_medio"] = round(media, 4)
    if len(cs) < MIN_COHORTES_FRENO:
        return out
    ic = _ic(ex)
    out["ic"] = None if ic is None else [round(ic[0], 4), round(ic[1], 4)]
    if en_pausa_antes and media < 0:
        out["pausa"], out["estado"] = True, "pausa"            # sigue en pausa hasta que vuelva a ≥ 0
    elif ic is not None and media < 0 and ic[1] < 0:
        out["pausa"], out["estado"] = True, "pausa"
    else:
        out["estado"] = "en_marcha"
    return out


def comparar_veredictos(seg: dict | None, paquetes: tuple[str, ...], grupo_v: str,
                        excluido_antes: bool = False) -> dict:
    """Por cohorte de más de un año: media de las de `grupo_v` («VIGILAR» o «SIN_TESIS») menos
    media de las COMPRA de la misma sección. Decide si dejan de entrar en el reparto."""
    difs = []
    for c in ((seg or {}).get("cohortes") or []):
        if c.get("paquete") not in paquetes or (c.get("dias") or 0) < DIAS_COHORTE:
            continue
        picks = c.get("picks") or []
        g = [p[1] for p in picks if len(p) > 3 and (p[3] or "SIN_TESIS") == grupo_v and p[1] is not None]
        b = [p[1] for p in picks if len(p) > 3 and p[3] in COMPRA and p[1] is not None]
        if g and b:
            difs.append((c.get("datos_a") or "", sum(g) / len(g) - sum(b) / len(b)))
    difs.sort()
    out = {"grupo": grupo_v, "n": len(difs), "necesarias": MIN_COHORTES_VEREDICTO,
           "diferencia_media": None, "ic": None, "excluir": False, "estado": "acumulando"}
    if not difs:
        return out
    x = [d for _, d in difs]
    media = sum(x) / len(x)
    out["diferencia_media"] = round(media, 4)
    if len(x) < MIN_COHORTES_VEREDICTO:
        return out
    ic = _ic(x)
    out["ic"] = None if ic is None else [round(ic[0], 4), round(ic[1], 4)]
    if (excluido_antes and media < 0) or (ic is not None and media < 0 and ic[1] < 0):
        out["excluir"], out["estado"] = True, "fuera_del_reparto"
    else:
        out["estado"] = "dentro"
    return out


# --------------------------------------------------------------------------- #
# Decisión del mes
# --------------------------------------------------------------------------- #

def decidir(activos: tuple[str, ...], reglas, seg: dict | None, validacion: dict | None,
            previo: dict | None = None, hoy: str | None = None):
    """Aplica los tres mecanismos. Devuelve (activos, reglas, excluir_veredictos, bloque_informe).
    `previo` = bloque «autocorreccion» del informe anterior (para mantener pausas y exclusiones
    hasta que se cumpla la condición de salida)."""
    hoy = hoy or date.today().isoformat()
    previo = previo or {}
    activos = list(activos)
    cambios_reglas: dict = {}
    notas: list[str] = []

    # 1) revalidación histórica
    val = (validacion or {}).get("grupos") or {}
    for g, info in val.items():
        if g not in MAPA:
            continue
        if not any(p in activos for p in PAQUETE_DE_GRUPO[g]):
            continue                     # este grupo no está en el módulo: no se toca
        if info.get("activo"):
            if info.get("variante") not in MAPA[g]:
                continue                 # variante desconocida: se deja como estaba
            paq, ajustes = MAPA[g][info["variante"]]
            pos = min(activos.index(p) for p in PAQUETE_DE_GRUPO[g] if p in activos)
            activos = [p for p in activos if p not in PAQUETE_DE_GRUPO[g]]
            activos.insert(pos, paq)     # mismo sitio que tenía: no cambia el orden de las secciones
            cambios_reglas.update(ajustes)
        else:
            for p in PAQUETE_DE_GRUPO[g]:
                if p in activos:
                    activos.remove(p)
            notas.append(f"{NOMBRE_GRUPO[g]}: en pausa por la revalidación histórica ({info.get('motivo')}).")

    # 2) freno por resultados reales
    frenos = {}
    for p in ("lynch_p", "calidad_g", "lynch_g"):
        antes = bool(((previo.get("frenos") or {}).get(p) or {}).get("pausa"))
        f = freno_en_vivo(seg, p, antes)
        frenos[p] = f
        if f["pausa"] and p in activos:
            activos.remove(p)
            notas.append(f"{p}: en pausa porque en la realidad rinde menos que su índice "
                         f"({f['exceso_medio'] * 100:+.1f} pp de media en {f['n']} cohortes de más de un año).")

    # 3) opinión de la IA
    seccs = tuple(p for p in ("lynch_p", "calidad_g", "lynch_g"))
    veredictos = {}
    excluir: list[str] = []
    for gv in ("VIGILAR", "SIN_TESIS"):
        antes = bool(((previo.get("veredictos") or {}).get(gv) or {}).get("excluir"))
        r = comparar_veredictos(seg, seccs, gv, antes)
        veredictos[gv] = r
        if r["excluir"]:
            excluir.append(gv)
            notas.append(f"Las {'VIGILAR' if gv == 'VIGILAR' else '«sin IA aún»'} quedan fuera del reparto: "
                         f"rinden {r['diferencia_media'] * 100:+.1f} pp menos que las COMPRA ({r['n']} cohortes).")

    reglas = dataclasses.replace(reglas, **cambios_reglas) if cambios_reglas else reglas
    bloque = {
        "fecha": hoy,
        "validacion": {"actualizado": (validacion or {}).get("actualizado"), "grupos": val,
                       "historial": ((validacion or {}).get("historial") or [])[-5:]},
        "frenos": frenos,
        "veredictos": veredictos,
        "excluir_veredictos": excluir,
        "activos": activos,
        "notas": notas,
        "reglas": {"min_cohortes_freno": MIN_COHORTES_FRENO, "min_cohortes_veredicto": MIN_COHORTES_VEREDICTO,
                   "margen_cambio_pp": MARGEN_CAMBIO_PP},
    }
    return tuple(activos), reglas, tuple(excluir), bloque


def veredictos_de_prueba(decisiones: dict) -> dict[str, dict[str, dict]]:
    """Convierte el resultado de prueba_lynch (decidir por variante y grupo) al formato
    de `actualizar_validacion`."""
    out: dict[str, dict[str, dict]] = {}
    for clave, d in decisiones.items():
        if ":" not in clave:
            continue
        v, g = clave.split(":", 1)
        out.setdefault(g, {})[v] = {"veredicto": d.get("veredicto"), "ex12": d.get("ex12"), "ex24": d.get("ex24")}
    return out
