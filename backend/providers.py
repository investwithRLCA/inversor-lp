"""
Capa 1 — DATOS.

Proveedores de fundamentales normalizados a un único dataclass `Fundamentals`,
para que las capas superiores (filtro cuantitativo e IA) no sepan de dónde vienen
los datos.

Implementados:
  - SecEdgarProvider : SEC EDGAR "companyfacts" (XBRL). Gratis, sin API key,
                       datos auditados. Es la fuente canónica.
  - FinnhubProvider  : precio y market cap en tiempo casi real + metadatos.
                       Plan gratuito: 60 req/min.

Recomendación: EDGAR para el balance/cuenta de resultados (calidad) y Finnhub
solo para el precio (frescura). `build_fundamentals()` hace exactamente eso.

Dependencias: requests
"""

from __future__ import annotations

import os
import threading
import time
import logging
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Any, Protocol

import requests

log = logging.getLogger(__name__)

SEC_UA = os.getenv("SEC_USER_AGENT", "InversorLP/1.0 (contacto@ejemplo.com)")
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY", "")

M = 1_000_000.0  # los importes se normalizan a millones

# --------------------------------------------------------------------------- #
# Caché de proceso para el listado de tickers de la SEC
#
# BUG encontrado al revisar la velocidad del barrido: `build_fundamentals()`
# crea un `SecEdgarProvider()` NUEVO en cada llamada (una por ticker), y
# `self._ticker_map` es un atributo DE INSTANCIA — así que ese listado
# (company_tickers.json, varios MB, ~10.000 empresas) se volvía a descargar
# de la SEC en cada ticker. Un barrido de 100 tickers descargaba ese
# fichero gigante 100 veces en vez de 1. Es, con mucho, la causa principal
# de que el barrido fuera lento. Se soluciona con una caché a
# nivel de módulo (compartida por todas las instancias del proceso, y
# protegida con un lock porque run_universo.py ahora cribará en paralelo).
# --------------------------------------------------------------------------- #
_TICKER_MAP_CACHE: dict[str, dict] | None = None
_TICKER_MAP_CACHE_AT: float = 0.0
_TICKER_MAP_LOCK = threading.Lock()
TICKER_MAP_TTL_SEG = 6 * 3600  # 6h: la lista de la SEC no cambia minuto a minuto

# --------------------------------------------------------------------------- #
# Throttle global para Finnhub (60 peticiones/min en el plan gratuito)
#
# Antes no hacía falta: todo era secuencial. Ahora que run_universo.py puede
# cribar varios tickers en paralelo, varios hilos podrían llamar a Finnhub
# casi a la vez y superar el límite por minuto. Un lock + hueco mínimo entre
# llamadas (compartido por todos los hilos) lo evita sin tener que coordinar
# nada desde fuera de este fichero.
# --------------------------------------------------------------------------- #
_FINNHUB_LOCK = threading.Lock()
_FINNHUB_LAST_CALL: float = 0.0
FINNHUB_MIN_GAP_SEG = 1.1  # ~54 peticiones/min como máximo, con margen bajo las 60


# --------------------------------------------------------------------------- #
# Modelo normalizado
# --------------------------------------------------------------------------- #

@dataclass
class Fundamentals:
    """Fundamentales anuales normalizados. Series ordenadas de FY más antiguo a
    más reciente. Todos los importes en MILLONES de `currency`."""

    ticker: str
    name: str = ""
    cik: str | None = None
    sector: str | None = None
    country: str | None = None
    currency: str = "USD"
    is_financial: bool = False          # bancos/seguros/REIT: D/E y ROIC no aplican igual

    fiscal_years: list[int] = field(default_factory=list)
    as_of: date = field(default_factory=date.today)   # fecha de corte de los datos

    # Mercado
    price: float | None = None
    shares_diluted: list[float] = field(default_factory=list)
    market_cap: float | None = None

    # Cuenta de resultados
    revenue: list[float] = field(default_factory=list)
    gross_profit: list[float] = field(default_factory=list)
    ebit: list[float] = field(default_factory=list)
    net_income: list[float] = field(default_factory=list)
    interest_expense: list[float] = field(default_factory=list)
    tax_expense: list[float] = field(default_factory=list)
    pretax_income: list[float] = field(default_factory=list)

    # Flujos
    cfo: list[float] = field(default_factory=list)
    capex: list[float] = field(default_factory=list)
    dividends_paid: list[float] = field(default_factory=list)
    buybacks: list[float] = field(default_factory=list)

    # Balance (último cierre)
    total_debt: float | None = None
    cash: float | None = None
    equity: float | None = None
    total_assets: float | None = None
    current_liabilities: float | None = None

    def latest(self, serie: list[float]) -> float | None:
        return serie[-1] if serie else None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["as_of"] = self.as_of.isoformat()
        return d


class FundamentalsProvider(Protocol):
    def fetch(self, ticker: str) -> Fundamentals: ...


# --------------------------------------------------------------------------- #
# SEC EDGAR (XBRL companyfacts)
# --------------------------------------------------------------------------- #

# Cada concepto puede venir con distintas etiquetas us-gaap según la empresa.
# Se prueban en orden hasta encontrar una con datos.
CONCEPTS: dict[str, list[str]] = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ],
    "gross_profit": ["GrossProfit"],
    "ebit": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "interest_expense": [
        "InterestExpense",
        "InterestExpenseNonoperating",
        "InterestIncomeExpenseNet",
        "InterestExpenseDebt",
    ],
    "tax_expense": ["IncomeTaxExpenseBenefit"],
    "pretax_income": [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ],
    "cfo": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    ],
    "dividends_paid": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
    "buybacks": ["PaymentsForRepurchaseOfCommonStock"],
    "shares_diluted": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
}

BALANCE_CONCEPTS: dict[str, list[str]] = {
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ],
    "short_term_investments": ["ShortTermInvestments", "MarketableSecuritiesCurrent"],
    "equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "total_assets": ["Assets"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "long_term_debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "short_term_debt": [
        "LongTermDebtCurrent",
        "ShortTermBorrowings",
        "DebtCurrent",
    ],
    "operating_lease": ["OperatingLeaseLiabilityNoncurrent"],
}


class SecEdgarProvider:
    """Lee directamente la API XBRL de la SEC. Sin API key, sin librerías extra.

    Nota: `edgartools` es un envoltorio cómodo sobre exactamente estos mismos
    endpoints. Se usa la API cruda porque su contrato es estable y evita romper
    la app cuando cambia la interfaz de la librería. Si prefieres edgartools,
    sustituye `fetch()` manteniendo la firma.
    """

    BASE = "https://data.sec.gov"
    TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

    def __init__(self, years: int = 5, throttle: float = 0.35):
        self.years = years
        # SEC pide <10 req/s en total. 0.12 bastaba cuando todo era
        # secuencial (~8,3 req/s); ahora que run_universo.py criba en
        # paralelo (ver PARALELISMO_CRIBADO = 3), cada hilo tiene que
        # guardar un hueco mayor para que la SUMA de los 3 siga por
        # debajo de 10 req/s (3 × 1/0.35 ≈ 8,6 req/s).
        self.throttle = throttle
        self._ticker_map: dict[str, dict] | None = None
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"})

    # -- utilidades ------------------------------------------------------- #

    def _get(self, url: str) -> dict:
        time.sleep(self.throttle)
        r = self._session.get(url, timeout=30)
        r.raise_for_status()
        return r.json()

    def _load_ticker_map(self) -> dict[str, dict]:
        global _TICKER_MAP_CACHE, _TICKER_MAP_CACHE_AT
        if self._ticker_map is not None:
            return self._ticker_map

        with _TICKER_MAP_LOCK:
            vencida = (time.monotonic() - _TICKER_MAP_CACHE_AT) > TICKER_MAP_TTL_SEG
            if _TICKER_MAP_CACHE is None or vencida:
                raw = self._get(self.TICKERS_URL)
                _TICKER_MAP_CACHE = {
                    v["ticker"].upper(): {"cik": f'{v["cik_str"]:010d}', "name": v["title"]}
                    for v in raw.values()
                }
                _TICKER_MAP_CACHE_AT = time.monotonic()
                log.info("Listado de tickers EDGAR descargado de nuevo (%d tickers)",
                          len(_TICKER_MAP_CACHE))

        self._ticker_map = _TICKER_MAP_CACHE
        return self._ticker_map

    def resolve_cik(self, ticker: str) -> tuple[str, str]:
        entry = self._load_ticker_map().get(ticker.upper())
        if not entry:
            raise ValueError(f"Ticker no encontrado en EDGAR: {ticker}")
        return entry["cik"], entry["name"]

    def full_ticker_list(self) -> dict[str, dict]:
        """El listado COMPLETO que conoce la SEC (unas 10.000 entradas, no solo
        el S&P 500). Es la puerta de entrada para analizar más allá de una
        lista escrita a mano — ver `universe.py`."""
        return self._load_ticker_map()

    # -- extracción de conceptos ------------------------------------------ #

    @staticmethod
    def _annual_series(facts: dict, tags: list[str], years: int, kind: str) -> dict[int, float]:
        """Devuelve {fiscal_year: valor} para el primer tag con datos.

        kind='flow'  -> hechos de duración anual (10-K, ~365 días)
        kind='stock' -> hechos puntuales de balance
        """
        gaap = facts.get("facts", {}).get("us-gaap", {})
        for tag in tags:
            node = gaap.get(tag)
            if not node:
                continue
            out: dict[int, tuple[str, float]] = {}
            for unit_vals in node.get("units", {}).values():
                for item in unit_vals:
                    if item.get("form") not in ("10-K", "10-K/A", "20-F"):
                        continue
                    fy, fp = item.get("fy"), item.get("fp")
                    if fy is None or fp != "FY":
                        continue
                    if kind == "flow":
                        start, end = item.get("start"), item.get("end")
                        if not start or not end:
                            continue
                        days = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days
                        if not 300 <= days <= 400:      # descarta trimestres y periodos raros
                            continue
                    filed = item.get("filed", "")
                    # el último 'filed' gana (restatements)
                    prev = out.get(fy)
                    if prev is None or filed >= prev[0]:
                        out[fy] = (filed, float(item["val"]))
            if out:
                keep = sorted(out)[-years:]
                return {fy: out[fy][1] for fy in keep}
        return {}

    @staticmethod
    def _fecha_presentacion(facts: dict, tags: list[str], fy_objetivo: int) -> str | None:
        """Fecha ('filed') del 10-K/20-F que contiene el ejercicio fiscal
        `fy_objetivo`. A diferencia de "última presentación de cualquier tipo"
        (que puede ser un trámite sin relación con las cuentas), esto es la
        fecha real de publicación de las cifras que se están usando."""
        gaap = facts.get("facts", {}).get("us-gaap", {})
        for tag in tags:
            node = gaap.get(tag)
            if not node:
                continue
            mejor = None
            for unit_vals in node.get("units", {}).values():
                for item in unit_vals:
                    if item.get("form") not in ("10-K", "10-K/A", "20-F"):
                        continue
                    if item.get("fy") != fy_objetivo or item.get("fp") != "FY":
                        continue
                    start, end = item.get("start"), item.get("end")
                    if not start or not end:
                        continue
                    days = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days
                    if not 300 <= days <= 400:
                        continue
                    filed = item.get("filed", "")
                    if filed and (mejor is None or filed > mejor):
                        mejor = filed
            if mejor:
                return mejor
        return None

    def fetch(self, ticker: str) -> Fundamentals:
        cik, name = self.resolve_cik(ticker)
        facts = self._get(f"{self.BASE}/api/xbrl/companyfacts/CIK{cik}.json")
        submissions = self._get(f"{self.BASE}/submissions/CIK{cik}.json")

        sic = str(submissions.get("sic", ""))
        is_financial = sic.startswith(("60", "61", "62", "63", "64", "65"))

        flows = {k: self._annual_series(facts, tags, self.years, "flow")
                 for k, tags in CONCEPTS.items()}
        stocks = {k: self._annual_series(facts, tags, self.years, "stock")
                  for k, tags in BALANCE_CONCEPTS.items()}

        # eje temporal común: años con ingresos
        fys = sorted(flows["revenue"].keys())
        if not fys:
            gaap_vacio = not facts.get("facts", {}).get("us-gaap")
            tiene_ifrs = bool(facts.get("facts", {}).get("ifrs-full"))
            if gaap_vacio and tiene_ifrs:
                raise ValueError(
                    f"{ticker}: informa bajo IFRS (emisor extranjero, probablemente 20-F), "
                    "no US-GAAP — no soportado todavía, ver limitaciones en el README"
                )
            raise ValueError(f"{ticker}: EDGAR no devuelve serie de ingresos utilizable")

        def serie(key: str, scale: float = M) -> list[float]:
            src = flows.get(key, {})
            return [src[fy] / scale for fy in fys if fy in src] if src else []

        def last(key: str, scale: float = M) -> float | None:
            src = stocks.get(key, {})
            if not src:
                return None
            return src[max(src)] / scale

        debt = (last("long_term_debt") or 0.0) + (last("short_term_debt") or 0.0)
        cash = (last("cash") or 0.0) + (last("short_term_investments") or 0.0)

        fecha_cifras = self._fecha_presentacion(facts, CONCEPTS["revenue"], fys[-1])
        as_of = (
            date.fromisoformat(fecha_cifras) if fecha_cifras
            else date.fromisoformat(submissions.get("filings", {})
                                     .get("recent", {})
                                     .get("filingDate", [date.today().isoformat()])[0])
        )

        f = Fundamentals(
            ticker=ticker.upper(),
            name=name,
            cik=cik,
            country="US",
            currency="USD",
            is_financial=is_financial,
            fiscal_years=fys,
            as_of=as_of,
            revenue=serie("revenue"),
            gross_profit=serie("gross_profit"),
            ebit=serie("ebit"),
            net_income=serie("net_income"),
            interest_expense=[abs(x) for x in serie("interest_expense")],
            tax_expense=serie("tax_expense"),
            pretax_income=serie("pretax_income"),
            cfo=serie("cfo"),
            capex=[abs(x) for x in serie("capex")],
            dividends_paid=[abs(x) for x in serie("dividends_paid")],
            buybacks=[abs(x) for x in serie("buybacks")],
            shares_diluted=serie("shares_diluted"),
            total_debt=debt or None,
            cash=cash or None,
            equity=last("equity"),
            total_assets=last("total_assets"),
            current_liabilities=last("current_liabilities"),
        )
        return f


# --------------------------------------------------------------------------- #
# Finnhub (precio y metadatos)
# --------------------------------------------------------------------------- #

class FinnhubProvider:
    BASE = "https://finnhub.io/api/v1"

    def __init__(self, api_key: str | None = None):
        self.key = api_key or FINNHUB_KEY
        if not self.key:
            raise RuntimeError("Falta FINNHUB_API_KEY")
        self._session = requests.Session()

    def _get(self, path: str, **params) -> dict:
        global _FINNHUB_LAST_CALL
        params["token"] = self.key
        with _FINNHUB_LOCK:
            espera = FINNHUB_MIN_GAP_SEG - (time.monotonic() - _FINNHUB_LAST_CALL)
            if espera > 0:
                time.sleep(espera)
            r = self._session.get(f"{self.BASE}/{path}", params=params, timeout=20)
            _FINNHUB_LAST_CALL = time.monotonic()
        r.raise_for_status()
        return r.json()

    def quote(self, ticker: str) -> float | None:
        d = self._get("quote", symbol=ticker)
        p = d.get("c")
        return float(p) if p else None

    def profile(self, ticker: str) -> dict:
        return self._get("stock/profile2", symbol=ticker)

    def enrich(self, f: Fundamentals) -> Fundamentals:
        """Añade precio, sector y market cap a unos fundamentales de EDGAR."""
        try:
            f.price = self.quote(f.ticker)
            prof = self.profile(f.ticker)
            f.sector = prof.get("finnhubIndustry") or f.sector
            f.name = f.name or prof.get("name", "")
            f.currency = prof.get("currency") or f.currency
            if prof.get("marketCapitalization"):
                f.market_cap = float(prof["marketCapitalization"])   # ya en millones
        except Exception as e:                                       # noqa: BLE001
            log.warning("Finnhub no disponible para %s: %s", f.ticker, e)
        if f.market_cap is None and f.price and f.shares_diluted:
            f.market_cap = f.price * f.shares_diluted[-1]
        return f


# --------------------------------------------------------------------------- #
# Fachada
# --------------------------------------------------------------------------- #

def build_fundamentals(ticker: str, years: int = 5, use_finnhub: bool = True) -> Fundamentals:
    """EDGAR para los estados financieros + Finnhub para el precio."""
    f = SecEdgarProvider(years=years).fetch(ticker)
    if use_finnhub and FINNHUB_KEY:
        f = FinnhubProvider().enrich(f)
    return f


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json
    import sys

    tk = sys.argv[1] if len(sys.argv) > 1 else "MSFT"
    print(json.dumps(build_fundamentals(tk).to_dict(), indent=2, ensure_ascii=False))
