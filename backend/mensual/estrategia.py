"""
Módulo mensual — REGLAS de la estrategia "comprar el bajón de una empresa
sólida y vender cuando esté cara".

Aquí vive SOLO la lógica de decisión, como funciones puras (reciben datos,
devuelven decisiones, no tocan red ni base de datos). Las usa el backtest
(`backtest.py`) y las usará después el recomendador mensual real, así que lo
que se prueba en el pasado es exactamente lo que se ejecutaría hoy.

La idea, en números (sin velas ni patrones gráficos):

  1. "Barata frente a sí misma": el precio actual está por debajo de su
     propia mediana de los últimos ~5 años (`descuento`) y en la zona baja
     de su propio rango (`percentil`). Es la versión medible de "esta
     acción siempre ha estado entre 80 y 120 y ahora está en 60".
  2. "Bajón, no derrumbe": se descartan caídas enormes (> max_descuento),
     que casi siempre son un problema de fondo y no un susto pasajero.
  3. "Que haya dejado de caer" (opcional): el precio ha rebotado un mínimo
     desde su suelo del último mes. Evita comprar cuchillos cayendo.
  4. Reparto: el dinero disponible (lo que aportes ese mes + el efectivo que
     hubiera quedado esperando + lo vendido) se divide a partes iguales entre
     las mejores N candidatas, con un tope por empresa y un máximo de 2 por
     sector. Lo que no quepa NO se fuerza: se queda en efectivo esperando la
     siguiente oportunidad (no hay "ETF de relleno": el dinero lo pones tú a
     mano y solo en las acciones que la herramienta te indique).
  5. Venta: solo cuando está claramente cara (o variantes), nunca por
     alcanzar el "valor justo" a secas — así se cortan menos ganadores y se
     pagan menos impuestos (en España cada venta tributa).

NO incluye el filtro de calidad fundamental (el cribado cuantitativo del
proyecto de largo plazo): ese se aplicará sobre la lista de candidatas en el
recomendador real. El backtest, por tanto, mide solo la parte de PRECIO.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np
import pandas as pd

SENAL_COLS = ["precio", "mediana", "percentil", "descuento", "rebote"]


@dataclass(frozen=True)
class Params:
    # --- 1-2. "barata frente a sí misma", pero no hundida --------------- #
    ventana_dias: int = 1260          # ~5 años bursátiles
    min_historia_frac: float = 0.90   # exige ≥90 % de la ventana con datos
    min_descuento: float = 0.15       # ≥15 % por debajo de su mediana
    max_descuento: float = 0.60       # >60 % de caída = probablemente roto
    max_percentil: float = 0.20       # y en el 20 % inferior de su rango
    # --- 3. esperar a que deje de caer ---------------------------------- #
    estabilizacion: bool = True
    rebote_min: float = 0.03          # ≥3 % sobre su mínimo de 21 sesiones
    # --- 4. reparto ------------------------------------------------------ #
    n_max: int = 4
    n_max_sector: int = 2             # como mucho 2 candidatas del mismo sector
    tope_posicion: float = 0.25       # máx. 25 % de las acciones del módulo por empresa
                                      # (nunca menos de 1/nº de empresas que se tengan)
    rend_efectivo: float = 0.02       # rendimiento NETO anual del efectivo que espera
    relleno: str = "efectivo"         # 'efectivo' (modelo real) | 'etf' (solo diagnóstico)
    # --- 5. venta --------------------------------------------------------- #
    venta: str = "cara"               # 'cara' | 'mediana' | 'mitad' | 'nunca'
    cara_percentil: float = 0.80      # "cara" = en el 20 % superior de su rango…
    cara_prima: float = 0.10          # …y ≥10 % por encima de su mediana
    revision_meses: int = 24
    vender_por_tiempo: bool = False   # si True, vende al cumplir revision_meses
    # --- fricciones -------------------------------------------------------- #
    comision: float = 0.001           # 0,1 % por operación (compra o venta)
    tipo_impuesto: float = 0.21       # aproximación IRPF del ahorro (editable)

    def etiqueta(self) -> str:
        e = "con espera" if self.estabilizacion else "sin espera"
        return f"venta={self.venta} · {e}"


# --------------------------------------------------------------------------- #
# Señales
# --------------------------------------------------------------------------- #

def senales(precios: pd.DataFrame, fecha, p: Params) -> pd.DataFrame:
    """Calcula, con datos hasta `fecha` INCLUIDA y nada posterior, cinco
    cifras por empresa:

      precio     último precio
      mediana    mediana de la ventana (su "valor de antaño")
      percentil  fracción de la ventana en la que el precio fue MÁS BAJO que
                 hoy (0 = en su mínimo histórico de la ventana, 1 = en su máximo)
      descuento  1 - precio/mediana  (positivo = por debajo de su mediana)
      rebote     precio / mínimo de las últimas 21 sesiones - 1

    Devuelve un DataFrame indexado por ticker (el mismo orden de columnas de
    `precios`), con NaN donde no hay historia suficiente."""
    cols = precios.columns
    vacio = pd.DataFrame(np.nan, index=cols, columns=SENAL_COLS)

    hist = precios.loc[:fecha]
    n = p.ventana_dias
    minimo_obs = int(n * p.min_historia_frac)
    if len(hist) < minimo_obs:
        return vacio

    arr = hist.iloc[-n:].to_numpy(dtype=float)
    ultimo = arr[-1]
    validos = np.sum(~np.isnan(arr), axis=0)
    ok = (validos >= minimo_obs) & ~np.isnan(ultimo) & (ultimo > 0)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        mediana = np.nanmedian(arr, axis=0)
        percentil = (arr < ultimo).sum(axis=0) / np.where(validos > 0, validos, np.nan)
        minimo21 = np.nanmin(arr[-21:], axis=0)
        rebote = ultimo / minimo21 - 1.0
        descuento = 1.0 - ultimo / mediana

    out = pd.DataFrame(
        {"precio": ultimo, "mediana": mediana, "percentil": percentil,
         "descuento": descuento, "rebote": rebote},
        index=cols,
    )
    out.loc[~ok, :] = np.nan
    return out


# --------------------------------------------------------------------------- #
# Candidatas y reparto
# --------------------------------------------------------------------------- #

def aplicar_tope_sector(orden, sector, max_por_sector: int, n_max: int) -> np.ndarray:
    """Recorre `orden` (ya ordenado de mejor a peor) y se queda con las primeras
    `n_max` sin pasar de `max_por_sector` por sector. `sector` son códigos
    enteros; -1 = sector desconocido (sin límite). Sin `sector`, solo recorta."""
    orden = np.asarray(orden, dtype=int)
    if sector is None or max_por_sector <= 0:
        return orden[:n_max]
    cuenta: dict[int, int] = {}
    out: list[int] = []
    for i in orden:
        s = int(sector[i])
        if s >= 0:
            if cuenta.get(s, 0) >= max_por_sector:
                continue
            cuenta[s] = cuenta.get(s, 0) + 1
        out.append(int(i))
        if len(out) >= n_max:
            break
    return np.array(out, dtype=int)


def elegir_candidatas(descuento: np.ndarray, percentil: np.ndarray,
                      rebote: np.ndarray, p: Params, sector=None) -> np.ndarray:
    """Índices de las empresas que cumplen las reglas de entrada, de mayor a
    menor descuento, recortadas a `n_max` y a `n_max_sector` por sector.
    NaN (sin datos) nunca cumple."""
    with np.errstate(invalid="ignore"):
        ok = (
            (descuento >= p.min_descuento)
            & (descuento <= p.max_descuento)
            & (percentil <= p.max_percentil)
        )
        if p.estabilizacion:
            ok &= rebote >= p.rebote_min
    idx = np.flatnonzero(ok)
    idx = idx[np.argsort(-descuento[idx], kind="stable")]
    return aplicar_tope_sector(idx, sector, p.n_max_sector, p.n_max)


def _llenar_a_partes_iguales(total: float, hueco: dict[int, float]) -> dict[int, float]:
    """Reparte `total` a partes iguales entre las claves de `hueco`, sin dar a
    ninguna más que su hueco; lo que una no pueda absorber se reparte entre las
    demás ("llenado de vasos"). Si todos los huecos se llenan, queda resto."""
    asign = {i: 0.0 for i in hueco}
    abiertos = [i for i in hueco if hueco[i] > 1e-12]
    resto = total
    while abiertos and resto > 1e-9:
        cuota = resto / len(abiertos)
        cerrados = []
        for i in abiertos:
            dar = min(cuota, hueco[i] - asign[i])
            asign[i] += dar
            resto -= dar
            if hueco[i] - asign[i] <= 1e-12:
                cerrados.append(i)
        if not cerrados:
            break
        abiertos = [i for i in abiertos if i not in cerrados]
    return asign


def repartir(disponible: float, idx: np.ndarray, valor_pos: np.ndarray,
             p: Params) -> tuple[dict[int, float], float]:
    """Divide `disponible` a partes iguales entre las candidatas `idx`.

    Tope por empresa: ninguna puede pasar de `tope_posicion` del valor que
    tendrán TODAS las acciones del módulo después de comprar (contando lo que
    ya tienes). Para que la primera compra no sea imposible, el tope nunca baja
    de 1/(nº de empresas distintas que tendrías): con 2 empresas el tope
    efectivo es ≥50 %, con 3 ≥33 %, con 4 o más el `tope_posicion`.

    Lo que no quepa NO se fuerza a entrar: es el SOBRANTE, que se queda en
    efectivo esperando oportunidades. Devuelve ({índice: importe}, sobrante)."""
    if disponible <= 0:
        return {}, 0.0
    if len(idx) == 0:
        return {}, float(disponible)

    idx = [int(i) for i in idx]
    en_cartera = {int(i) for i in np.flatnonzero(valor_pos > 0)}
    tope = max(p.tope_posicion, 1.0 / len(set(idx) | en_cartera))
    acciones = float(valor_pos.sum())

    # El techo depende de cuánto se acabe comprando, y eso del techo: se itera
    # desde "se compra todo" hacia abajo hasta que cuadra (converge rápido).
    comprado = disponible
    compras: dict[int, float] = {}
    for _ in range(200):
        techo = tope * (acciones + comprado)
        hueco = {i: max(0.0, techo - float(valor_pos[i])) for i in idx}
        compras = _llenar_a_partes_iguales(disponible, hueco)
        nuevo = sum(compras.values())
        if abs(nuevo - comprado) < 1e-9:
            comprado = nuevo
            break
        comprado = nuevo

    compras = {i: a for i, a in compras.items() if a > 1e-9}
    return compras, max(disponible - sum(compras.values()), 0.0)


# --------------------------------------------------------------------------- #
# Ventas
# --------------------------------------------------------------------------- #

def decidir_ventas(en_cartera: np.ndarray, precio: np.ndarray, mediana: np.ndarray,
                   percentil: np.ndarray, mitad_vendida: np.ndarray,
                   meses_tenencia: np.ndarray, p: Params) -> np.ndarray:
    """Fracción (0..1) de cada posición a vender este mes.

      'mediana'  vende TODO al volver a su mediana ("valor de antaño").
      'cara'     vende TODO solo cuando está claramente cara.
      'mitad'    vende la mitad al llegar a la mediana, el resto cuando esté cara.
      'nunca'    no vende por precio (referencia para aislar el efecto de la compra).

    Con vender_por_tiempo=True, además, vende lo que lleve ≥ revision_meses."""
    frac = np.zeros(len(precio))
    with np.errstate(invalid="ignore"):
        en_mediana = en_cartera & (precio >= mediana)
        cara = (
            en_cartera
            & (percentil >= p.cara_percentil)
            & (precio >= (1.0 + p.cara_prima) * mediana)
        )

    if p.venta == "mediana":
        frac[en_mediana] = 1.0
    elif p.venta == "cara":
        frac[cara] = 1.0
    elif p.venta == "mitad":
        frac[en_mediana & ~mitad_vendida] = 0.5
        frac[cara] = 1.0
    elif p.venta != "nunca":
        raise ValueError(f"venta desconocida: {p.venta!r}")

    if p.vender_por_tiempo:
        frac[en_cartera & (meses_tenencia >= p.revision_meses)] = 1.0
    return frac
