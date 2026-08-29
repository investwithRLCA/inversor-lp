"""
Capa 2 — FILTRO CUANTITATIVO (sin IA, coste cero).

Objetivo: descartar el 80-90 % del universo antes de gastar un solo token.

Dos etapas:
  1. VETOS  : condiciones eliminatorias de calidad/solvencia. Un solo veto → fuera.
  2. SCORE  : puntuación 0-100 ponderada. Pasa a la IA si score >= umbral.

Todo umbral vive en `Thresholds` para que puedas ajustarlo sin tocar la lógica,
y `RULESET_VERSION` se guarda en base de datos con cada cribado: si cambias los
criterios, los análisis viejos quedan marcados como obsoletos.

Uso:
    from providers import build_fundamentals
    from quant_filter import screen, build_llm_payload

    f = build_fundamentals("MSFT")
    r = screen(f)
    if r.passed:
        payload = build_llm_payload(f, r)   # markdown compacto para el LLM
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Any, Callable

from providers import Fundamentals

RULESET_VERSION = "quant-1.2"


# --------------------------------------------------------------------------- #
# Utilidades numéricas seguras
# --------------------------------------------------------------------------- #

def _div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def cagr(serie: list[float], years: int) -> float | None:
    """CAGR en % sobre los últimos `years` periodos. None si no procede."""
    if len(serie) < years + 1:
        return None
    ini, fin = serie[-(years + 1)], serie[-1]
    if ini is None or fin is None or ini <= 0 or fin <= 0:
        return None
    return ((fin / ini) ** (1 / years) - 1) * 100


def slope_pct(serie: list[float]) -> float | None:
    """Pendiente relativa simple: variación media anual en puntos porcentuales."""
    if len(serie) < 3:
        return None
    return (serie[-1] - serie[0]) / (len(serie) - 1)


# --------------------------------------------------------------------------- #
# Métricas derivadas
# --------------------------------------------------------------------------- #

@dataclass
class Metrics:
    ticker: str = ""
    per: float | None = None
    ev_ebit: float | None = None
    p_fcf: float | None = None
    p_book: float | None = None

    roic: float | None = None                    # último ejercicio, %
    roic_serie: list[float] = field(default_factory=list)
    roic_medio: float | None = None

    debt_equity: float | None = None
    net_debt_ebitda: float | None = None
    interest_coverage: float | None = None
    current_ratio: float | None = None

    fcf: float | None = None
    fcf_serie: list[float] = field(default_factory=list)
    fcf_positive_years: int = 0
    fcf_margin: float | None = None
    fcf_conversion: float | None = None           # FCF / beneficio neto

    gross_margin: float | None = None
    gross_margin_serie: list[float] = field(default_factory=list)
    gross_margin_trend: float | None = None
    ebit_margin: float | None = None

    rev_cagr_3y: float | None = None
    rev_cagr_5y: float | None = None
    fcf_cagr_5y: float | None = None
    eps_cagr_5y: float | None = None

    share_change_5y: float | None = None          # % variación de acciones (negativo = recompra)
    dividend_yield: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_metrics(f: Fundamentals) -> Metrics:
    m = Metrics(ticker=f.ticker)

    rev, ebit, ni = f.revenue, f.ebit, f.net_income
    cfo, capex = f.cfo, f.capex

    # --- Flujo de caja libre ---
    n = min(len(cfo), len(capex))
    m.fcf_serie = [cfo[i] - capex[i] for i in range(-n, 0)] if n else []
    m.fcf = m.fcf_serie[-1] if m.fcf_serie else None
    m.fcf_positive_years = sum(1 for x in m.fcf_serie if x > 0)
    m.fcf_margin = _div(m.fcf, rev[-1]) and _div(m.fcf, rev[-1]) * 100
    if m.fcf is not None and ni and ni[-1] > 0:
        m.fcf_conversion = m.fcf / ni[-1]

    # --- Márgenes ---
    if f.gross_profit and rev:
        k = min(len(f.gross_profit), len(rev))
        m.gross_margin_serie = [f.gross_profit[-k + i] / rev[-k + i] * 100
                                for i in range(k) if rev[-k + i]]
        if m.gross_margin_serie:
            m.gross_margin = m.gross_margin_serie[-1]
            m.gross_margin_trend = slope_pct(m.gross_margin_serie)
    if ebit and rev:
        m.ebit_margin = _div(ebit[-1], rev[-1]) and _div(ebit[-1], rev[-1]) * 100

    # --- ROIC = NOPAT / capital invertido ---
    tax_rate = 0.21
    if f.tax_expense and f.pretax_income and f.pretax_income[-1]:
        tr = f.tax_expense[-1] / f.pretax_income[-1]
        if 0 <= tr <= 0.6:
            tax_rate = tr

    invested = None
    if f.equity is not None:
        invested = f.equity + (f.total_debt or 0.0) - (f.cash or 0.0)
        invested = invested if invested > 0 else None

    if invested and ebit:
        m.roic = (ebit[-1] * (1 - tax_rate)) / invested * 100
        # aproximación de la serie con el capital invertido actual (suficiente para cribar)
        m.roic_serie = [round(e * (1 - tax_rate) / invested * 100, 1) for e in ebit]
        m.roic_medio = sum(m.roic_serie) / len(m.roic_serie)

    # --- Solvencia ---
    m.debt_equity = _div(f.total_debt, f.equity)
    if ebit and f.interest_expense and f.interest_expense[-1] > 0:
        m.interest_coverage = ebit[-1] / f.interest_expense[-1]
    elif ebit and (f.total_debt or 0) == 0:
        m.interest_coverage = 999.0
    ebitda_proxy = ebit[-1] * 1.25 if ebit else None      # sin D&A explícita
    net_debt = (f.total_debt or 0.0) - (f.cash or 0.0)
    m.net_debt_ebitda = _div(net_debt, ebitda_proxy)

    # --- Crecimiento ---
    m.rev_cagr_3y = cagr(rev, 3)
    m.rev_cagr_5y = cagr(rev, 4) if len(rev) >= 5 else None
    m.fcf_cagr_5y = cagr(m.fcf_serie, min(4, len(m.fcf_serie) - 1)) if len(m.fcf_serie) >= 4 else None
    if ni and f.shares_diluted and len(ni) == len(f.shares_diluted):
        eps = [ni[i] / f.shares_diluted[i] for i in range(len(ni)) if f.shares_diluted[i]]
        m.eps_cagr_5y = cagr(eps, min(4, len(eps) - 1)) if len(eps) >= 4 else None

    # --- Acciones ---
    if len(f.shares_diluted) >= 2 and f.shares_diluted[0]:
        m.share_change_5y = (f.shares_diluted[-1] / f.shares_diluted[0] - 1) * 100

    # --- Múltiplos ---
    if f.price and f.shares_diluted and ni and ni[-1] > 0:
        m.per = f.price / (ni[-1] / f.shares_diluted[-1])
    mcap = f.market_cap or (f.price * f.shares_diluted[-1] if f.price and f.shares_diluted else None)
    if mcap:
        m.p_fcf = _div(mcap, m.fcf) if m.fcf and m.fcf > 0 else None
        m.p_book = _div(mcap, f.equity)
        ev = mcap + (f.total_debt or 0.0) - (f.cash or 0.0)
        m.ev_ebit = _div(ev, ebit[-1]) if ebit and ebit[-1] > 0 else None
    if f.dividends_paid and mcap:
        m.dividend_yield = f.dividends_paid[-1] / mcap * 100

    return m


# --------------------------------------------------------------------------- #
# Umbrales configurables
# --------------------------------------------------------------------------- #

@dataclass
class Thresholds:
    # Vetos
    min_market_cap: float = 1_000.0        # M — evita micro caps ilíquidas
    min_years_data: int = 4
    max_per: float = 45.0                  # sin margen de seguridad posible por encima
    max_debt_equity: float = 2.0
    min_interest_coverage: float = 3.0
    min_fcf_positive_years: int = 3
    min_roic: float = 8.0                  # por debajo del coste de capital → destruye valor
    min_rev_cagr_3y: float = 0.0           # ingresos en declive → fuera
    max_share_dilution: float = 15.0       # % de aumento de acciones aceptable

    # Objetivos de scoring (nota máxima)
    target_roic: float = 15.0
    target_rev_cagr: float = 8.0
    target_fcf_margin: float = 12.0
    target_gross_margin: float = 40.0
    fair_per: float = 20.0

    pass_score: float = 55.0               # score mínimo para llegar a la IA


DEFAULT = Thresholds()


# --------------------------------------------------------------------------- #
# Motor de reglas
# --------------------------------------------------------------------------- #

@dataclass
class ScreenResult:
    ticker: str
    passed: bool
    score: float
    vetoes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    breakdown: dict[str, float] = field(default_factory=dict)
    metrics: Metrics = field(default_factory=Metrics)
    ruleset_version: str = RULESET_VERSION

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["metrics"] = self.metrics.to_dict()
        return d


def _apply_vetoes(f: Fundamentals, m: Metrics, t: Thresholds) -> tuple[list[str], list[str]]:
    v: list[str] = []
    w: list[str] = []

    if len(f.revenue) < t.min_years_data:
        v.append(f"histórico insuficiente ({len(f.revenue)} años < {t.min_years_data})")

    mcap = f.market_cap
    if mcap is not None and mcap < t.min_market_cap:
        v.append(f"capitalización {mcap:,.0f}M < {t.min_market_cap:,.0f}M")
    elif mcap is None:
        w.append("capitalización no disponible")

    if m.fcf_positive_years < t.min_fcf_positive_years:
        v.append(f"solo {m.fcf_positive_years} años de FCF positivo")

    if m.roic is not None and m.roic < t.min_roic:
        v.append(f"ROIC {m.roic:.1f}% < {t.min_roic}% (destruye valor)")
    elif m.roic is None:
        w.append("ROIC no calculable")

    if m.rev_cagr_3y is not None and m.rev_cagr_3y < t.min_rev_cagr_3y:
        v.append(f"ingresos en declive: CAGR 3a {m.rev_cagr_3y:.1f}%")

    if m.per is not None and m.per > t.max_per:
        v.append(f"PER {m.per:.1f} > {t.max_per} (sin margen de seguridad plausible)")
    elif m.per is None:
        w.append("PER no disponible (¿beneficio negativo?)")

    # Solvencia: no aplica igual a bancos y aseguradoras
    if not f.is_financial:
        if m.debt_equity is not None and m.debt_equity > t.max_debt_equity:
            v.append(f"Deuda/Patrimonio {m.debt_equity:.2f} > {t.max_debt_equity}")
        if m.interest_coverage is not None and m.interest_coverage < t.min_interest_coverage:
            v.append(f"cobertura de intereses {m.interest_coverage:.1f}x < {t.min_interest_coverage}x")
    else:
        w.append("empresa financiera: ratios de deuda no comparables, revisión manual")

    if m.share_change_5y is not None and m.share_change_5y > t.max_share_dilution:
        v.append(f"dilución del {m.share_change_5y:.1f}% en el periodo")

    # Avisos que no eliminan pero la IA debe ver
    if m.fcf_conversion is not None and m.fcf_conversion < 0.6:
        w.append(f"FCF/BN {m.fcf_conversion:.2f}: calidad del beneficio baja")
    if m.gross_margin_trend is not None and m.gross_margin_trend < -1.0:
        w.append(f"margen bruto cae {abs(m.gross_margin_trend):.1f} pp/año")
    if m.net_debt_ebitda is not None and m.net_debt_ebitda > 3.0:
        w.append(f"Deuda neta/EBITDA {m.net_debt_ebitda:.1f}x elevada")

    return v, w


def _scale(value: float | None, floor: float, target: float, invert: bool = False) -> float:
    """Normaliza a 0-1. `invert=True` para métricas donde menos es mejor."""
    if value is None:
        return 0.5                       # neutro: no penalizamos el dato ausente dos veces
    if invert:
        if value <= target:
            return 1.0
        if value >= floor:
            return 0.0
        return (floor - value) / (floor - target)
    if value >= target:
        return 1.0
    if value <= floor:
        return 0.0
    return (value - floor) / (target - floor)


def _score(f: Fundamentals, m: Metrics, t: Thresholds) -> tuple[float, dict[str, float]]:
    """0-100. Pondera calidad (60), crecimiento (25) y precio (15).
    El precio pesa poco a propósito: la valoración fina la hace la IA."""
    parts: dict[str, float] = {}

    parts["roic"] = _scale(m.roic, t.min_roic, t.target_roic * 1.6) * 22
    parts["margen_bruto"] = _scale(m.gross_margin, 15, t.target_gross_margin + 25) * 10
    parts["margen_fcf"] = _scale(m.fcf_margin, 2, t.target_fcf_margin + 8) * 12
    parts["estabilidad_fcf"] = (m.fcf_positive_years / max(len(m.fcf_serie), 1)) * 8
    parts["solvencia"] = _scale(m.interest_coverage, t.min_interest_coverage, 20) * 8

    parts["crec_ingresos"] = _scale(m.rev_cagr_3y, 0, t.target_rev_cagr + 7) * 13
    parts["crec_fcf"] = _scale(m.fcf_cagr_5y, 0, 15) * 7
    parts["recompras"] = _scale(m.share_change_5y, 10, -5, invert=True) * 5

    parts["per"] = _scale(m.per, t.max_per, t.fair_per * 0.6, invert=True) * 9
    parts["ev_ebit"] = _scale(m.ev_ebit, 30, 12, invert=True) * 6

    total = sum(parts.values())
    return round(total, 1), {k: round(v, 1) for k, v in parts.items()}


def screen(f: Fundamentals, t: Thresholds = DEFAULT) -> ScreenResult:
    m = compute_metrics(f)
    vetoes, warnings = _apply_vetoes(f, m, t)
    score, breakdown = _score(f, m, t)
    passed = not vetoes and score >= t.pass_score
    return ScreenResult(
        ticker=f.ticker,
        passed=passed,
        score=score,
        vetoes=vetoes,
        warnings=warnings,
        breakdown=breakdown,
        metrics=m,
    )


# --------------------------------------------------------------------------- #
# Capa 3 (entrada): payload compacto para el LLM
# --------------------------------------------------------------------------- #

def _s(serie: list[float], dec: int = 0) -> str:
    if not serie:
        return "n/d"
    return "|".join(f"{x:.{dec}f}" for x in serie)


def _n(x: float | None, dec: int = 1) -> str:
    return "n/d" if x is None else f"{x:.{dec}f}"


def build_llm_payload(f: Fundamentals, r: ScreenResult) -> str:
    """Genera el bloque <DATOS> descrito en prompts/user_payload_format.md.
    ~200-250 tokens frente a los ~800 de una tabla Markdown legible."""
    m = r.metrics
    fy = f"{f.fiscal_years[0]}..{f.fiscal_years[-1]}" if f.fiscal_years else "n/d"
    flags = "; ".join(r.warnings) if r.warnings else "sin avisos"

    lines = [
        f'<DATOS ticker="{f.ticker}" fecha_corte="{f.as_of.isoformat()}" '
        f'moneda="{f.currency}" fy_serie="{fy}" unidades="M">',
        f"ID|{f.name}|{f.sector or 'n/d'}|{f.country or 'n/d'}|FY{f.fiscal_years[-1] if f.fiscal_years else 'n/d'}",
        f"PX|{_n(f.price, 2)}|{_n(f.market_cap, 0)}",
        f"MULT|{_n(m.per)}|{_n(m.ev_ebit)}|{_n(m.p_fcf)}|{_n(m.p_book)}|{_n(m.dividend_yield)}",
        f"REV|{_s(f.revenue)}",
        f"EBIT|{_s(f.ebit)}",
        f"NI|{_s(f.net_income)}",
        f"FCF|{_s(m.fcf_serie)}",
        f"MGN|{_s(m.gross_margin_serie, 1)}",
        f"ROIC|{_s(m.roic_serie, 1)}",
        f"BAL|{_n(f.total_debt, 0)}|{_n(f.cash, 0)}|{_n(f.equity, 0)}|"
        f"{_n(m.net_debt_ebitda, 2)}|{_n(m.interest_coverage)}",
        f"SHR|{_s(f.shares_diluted)}",
        f"CAGR|{_n(m.rev_cagr_3y)}|{_n(m.rev_cagr_5y)}|{_n(m.eps_cagr_5y)}|{_n(m.fcf_cagr_5y)}",
        f"FLAG|quant={r.score} ruleset={r.ruleset_version}; {flags}",
        "</DATOS>",
        "",
        "Analiza según tu mandato.",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    from providers import build_fundamentals

    for tk in (sys.argv[1:] or ["MSFT"]):
        fund = build_fundamentals(tk)
        res = screen(fund)
        estado = "PASA" if res.passed else "DESCARTA"
        print(f"\n{'='*60}\n{tk}: {estado}  score={res.score}")
        if res.vetoes:
            print("  Vetos:", *[f"\n    - {v}" for v in res.vetoes])
        if res.warnings:
            print("  Avisos:", *[f"\n    - {w}" for w in res.warnings])
        if res.passed:
            print("\n--- payload al LLM ---")
            print(build_llm_payload(fund, res))
