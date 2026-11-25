"""Lista de la compra del mes para Trading 212 (la lee el atajo «Vigia Compra» del iPhone).

Tras generar el informe mensual, escribe un fichero por sección en docs/compra/:

    docs/compra/<seccion>-<AAAA-MM>.json

con las empresas elegidas, su peso y cuántas acciones tocan por cada 100 € («por100»).
El atajo del iPhone, el día que tú elijas, abre el fichero DEL MES EN CURSO, multiplica
por tu importe (que vive solo en el atajo) y lanza una orden de compra a mercado por
empresa. Si el informe de ese mes no se generó, el fichero no existe y no se compra nada.

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


def lista(inf: dict, clave: str, cands: list[dict], fx: float, fuera: set[str]) -> dict:
    w = pesos(cands, fuera)
    precio = {str(c.get("ticker") or "").upper(): float(c["precio"]) for c in cands if str(c.get("ticker") or "").upper() in w}
    ordenes = [{
        "empresa": t,
        "ticker": ticker_t212(t),
        "peso": round(w[t], 6),
        "precio_usd": round(precio[t], 4),
        # acciones por cada 100 € invertidos en la sección
        "por100": round(100 * fx * w[t] / precio[t], 8),
    } for t in sorted(w, key=lambda x: -w[x])]
    quitadas = sorted({str(c.get("ticker") or "").upper() for c in cands} & fuera)
    return {
        "seccion": clave,
        "mes": inf.get("mes"),
        "datos_a": inf.get("datos_a"),
        "generado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "eurusd": fx,
        "ordenes": ordenes,
        "quitadas_no_en_trading212": quitadas,
        "nota": ("Sección en pausa este mes: no se compra nada." if not ordenes else
                 "Compra a mercado: acciones = por100 × importe en euros / 100, redondeado hacia abajo."),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--informe", required=True)
    ap.add_argument("--salida", required=True, help="carpeta docs/compra")
    ap.add_argument("--eurusd", type=float, help="solo pruebas: cambio fijo")
    a = ap.parse_args(argv)
    inf = json.loads(Path(a.informe).read_text(encoding="utf-8"))
    mes = inf.get("mes")
    if not mes:
        print("El informe no trae «mes»: no se escribe la lista de la compra.")
        return 1
    fx = a.eurusd or eurusd()
    fuera = no_disponibles()
    sal = Path(a.salida)
    sal.mkdir(parents=True, exist_ok=True)
    for clave, cands in secciones_de(inf).items():
        doc = lista(inf, clave, cands, fx, fuera)
        (sal / f"{clave}-{mes}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"Compra {clave} {mes}: {len(doc['ordenes'])} empresas"
              + (f" (quitadas, no están en Trading 212: {', '.join(doc['quitadas_no_en_trading212'])})" if doc["quitadas_no_en_trading212"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
