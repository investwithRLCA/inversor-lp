"""
Fundamentales «tal como se conocían» en cada fecha (point-in-time).

Para probar en el pasado las reglas de calidad (ROIC, crecimiento, caja, deuda…)
hay que usar las cuentas que YA eran públicas en cada fecha, no las de hoy:
usar las de hoy contaría el futuro como si se supiera. La SEC lo permite porque
cada cifra de «companyfacts» lleva la fecha en que se presentó (`filed`):

  · en cada fecha solo se miran los hechos con `filed` ≤ esa fecha;
  · si una cifra se reformuló después, en esa fecha vale la versión que había
    entonces (la presentada más recientemente, pero antes de esa fecha);
  · se juntan las etiquetas contables de cada concepto por periodo (así los
    cambios de etiqueta, p. ej. con la norma de ingresos de 2018, no recortan
    el histórico).

Se construye el mismo `Fundamentals` que usa el bot, para que el cribado y los
paquetes se ejecuten TAL CUAL en el backtest (mismo código, mismos umbrales).

Diferencias conocidas con el bot en producción (y por qué no importan al juzgar):
  · capitalización = precio de cierre × acciones diluidas del último ejercicio
    (el bot usa la de Finnhub);
  · una cifra de balance con más de 15 meses de antigüedad respecto al último
    ejercicio con ingresos se ignora (en vez de arrastrar un dato rancio).
"""

from __future__ import annotations

import gzip
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from providers import (BALANCE_CONCEPTS, BANK_CONCEPTS, CONCEPTS, M, SIC_BANCA, TODAS_ETIQUETAS_INGRESOS,
                       Fundamentals, SecEdgarProvider, etiquetas_ingresos, fusionar_ingresos, ingresos_banca,
                       usar_ingresos_banca)

log = logging.getLogger(__name__)

FORMULARIOS = ("10-K", "10-K/A", "20-F")
VERSION_CACHE = 3              # 2: inventario (Lynch) · 3: ingresos de bancos, REIT, eléctricas y sucesoras
                               # (el nombre del fichero lleva la versión)
MAX_EDAD_BALANCE_DIAS = 450      # un dato de balance más viejo que esto frente a las ventas se ignora
MAX_EDAD_CUENTAS_DIAS = 640      # si las últimas cuentas publicadas tienen > ~21 meses, la empresa no cuenta


def _dias(a: str, b: str) -> int:
    return (date.fromisoformat(a) - date.fromisoformat(b)).days


# --------------------------------------------------------------------------- #
# 1. Compactar lo que devuelve la SEC (lo que se guarda en disco)
# --------------------------------------------------------------------------- #

def compactar(facts: dict) -> dict:
    """De «companyfacts» se queda con (fin de periodo, fecha de presentación, valor)
    de los hechos anuales de cada concepto y etiqueta. Mucho más pequeño y se lee
    sin red."""
    gaap = (facts or {}).get("facts", {}).get("us-gaap", {})
    out: dict[str, dict[str, list]] = {}
    grupos = dict(CONCEPTS)
    grupos["revenue"] = TODAS_ETIQUETAS_INGRESOS            # también las de sector (se eligen por SIC al usarlas)
    grupos.update({"banca_" + k: v for k, v in BANK_CONCEPTS.items()})
    for grupo, tags in list(grupos.items()) + list(BALANCE_CONCEPTS.items()):
        es_balance = grupo in BALANCE_CONCEPTS
        por_tag: dict[str, list] = {}
        for tag in tags:
            nodo = gaap.get(tag)
            if not nodo:
                continue
            filas: list[list] = []
            for vals in nodo.get("units", {}).values():
                for it in vals:
                    if it.get("form") not in FORMULARIOS:
                        continue
                    fin, pres = it.get("end"), it.get("filed")
                    if not fin or not pres or it.get("val") is None:
                        continue
                    if not es_balance:
                        ini = it.get("start")
                        if not ini:
                            continue
                        try:
                            d = _dias(fin, ini)
                        except ValueError:
                            continue
                        if not 300 <= d <= 400:           # fuera trimestres y periodos raros
                            continue
                    try:
                        filas.append([fin, pres, float(it["val"])])
                    except (TypeError, ValueError):
                        continue
            if filas:
                por_tag[tag] = filas
        if por_tag:
            out[grupo] = por_tag
    return out


def _serie_en(por_tag: dict, orden_tags: list[str], corte: str) -> dict[str, float]:
    """{fin de periodo: valor} con lo presentado hasta `corte` (inclusive).
    Por periodo manda la presentación más reciente ≤ corte; entre etiquetas, la
    primera de la lista que tenga ese periodo."""
    res: dict[str, float] = {}
    for tag in orden_tags:
        filas = por_tag.get(tag)
        if not filas:
            continue
        mejor: dict[str, tuple[str, float]] = {}
        for fin, pres, val in filas:
            if pres > corte:
                continue
            p = mejor.get(fin)
            if p is None or pres >= p[0]:
                mejor[fin] = (pres, val)
        for fin, (_, val) in mejor.items():
            res.setdefault(fin, val)
    return res


# --------------------------------------------------------------------------- #
# 2. Fuente point-in-time
# --------------------------------------------------------------------------- #

def factor_splits(splits, desde) -> float:
    """Producto de los splits con fecha POSTERIOR a `desde`.

    Yahoo da los precios antiguos ya ajustados por los splits de después. Las cuentas que
    se presentaron en `desde` usan las acciones de entonces. Para que precio y acciones
    (y PER, capitalización) estén en la misma base: precio de Yahoo × factor_splits(splits,
    fecha de la presentación). Sin esto, una empresa que luego hizo un split de 10 por 1
    parece 10 veces más barata y más pequeña de lo que era: sesgo a favor de las futuras
    ganadoras (las que suben mucho son las que hacen splits)."""
    if splits is None or len(splits) == 0:
        return 1.0
    import pandas as _pd
    posteriores = splits[splits.index > _pd.Timestamp(desde)]
    f = 1.0
    for x in posteriores:
        if x and x > 0:
            f *= float(x)
    return f


class FuentePIT:
    """Datos de la SEC de varias empresas ya descargados (ver `descargar`) y
    capaces de dar los `Fundamentals` de cualquier empresa en cualquier fecha."""

    def __init__(self, datos: dict[str, dict] | None = None, anios: int = 5):
        self.datos: dict[str, dict] = datos or {}
        self.anios = anios
        self._presentaciones: dict[str, list[str]] = {}
        self._cache: dict[tuple[str, str], tuple[Fundamentals, str] | None] = {}

    # -- disco -------------------------------------------------------------- #
    @staticmethod
    def _ruta(carpeta: Path, ticker: str) -> Path:
        return Path(carpeta) / f"{ticker.replace('/', '_')}.v{VERSION_CACHE}.json.gz"

    @classmethod
    def cargar(cls, carpeta: Path, tickers: list[str], anios: int = 5) -> "FuentePIT":
        datos = {}
        for t in tickers:
            f = cls._ruta(carpeta, t)
            if not f.exists():
                continue
            try:
                with gzip.open(f, "rt", encoding="utf-8") as fh:
                    d = json.load(fh)
                if d.get("version") == VERSION_CACHE and d.get("conceptos"):
                    datos[t] = d
            except (OSError, ValueError):
                continue
        return cls(datos, anios)

    @classmethod
    def descargar(cls, tickers: list[str], carpeta: Path, hilos: int = 3,
                  throttle: float = 0.35, anios: int = 5, max_edad_dias: int | None = None) -> "FuentePIT":
        """Baja de la SEC lo que falte en `carpeta` (las ya bajadas no se repiten, salvo que
        tengan más de `max_edad_dias`: así una prueba que se repite cada mes ve las cuentas nuevas)."""
        carpeta = Path(carpeta)
        carpeta.mkdir(parents=True, exist_ok=True)

        def falta(t: str) -> bool:
            f = cls._ruta(carpeta, t)
            if not f.exists():
                return True
            return bool(max_edad_dias) and (time.time() - f.stat().st_mtime) > max_edad_dias * 86400
        pendientes = [t for t in tickers if falta(t)]
        print(f"SEC: {len(tickers) - len(pendientes)} empresas ya en caché, {len(pendientes)} por descargar.")
        fallos: dict[str, str] = {}
        hecho = [0]

        def uno(t: str, prov: SecEdgarProvider):
            try:
                cik, nombre = prov.resolve_cik(t)
                facts = prov.companyfacts(t, cik)
                try:
                    sic = str(prov._get(f"{prov.BASE}/submissions/CIK{cik}.json").get("sic", ""))
                except Exception:        # noqa: BLE001  (el SIC es secundario)
                    sic = ""
                datos = {"version": VERSION_CACHE, "ticker": t, "nombre": nombre, "cik": cik, "sic": sic,
                         "conceptos": compactar(facts)}
                with gzip.open(cls._ruta(carpeta, t), "wt", encoding="utf-8") as fh:
                    json.dump(datos, fh)
            except Exception as e:       # noqa: BLE001
                fallos[t] = str(e)[:120]
            hecho[0] += 1
            if hecho[0] % 50 == 0:
                print(f"  SEC {hecho[0]}/{len(pendientes)}")

        if pendientes:
            # un proveedor (y por tanto un hueco de tiempo) por hilo: la suma queda < 10 peticiones/s
            provs = [SecEdgarProvider(years=anios, throttle=throttle) for _ in range(max(1, hilos))]
            provs[0]._load_ticker_map()
            trozos = [pendientes[i::len(provs)] for i in range(len(provs))]

            def trabajo(k: int):
                for t in trozos[k]:
                    uno(t, provs[k])

            with ThreadPoolExecutor(max_workers=len(provs)) as ex:
                list(ex.map(trabajo, range(len(provs))))
            if fallos:
                print(f"SEC: {len(fallos)} empresas sin datos utilizables: "
                      + ", ".join(sorted(fallos)[:30]) + ("…" if len(fallos) > 30 else ""))
        return cls.cargar(carpeta, tickers, anios)

    # -- consulta ----------------------------------------------------------- #
    def ultima_presentacion(self, ticker: str, fecha) -> str | None:
        """Fecha de la última presentación a la SEC hasta `fecha` (o None)."""
        if ticker not in self.datos:
            return None
        corte = fecha.strftime("%Y-%m-%d") if hasattr(fecha, "strftime") else str(fecha)[:10]
        return self._ultima_presentacion(ticker, corte)

    def _ultima_presentacion(self, ticker: str, corte: str) -> str | None:
        """Fecha de la última presentación a la SEC hasta `corte` (lo que cambia los datos)."""
        if ticker not in self._presentaciones:
            fechas = {pres for por_tag in self.datos[ticker]["conceptos"].values()
                      for filas in por_tag.values() for _, pres, _ in filas}
            self._presentaciones[ticker] = sorted(fechas)
        lista = self._presentaciones[ticker]
        import bisect
        i = bisect.bisect_right(lista, corte)
        return lista[i - 1] if i else None

    def fundamentales(self, ticker: str, fecha, precio: float | None) -> Fundamentals | None:
        """`Fundamentals` de `ticker` con lo publicado hasta `fecha`. None si
        aún no había cuentas anuales suficientes (o dejó de publicarlas).

        Entre dos presentaciones a la SEC las cuentas no cambian: se calculan una vez
        por presentación y se reutilizan (solo cambian el precio y la capitalización)."""
        d = self.datos.get(ticker)
        if not d:
            return None
        corte = fecha.strftime("%Y-%m-%d") if hasattr(fecha, "strftime") else str(fecha)[:10]
        clave = self._ultima_presentacion(ticker, corte)
        if clave is None:
            return None
        if (ticker, clave) not in self._cache:
            self._cache[(ticker, clave)] = self._base(ticker, clave)
        base = self._cache[(ticker, clave)]
        if base is None:
            return None
        f0, fin_ultimo = base
        if _dias(corte, fin_ultimo) > MAX_EDAD_CUENTAS_DIAS:
            return None
        import dataclasses as _dc
        f = _dc.replace(f0, as_of=date.fromisoformat(corte))
        if precio and precio > 0:
            f.price = float(precio)
            if f.shares_diluted:
                f.market_cap = f.price * f.shares_diluted[-1]
        return f

    def _base(self, ticker: str, corte: str) -> tuple[Fundamentals, str] | None:
        """Las cuentas con lo presentado hasta `corte`, sin precio. (Fundamentals, fin del último ejercicio)."""
        d = self.datos[ticker]
        conc = d["conceptos"]
        flujos = {k: _serie_en(conc.get(k, {}), CONCEPTS[k], corte) for k in CONCEPTS}
        saldos = {k: _serie_en(conc.get(k, {}), BALANCE_CONCEPTS[k], corte) for k in BALANCE_CONCEPTS}
        sic = str(d.get("sic") or "")
        tags = etiquetas_ingresos(sic)
        ingresos = fusionar_ingresos({t: _serie_en(conc.get("revenue", {}), [t], corte) for t in tags}, tags)
        if sic.startswith(SIC_BANCA):
            b = {k: _serie_en(conc.get("banca_" + k, {}), t, corte) for k, t in BANK_CONCEPTS.items()}
            banca = ingresos_banca(b["nii"], b["interest_income"], b["interest_expense"], b["noninterest_income"])
            if usar_ingresos_banca(ingresos, banca, flujos.get("net_income") or {}):
                ingresos = banca
        flujos["revenue"] = ingresos
        if not ingresos:
            return None
        n = self.anios
        fines = sorted(ingresos)[-n:]

        def serie(clave: str) -> list[float]:
            src = flujos.get(clave, {})
            if not src:
                return []
            propios = sorted(src)[-n:]              # como el bot: los últimos n de su propia serie…
            return [src[e] / M for e in fines if e in propios]   # …alineados con los años con ingresos

        def ultimo(clave: str) -> float | None:
            src = saldos.get(clave, {})
            if not src:
                return None
            fin = max(src)
            if _dias(fines[-1], fin) > MAX_EDAD_BALANCE_DIAS:
                return None
            return src[fin] / M

        deuda = (ultimo("long_term_debt") or 0.0) + (ultimo("short_term_debt") or 0.0)
        caja = (ultimo("cash") or 0.0) + (ultimo("short_term_investments") or 0.0)
        sic = d.get("sic", "")
        f = Fundamentals(
            ticker=ticker.upper(), name=d.get("nombre", ""), cik=d.get("cik"), country="US", currency="USD",
            is_financial=sic.startswith(("60", "61", "62", "63", "64", "65")),
            fiscal_years=[int(e[:4]) for e in fines],
            as_of=date.fromisoformat(corte),
            revenue=serie("revenue"), gross_profit=serie("gross_profit"), ebit=serie("ebit"),
            net_income=serie("net_income"),
            interest_expense=[abs(x) for x in serie("interest_expense")],
            tax_expense=serie("tax_expense"), pretax_income=serie("pretax_income"),
            cfo=serie("cfo"), capex=[abs(x) for x in serie("capex")],
            dividends_paid=[abs(x) for x in serie("dividends_paid")],
            buybacks=[abs(x) for x in serie("buybacks")],
            shares_diluted=serie("shares_diluted"),
            total_debt=deuda or None, cash=caja or None, equity=ultimo("equity"),
            total_assets=ultimo("total_assets"), current_liabilities=ultimo("current_liabilities"),
            inventory=([saldos["inventory"][e] / M if e in saldos["inventory"] else None for e in fines]
                       if saldos.get("inventory") else []),
        )
        return f, fines[-1]
