"""
Filtro Peter Lynch — reglas de «One Up on Wall Street» (1989) llevadas a números.

Fuentes de los umbrales (no inventados aquí):
  · Validea, modelo «P/E/Growth Investor» basado en One Up on Wall Street
    (blog.validea.com, «GARP, PEGs and Peter Lynch» y «The Investment Strategy
    of Peter Lynch»): categorías por crecimiento del beneficio por acción,
    PEG < 1 (0,5 lo ideal), PEG ajustado por dividendo para «stalwarts» y
    «slow growers», deuda/patrimonio ≤ 80 %, inventario que no crece más que
    las ventas (hasta +5 puntos tolerado), crecimiento > 50 % descartado por
    insostenible, en financieras patrimonio/activos ≥ 5 % y ROA ≥ 1 %.
  · AAII, «Lynch's One Up on Wall Street-Inspired Screening Strategy»:
    beneficios positivos los 5 años, crecimiento ≤ 50 %, PEG ajustado ≤ 0,5
    como «atractivo» y > 1 como «malo».

Categorías de Lynch que se pueden automatizar con las cuentas:
  · rápida  (fast grower): BPA crece 20–50 %/año. Lynch prefería 20–25 %.
  · sólida  (stalwart):    BPA crece 10–20 %/año y ventas ≥ 1.000 M$.
  · lenta   (slow grower): BPA crece < 10 %/año, ventas ≥ 1.000 M$ y dividendo ≥ 3 %.
Las cíclicas, las recuperaciones («turnarounds») y las de activos ocultos
(«asset plays») dependen de un juicio que no está en los números: no se
eligen aquí.

Lo que Lynch miraba y NO se puede comprobar con datos gratuitos: compras de
directivos, % en manos de fondos, nº de analistas que la siguen y la
«historia» del negocio. Eso queda para la IA y para ti.

Este módulo es puro (sin red): recibe un `Fundamentals` con precio.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

from providers import Fundamentals

VERSION = "lynch-1.0"


@dataclass
class ReglasLynch:
    crec_min_rapida: float = 20.0
    crec_max: float = 50.0            # más que esto no dura (Lynch / Validea / AAII)
    crec_min_solida: float = 10.0
    ventas_min_grande: float = 1_000.0  # M$: «stalwarts» y «slow growers» son empresas grandes
    peg_max: float = 1.0              # PEG (o PEG ajustado por dividendo) aceptable
    peg_ideal: float = 0.5            # «ganga»
    per_max_rapida_grande: float = 40.0
    dividendo_min_lenta: float = 3.0  # %
    deuda_patrimonio_max: float = 0.80
    inventario_subida_max: float = 5.0  # puntos de % de las ventas en un año
    patrimonio_activos_min_fin: float = 5.0   # % (financieras)
    roa_min_fin: float = 1.0                  # % (financieras)
    caja_neta_bonus: float = 30.0     # % de la capitalización: colchón «tipo Lynch»
    min_anios: int = 4                # años de BPA (3 de crecimiento) como mínimo
    min_capitalizacion: float = 300.0  # M$, igual que el cribado


DEFAULT = ReglasLynch()


@dataclass
class ResultadoLynch:
    ticker: str
    pasa: bool
    categoria: str | None = None      # rapida | solida | lenta | None
    puntos: float = 0.0               # 0-100 para ordenar
    crec_bpa: float | None = None     # %/año
    per: float | None = None
    peg: float | None = None          # PER / crecimiento
    peg_div: float | None = None      # PER / (crecimiento + dividendo)
    dividendo: float | None = None    # %
    deuda_patrimonio: float | None = None
    caja_neta_pct: float | None = None  # (caja - deuda) / capitalización, %
    capitalizacion: float | None = None  # M$
    ventas: float | None = None          # M$, último ejercicio
    inventario_cambio: float | None = None  # puntos de % de las ventas
    motivos: list[str] = field(default_factory=list)   # por qué NO pasa
    razones: list[str] = field(default_factory=list)   # por qué sí (en cristiano)
    version: str = VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def resumen(self, bpa: float | None = None) -> dict[str, Any]:
        """Lo que se guarda con cada cribado (quant_screens.breakdown["lynch"]) y ve el Vigía.
        `bpa` (beneficio por acción del último año) permite recalcular PER y PEG con otro precio."""
        r = lambda x, d=2: None if x is None else round(float(x), d)
        return {"v": self.version, "pasa": self.pasa, "cat": self.categoria, "pts": r(self.puntos, 1),
                "crec": r(self.crec_bpa, 1), "per": r(self.per, 1), "peg": r(self.peg), "peg_div": r(self.peg_div),
                "div": r(self.dividendo), "de": r(self.deuda_patrimonio), "caja": r(self.caja_neta_pct, 1),
                "inv": r(self.inventario_cambio, 1), "bpa": r(bpa, 4),
                "cap": r(self.capitalizacion, 0), "ventas": r(self.ventas, 0),
                "motivos": self.motivos[:3], "razones": self.razones[:5]}


def con_precio(l: dict, precio: float, r: ReglasLynch = DEFAULT) -> dict | None:
    """Rehace la parte de precio de una evaluación guardada (`ResultadoLynch.resumen`)
    con el precio de hoy: PER, PEG, dividendo, capitalización y puntos. Las demás
    reglas (crecimiento, deuda, inventario, beneficios) no dependen del precio.
    Devuelve None si la evaluación guardada no pasó por otra razón que el precio
    o si faltan datos."""
    if not l or not precio or precio <= 0 or not l.get("cat") or not l.get("bpa") or not l.get("crec"):
        return None
    otras = [m for m in (l.get("motivos") or []) if not m.startswith(("PEG", "PER "))]
    if otras:
        return None
    bpa, g, cat = float(l["bpa"]), float(l["crec"]), l["cat"]
    if bpa <= 0 or g <= 0:
        return None
    per_ant = l.get("per")
    precio_ant = per_ant * bpa if per_ant else None
    ratio = (precio / precio_ant) if precio_ant else 1.0
    per = precio / bpa
    div = (l.get("div") or 0.0) / ratio
    cap = (l.get("cap") or 0.0) * ratio if l.get("cap") else None
    peg, peg_div = per / g, per / (g + div)
    usado = peg if cat == "rapida" else peg_div
    motivos = []
    if usado > r.peg_max:
        motivos.append(f"{'PEG' if cat == 'rapida' else 'PEG con dividendo'} {usado:.2f} > {r.peg_max:.1f} al precio de hoy")
    if cat == "rapida" and (l.get("ventas") or 0) >= r.ventas_min_grande and per > r.per_max_rapida_grande:
        motivos.append(f"PER {per:.0f} > {r.per_max_rapida_grande:.0f} en una empresa ya grande")
    if cat == "lenta" and div < r.dividendo_min_lenta:
        motivos.append(f"dividendo {div:.1f} % < {r.dividendo_min_lenta:.0f} % al precio de hoy")
    if cap is not None and cap < r.min_capitalizacion:
        motivos.append(f"capitalización {cap:,.0f} M$ < {r.min_capitalizacion:,.0f} M$")
    pts_ant = l.get("pts") or 0.0
    peg_ant = (l.get("peg") if cat == "rapida" else l.get("peg_div"))
    parte = lambda x: 40 * _escala(x, r.peg_max, r.peg_ideal * 0.8) if x is not None else 0.0
    pts = round(pts_ant - parte(peg_ant) + parte(usado), 1)
    return {"pasa": not motivos, "motivos": motivos, "cat": cat, "per": round(per, 1), "peg": round(peg, 2),
            "peg_div": round(peg_div, 2), "peg_usado": round(usado, 2), "div": round(div, 2), "cap": None if cap is None else round(cap, 0),
            "crec": g, "puntos": pts, "de": l.get("de"), "caja": l.get("caja")}


def _cagr(a: float, b: float, n: int) -> float | None:
    if a is None or b is None or a <= 0 or b <= 0 or n <= 0:
        return None
    return ((b / a) ** (1 / n) - 1) * 100


def _escala(x: float | None, peor: float, mejor: float) -> float:
    """0 en `peor`, 1 en `mejor` (sirve en los dos sentidos)."""
    if x is None:
        return 0.0
    if peor == mejor:
        return 1.0
    t = (x - peor) / (mejor - peor)
    return max(0.0, min(1.0, t))


def evaluar(f: Fundamentals, r: ReglasLynch = DEFAULT) -> ResultadoLynch:
    res = ResultadoLynch(ticker=f.ticker, pasa=False)
    M = res.motivos

    # --- beneficio por acción ---
    ni, sh = f.net_income, f.shares_diluted
    if not ni or not sh or len(ni) != len(sh):
        M.append("sin beneficio por acción comparable")
        return res
    bpa = [ni[i] / sh[i] for i in range(len(ni)) if sh[i]]
    if len(bpa) < r.min_anios:
        M.append(f"solo {len(bpa)} años de beneficios")
        return res
    if any(x <= 0 for x in bpa):
        M.append("ha tenido pérdidas en los últimos años (Lynch pide beneficios todos los años)")
        return res
    n = len(bpa) - 1
    g = _cagr(bpa[0], bpa[-1], n)
    g = None if g is None else round(g, 4)        # evita que un 19,99999 por redondeo no cuente como 20
    res.crec_bpa = g
    if not f.price or f.price <= 0:
        M.append("sin precio")
        return res
    mcap = f.market_cap or (f.price * sh[-1] if sh else None)
    if mcap is not None and mcap < r.min_capitalizacion:
        M.append(f"capitalización {mcap:,.0f} M$ < {r.min_capitalizacion:,.0f} M$")
        return res
    res.per = f.price / bpa[-1]
    div = (f.dividends_paid[-1] / mcap * 100) if (f.dividends_paid and mcap) else 0.0
    res.dividendo = div
    ventas = f.revenue[-1] if f.revenue else 0.0
    res.capitalizacion, res.ventas = mcap, ventas

    # --- categoría ---
    if g is None or g <= 0:
        M.append("el beneficio por acción no crece")
        return res
    if g > r.crec_max:
        M.append(f"crece {g:.0f} %/año: demasiado rápido para durar (Lynch descarta > {r.crec_max:.0f} %)")
        return res
    if g >= r.crec_min_rapida:
        cat = "rapida"
    elif g >= r.crec_min_solida:
        if ventas < r.ventas_min_grande:
            M.append(f"crece {g:.0f} %/año pero vende {ventas:,.0f} M$: ni «rápida» (≥ 20 %) ni «sólida» grande (≥ 1.000 M$)")
            return res
        cat = "solida"
    else:
        if ventas < r.ventas_min_grande or div < r.dividendo_min_lenta:
            M.append(f"crece solo {g:.0f} %/año sin el dividendo (≥ {r.dividendo_min_lenta:.0f} %) ni el tamaño de una «lenta» que compense")
            return res
        cat = "lenta"
    res.categoria = cat

    # --- precio frente a crecimiento ---
    res.peg = res.per / g
    res.peg_div = res.per / (g + div)
    peg_usado = res.peg if cat == "rapida" else res.peg_div
    nombre_peg = "PEG" if cat == "rapida" else "PEG con dividendo"
    if peg_usado > r.peg_max:
        M.append(f"{nombre_peg} {peg_usado:.2f} > {r.peg_max:.1f}: se paga demasiado por el crecimiento")
    if cat == "rapida" and ventas >= r.ventas_min_grande and res.per > r.per_max_rapida_grande:
        M.append(f"PER {res.per:.0f} > {r.per_max_rapida_grande:.0f} en una empresa ya grande")

    # --- deuda ---
    if f.is_financial:
        pa = (f.equity / f.total_assets * 100) if (f.equity and f.total_assets) else None
        roa = (ni[-1] / f.total_assets * 100) if f.total_assets else None
        if pa is None or pa < r.patrimonio_activos_min_fin:
            M.append(f"financiera con poco capital (patrimonio/activos {('%.1f' % pa) if pa is not None else 'n/d'} %)")
        if roa is None or roa < r.roa_min_fin:
            M.append(f"financiera con ROA {('%.2f' % roa) if roa is not None else 'n/d'} % < {r.roa_min_fin:.0f} %")
    else:
        if f.equity is None or f.equity <= 0:
            M.append("patrimonio negativo o desconocido")
        else:
            de = (f.total_debt or 0.0) / f.equity
            res.deuda_patrimonio = de
            if de > r.deuda_patrimonio_max:
                M.append(f"deuda/patrimonio {de * 100:.0f} % > {r.deuda_patrimonio_max * 100:.0f} %")

    # --- inventario frente a ventas (si la empresa tiene inventario) ---
    inv = f.inventory or []
    if len(inv) >= 2 and len(f.revenue) >= 2 and inv[-1] is not None and inv[-2] is not None \
            and f.revenue[-1] and f.revenue[-2]:
        cambio = (inv[-1] / f.revenue[-1] - inv[-2] / f.revenue[-2]) * 100
        res.inventario_cambio = cambio
        if cambio > r.inventario_subida_max:
            M.append(f"el inventario crece más que las ventas (+{cambio:.1f} puntos): señal de alarma de Lynch")

    # --- colchón de caja ---
    if mcap:
        res.caja_neta_pct = ((f.cash or 0.0) - (f.total_debt or 0.0)) / mcap * 100

    res.pasa = not M
    res.puntos = _puntos(f, res, r, cat, peg_usado, g)
    if res.pasa:
        res.razones = _razones(res, cat, peg_usado, nombre_peg, g)
    return res


def _puntos(f: Fundamentals, res: ResultadoLynch, r: ReglasLynch, cat: str, peg: float, g: float) -> float:
    """0-100 para ordenar las que pasan: sobre todo cuánto crecimiento se compra por euro."""
    p = 40 * _escala(peg, r.peg_max, r.peg_ideal * 0.8)
    if cat == "rapida":
        p += 15 * (1.0 if g <= 25 else _escala(g, r.crec_max, 25))
    elif cat == "solida":
        p += 9
    else:
        p += 5
    if f.is_financial:
        p += 8
    elif res.deuda_patrimonio is not None:
        p += 15 * _escala(res.deuda_patrimonio, r.deuda_patrimonio_max, 0.0)
    if res.caja_neta_pct is not None:
        p += 10 * _escala(res.caja_neta_pct, 0.0, r.caja_neta_bonus)
    fcf = (f.cfo[-1] - f.capex[-1]) if (f.cfo and f.capex) else None
    mcap = f.market_cap
    if fcf is not None and mcap:
        p += 10 * _escala(fcf / mcap * 100, 0.0, 8.0)
    if len(f.shares_diluted) >= 2 and f.shares_diluted[0]:
        cambio = (f.shares_diluted[-1] / f.shares_diluted[0] - 1) * 100
        p += 5 * _escala(cambio, 5.0, -5.0)
    p += 5 * (1.0 if res.inventario_cambio is None or res.inventario_cambio <= 0 else 0.5)
    return round(p, 1)


def _razones(res: ResultadoLynch, cat: str, peg: float, nombre_peg: str, g: float) -> list[str]:
    nombre = {"rapida": "Crecimiento rápido («fast grower»)", "solida": "Empresa sólida («stalwart»)",
              "lenta": "Crecimiento lento con buen dividendo («slow grower»)"}[cat]
    out = [f"{nombre}: el beneficio por acción ha crecido un {g:.0f} % al año, sin años de pérdidas.",
           f"{nombre_peg} {peg:.2f}: " + ("una ganga según Lynch (≤ 0,5)." if peg <= 0.5 else "precio razonable para lo que crece (≤ 1).")]
    if res.deuda_patrimonio is not None:
        out.append(f"Deuda/patrimonio {res.deuda_patrimonio * 100:.0f} % (Lynch pide ≤ 80 %).")
    if res.caja_neta_pct is not None and res.caja_neta_pct >= 30:
        out.append(f"Caja neta del {res.caja_neta_pct:.0f} % de lo que vale en bolsa: colchón.")
    if res.inventario_cambio is not None:
        out.append("El inventario no crece más que las ventas." if res.inventario_cambio <= 0
                   else f"El inventario sube algo más que las ventas (+{res.inventario_cambio:.1f} puntos), dentro de lo tolerado.")
    return out
