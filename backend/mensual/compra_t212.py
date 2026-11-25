"""Lista de la compra del mes para Trading 212 (la lee el atajo «Vigia Compra» del iPhone).

Se publica UNA vez al mes: el primer día de bolsa de EE. UU. del mes, a las 15:07 UTC
(con la bolsa abierta tanto en horario de verano como de invierno), con los precios de
ese momento. Escribe un fichero por sección en docs/compra/:

    docs/compra/<seccion>-<AAAA-MM>.json   (archivo de cada mes)
    docs/compra/<seccion>.json             (la última; es la que lee el atajo)

con las empresas elegidas, su peso y cuántas acciones tocan por cada 100 € («por100»).
El atajo del iPhone abre la última lista y solo compra si es la del mes en curso; multiplica por tu importe (que vive
solo en el atajo), redondea hacia abajo a 3 decimales y lanza una orden de compra a mercado
por empresa. Si el informe de ese mes no se generó, no hay lista nueva y no se compra nada.

Las acciones de EE. UU. cotizan en dólares y la API de Trading 212 solo admite órdenes en
número de acciones (no en euros): por eso «por100» usa el cambio euro/dólar del BCE. Tú
pagas en euros; Trading 212 convierte solo. MARGEN deja un 2 % sin gastar para que una
subida del precio o la comisión de cambio no dejen la última orden sin saldo.

Solo datos públicos de empresas: nada de tu cartera ni de tu importe.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

AQUI = Path(__file__).resolve().parent
NO_DISPONIBLES = AQUI / "t212_no_disponibles.txt"   # tickers que Trading 212 no tiene
SECCIONES = ("lynch_p", "calidad_g", "lynch_g", "dividendos")
MARGEN = 0.98          # se gasta el 98 % del importe
DECIMALES = 3          # acciones con 3 decimales (Trading 212 admite hasta 4)


def ticker_t212(t: str) -> str:
    """AAPL → AAPL_US_EQ, BRK.B → BRK_B_US_EQ (igual que tickerT212 en docs/mensual.js)."""
    s = str(t or "").strip().upper()
    for c in ".-/ ":
        s = s.replace(c, "_")
    while "__" in s:
        s = s.replace("__", "_")
    return s + "_US_EQ"


def no_disponibles() -> set[str]:
    if not NO_DISPONIBLES.exists():
        return set()
    out = set()
    for linea in NO_DISPONIBLES.read_text(encoding="utf-8").splitlines():
        linea = linea.split("#", 1)[0].strip().upper()
        if linea:
            out.add(linea)
    return out


def eurusd() -> float:
    """Dólares por euro (BCE vía Frankfurter; si falla, el XML diario del BCE)."""
    errores = []
    try:
        with urllib.request.urlopen("https://api.frankfurter.app/latest?from=EUR&to=USD", timeout=20) as r:
            v = float(json.load(r)["rates"]["USD"])
            if 0.5 < v < 2.5:
                return v
    except Exception as e:  # noqa: BLE001
        errores.append(f"frankfurter: {e}")
    try:
        with urllib.request.urlopen("https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml", timeout=20) as r:
            xml = r.read().decode("utf-8", "replace")
        i = xml.index("currency='USD'")
        j = xml.index("rate='", i) + 6
        v = float(xml[j:xml.index("'", j)])
        if 0.5 < v < 2.5:
            return v
    except Exception as e:  # noqa: BLE001
        errores.append(f"bce: {e}")
    raise RuntimeError("No se pudo obtener el cambio euro/dólar: " + "; ".join(errores))


def secciones_de(inf: dict) -> dict[str, list[dict]]:
    """Candidatas de cada sección activa (las pausadas por la autocorrección salen vacías)."""
    paqs = dict(inf.get("paquetes") or {})
    activos = set(inf.get("paquetes_activos") or paqs.keys())
    out: dict[str, list[dict]] = {}
    for k in SECCIONES:
        if k == "dividendos":
            d = inf.get("dividendos")
            if isinstance(d, dict):
                out[k] = list(d.get("candidatas") or [])
            continue
        if k in paqs:
            out[k] = list((paqs[k] or {}).get("candidatas") or []) if k in activos else []
    return out


def pesos(cands: list[dict], fuera: set[str]) -> dict[str, float]:
    """Peso del informe (a partes iguales si no lo trae), sin las que Trading 212 no tiene."""
    base = {}
    for c in cands:
        t = str(c.get("ticker") or "").upper()
        p = c.get("precio")
        if not t or t in fuera or not isinstance(p, (int, float)) or p <= 0:
            continue
        w = c.get("peso")
        base[t] = float(w) if isinstance(w, (int, float)) and w > 0 else 1.0
    total = sum(base.values())
    return {t: v / total for t, v in base.items()} if total > 0 else {}


def lista(inf: dict, clave: str, cands: list[dict], fx: float, fuera: set[str],
          vivos: dict[str, float] | None = None, precios_de: str | None = None) -> dict:
    w = pesos(cands, fuera)
    vivos = vivos or {}
    precio = {}
    for c in cands:
        t = str(c.get("ticker") or "").upper()
        if t in w:
            precio[t] = precio_fiable(vivos.get(t), float(c["precio"]))
    ordenes = [{
        "empresa": t,
        "ticker": ticker_t212(t),
        "peso": round(w[t], 6),
        "precio_usd": round(precio[t], 4),
        # acciones por cada 100 € invertidos en la sección (gastando el 98 %)
        "por100": round(100 * MARGEN * fx * w[t] / precio[t], 8),
    } for t in sorted(w, key=lambda x: -w[x])]
    quitadas = sorted({str(c.get("ticker") or "").upper() for c in cands} & fuera)
    return {
        "seccion": clave,
        "mes": inf.get("mes"),                 # el atajo solo compra si coincide con el mes actual
        "datos_a": inf.get("datos_a"),
        "generado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "precios_de": precios_de or f"informe ({inf.get('datos_a')})",
        "eurusd": fx,
        "margen": MARGEN,
        "decimales": DECIMALES,
        "ordenes": ordenes,
        "quitadas_no_en_trading212": quitadas,
        "nota": ("Sección en pausa este mes: no se compra nada." if not ordenes else
                 "Compra a mercado: acciones = por100 × importe en euros / 100, redondeado hacia abajo a 3 decimales."),
    }


def informe_de_supabase() -> dict | None:
    """El último informe mensual guardado (tabla mensual_informes)."""
    from pipeline import Repo
    res = (Repo().db.table("mensual_informes").select("creado_el,mes,informe")
           .order("creado_el", desc=True).limit(1).execute())
    return res.data[0]["informe"] if res.data else None


def mercado_abierto(fh) -> bool:
    """¿Está abierta ahora la bolsa de EE. UU.? Mira la hora de la última operación del SPY:
    si es de hace menos de 20 minutos, sí (cubre fines de semana y festivos)."""
    d = fh._get("quote", symbol="SPY")
    t = d.get("t") or 0
    return bool(t) and (datetime.now(timezone.utc).timestamp() - float(t)) < 20 * 60


def precios_vivos(fh, tickers: list[str]) -> dict[str, float]:
    out = {}
    for t in tickers:
        try:
            p = fh.quote(t)
            if not p and "-" in t:                       # BRK-B (SEC) → BRK.B (Finnhub)
                p = fh.quote(t.replace("-", "."))
            if p and p > 0:
                out[t] = float(p)
        except Exception as e:  # noqa: BLE001
            print(f"  sin precio en vivo para {t} ({e}): se usa el del informe")
    return out


def precio_fiable(vivo: float | None, del_informe: float) -> float:
    """El precio en vivo, salvo que se aleje más de un 35 % del informe (sería un símbolo
    equivocado o un dato roto): entonces, el del informe."""
    if vivo and vivo > 0 and abs(vivo / del_informe - 1) <= 0.35:
        return float(vivo)
    return float(del_informe)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--informe", help="informe en JSON (si no, el último de Supabase)")
    ap.add_argument("--salida", required=True, help="carpeta docs/compra")
    ap.add_argument("--eurusd", type=float, help="solo pruebas: cambio fijo")
    ap.add_argument("--precios-vivos", action="store_true",
                    help="precios de este momento (Finnhub) y solo con la bolsa de EE. UU. abierta")
    ap.add_argument("--forzar", action="store_true",
                    help="solo pruebas: aunque la bolsa esté cerrada o la lista del mes ya exista")
    a = ap.parse_args(argv)
    inf = json.loads(Path(a.informe).read_text(encoding="utf-8")) if a.informe else informe_de_supabase()
    mes = (inf or {}).get("mes")
    if not mes:
        print("No hay informe (o no trae «mes»): no se escribe la lista de la compra.")
        return 1
    ahora = datetime.now(timezone.utc)
    if not a.forzar and mes != f"{ahora:%Y-%m}":
        print(f"El último informe es de {mes}, no de este mes: no se publica lista (no se compra nada).")
        return 0
    sal = Path(a.salida)
    if not a.forzar and sal.exists() and any(sal.glob(f"*-{mes}.json")):
        print(f"La lista de {mes} ya está publicada: no se vuelve a generar (el atajo ya puede haber comprado).")
        return 0
    vivos, precios_de = {}, None
    secciones = secciones_de(inf)
    if a.precios_vivos:
        from providers import FinnhubProvider
        fh = FinnhubProvider()
        if not a.forzar and not mercado_abierto(fh):
            print("La bolsa de EE. UU. está cerrada ahora (fin de semana o festivo): se intenta el próximo día.")
            return 0
        tickers = sorted({str(c.get("ticker") or "").upper() for cs in secciones.values() for c in cs} - {""})
        vivos = precios_vivos(fh, tickers)
        precios_de = f"{ahora:%Y-%m-%d %H:%M} UTC ({len(vivos)} de {len(tickers)} en vivo)"
        print(f"Precios en vivo: {len(vivos)} de {len(tickers)}")
    fx = a.eurusd or eurusd()
    fuera = no_disponibles()
    sal.mkdir(parents=True, exist_ok=True)
    for clave, cands in secciones.items():
        doc = lista(inf, clave, cands, fx, fuera, vivos, precios_de)
        texto = json.dumps(doc, ensure_ascii=False, indent=1)
        (sal / f"{clave}-{mes}.json").write_text(texto, encoding="utf-8")   # archivo del mes (lo usa «Apuntar»)
        (sal / f"{clave}.json").write_text(texto, encoding="utf-8")         # la última (la lee el atajo)
        print(f"Compra {clave} {mes}: {len(doc['ordenes'])} empresas"
              + (f" (quitadas, no están en Trading 212: {', '.join(doc['quitadas_no_en_trading212'])})" if doc["quitadas_no_en_trading212"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
