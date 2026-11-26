"""Lista de la compra del mes para Trading 212 (la lee el script «Vigia Compra» del iPhone).

Se publica UNA vez al mes: el primer día de bolsa de EE. UU. del mes, a las 15:07 UTC
(con la bolsa abierta tanto en horario de verano como de invierno), con los precios de
ese momento:

    docs/compra/lista.json             la última real (la que lee el iPhone)
    docs/compra/lista-<AAAA-MM>.json   archivo de cada mes
    docs/compra/lista-prueba.json      la de PRUEBA (--forzar): solo la usa la cuenta Practice

Lleva, por sección, las empresas elegidas con su ticker de Trading 212 y su precio, el
cambio euro/dólar del BCE y el tope por empresa. NO lleva importes: cuánto inviertes y
cómo lo repartes lo decides en Vigía y vive solo en tu iPhone. El script del iPhone
reparte tu total entre secciones y empresas con la misma regla que Vigía (a partes
iguales, sin pasar del tope por empresa contando lo que ya tienes) y lanza una orden de
compra a mercado por empresa. Solo compra: nunca vende.

Por qué hay dólares: tú pagas en euros y Trading 212 convierte solo, pero su API solo
admite órdenes en número de acciones (no en euros), así que el iPhone calcula cuántas
acciones son con el precio en dólares y el cambio. Se gasta el 98 % (MARGEN) para que
una subida del precio o la comisión de cambio no dejen la última orden sin saldo.

Una lista generada a mano con --forzar va a lista-prueba.json, lleva "prueba": true y nunca toca
la real: la cuenta real del iPhone ni la mira. lista.json existe siempre (aunque sea vacía) para
que el atajo del iPhone pueda leer su mes sin errores.

Solo datos públicos de empresas: nada de tu cartera ni de tus importes.
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
SCRIPT_MINIMO = 1      # versión mínima del script del iPhone que entiende esta lista
VERSION_LISTA = 2
LISTA_VACIA = {"v": VERSION_LISTA, "mes": "", "prueba": False, "secciones": {},
               "nota": "Aún no hay ninguna lista real publicada."}


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


def secciones_de(inf: dict) -> dict[str, dict]:
    """Candidatas de cada sección. Las que la autocorrección tiene en pausa salen vacías y marcadas."""
    paqs = dict(inf.get("paquetes") or {})
    activos = set(inf.get("paquetes_activos") or paqs.keys())
    out: dict[str, dict] = {}
    for k in SECCIONES:
        if k == "dividendos":
            d = inf.get("dividendos")
            if isinstance(d, dict):
                out[k] = {"cands": list(d.get("candidatas") or []), "pausada": False}
            continue
        if k in paqs:
            activa = k in activos
            out[k] = {"cands": list((paqs[k] or {}).get("candidatas") or []) if activa else [], "pausada": not activa}
    return out


def precio_fiable(vivo: float | None, del_informe: float) -> float:
    """El precio en vivo, salvo que se aleje más de un 35 % del informe (sería un símbolo
    equivocado o un dato roto): entonces, el del informe."""
    if vivo and vivo > 0 and abs(vivo / del_informe - 1) <= 0.35:
        return float(vivo)
    return float(del_informe)


def ordenes_de(cands: list[dict], fuera: set[str], vivos: dict[str, float] | None = None) -> tuple[list[dict], list[str]]:
    """Empresas de una sección con su ticker de Trading 212 y su precio (sin las que no tiene)."""
    vivos = vivos or {}
    vistas, out, quitadas = set(), [], []
    for c in cands:
        t = str(c.get("ticker") or "").upper()
        p = c.get("precio")
        if not t or t in vistas:
            continue
        vistas.add(t)
        if t in fuera:
            quitadas.append(t)
            continue
        if not isinstance(p, (int, float)) or p <= 0:
            continue
        # el ticker exacto que comprobó el informe en Trading 212 («t212»); si no lo trae, el deducido
        out.append({"empresa": t, "ticker": str(c.get("t212") or ticker_t212(t)),
                    "precio_usd": round(precio_fiable(vivos.get(t), float(p)), 4)})
    return out, sorted(quitadas)


def lista_mes(inf: dict, fx: float, fuera: set[str], vivos: dict[str, float] | None = None,
              precios_de: str | None = None, prueba: bool = False) -> dict:
    secs = {}
    for clave, s in secciones_de(inf).items():
        ordenes, quitadas = ordenes_de(s["cands"], fuera, vivos)
        secs[clave] = {"ordenes": ordenes, "pausada": s["pausada"], "quitadas_no_en_trading212": quitadas}
    tope = ((inf.get("parametros") or {}).get("tope_posicion"))
    t2 = inf.get("t212") or {}
    return {
        "v": VERSION_LISTA,
        "script_minimo": SCRIPT_MINIMO,
        "mes": inf.get("mes"),                  # el iPhone solo compra si coincide con el mes actual
        "prueba": bool(prueba),                 # true = generada a mano: nunca se compra con dinero real
        "datos_a": inf.get("datos_a"),
        "generado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "precios_de": precios_de or f"informe ({inf.get('datos_a')})",
        "eurusd": fx,
        "margen": MARGEN,
        "decimales": DECIMALES,
        "tope_posicion": float(tope) if isinstance(tope, (int, float)) and tope > 0 else 0.25,
        "t212_comprobado": bool(t2.get("comprobado")),   # el informe comprobó que todas están en Trading 212
        "secciones": secs,
    }


def informe_de_supabase() -> dict | None:
    """El último informe mensual guardado (tabla mensual_informes)."""
    from pipeline import Repo
    res = (Repo().db.table("mensual_informes").select("creado_el,mes,informe")
           .order("creado_el", desc=True).limit(1).execute())
    return res.data[0]["informe"] if res.data else None


def en_horario_de_bolsa(ahora: datetime) -> bool:
    """Lunes a viernes entre 15:00 y 19:30 UTC: la bolsa de EE. UU. está abierta tanto en horario
    de verano (13:30-20:00 UTC) como de invierno (14:30-21:00 UTC). No sabe de festivos."""
    m = ahora.hour * 60 + ahora.minute
    return ahora.weekday() < 5 and 15 * 60 <= m <= 19 * 60 + 30


def mercado_abierto(fh, ahora: datetime | None = None) -> bool:
    """¿Está abierta ahora la bolsa de EE. UU.? Mira la hora de la última operación del SPY:
    si es de hace menos de 20 minutos, sí (cubre fines de semana y festivos). Si Finnhub no
    responde, decide por el horario (un festivo raro solo haría que la orden espere a la apertura)."""
    ahora = ahora or datetime.now(timezone.utc)
    try:
        d = fh._get("quote", symbol="SPY")
        t = float(d.get("t") or 0)
    except Exception as e:  # noqa: BLE001
        print(f"Finnhub no responde ({e}): se decide por el horario de la bolsa.")
        return en_horario_de_bolsa(ahora)
    return bool(t) and (ahora.timestamp() - t) < 20 * 60


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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--informe", help="informe en JSON (si no, el último de Supabase)")
    ap.add_argument("--salida", required=True, help="carpeta docs/compra")
    ap.add_argument("--eurusd", type=float, help="solo pruebas: cambio fijo")
    ap.add_argument("--precios-vivos", action="store_true",
                    help="precios de este momento (Finnhub) y solo con la bolsa de EE. UU. abierta")
    ap.add_argument("--forzar", action="store_true",
                    help="solo pruebas: genera ya una lista de PRUEBA (nunca se compra con dinero real)")
    a = ap.parse_args(argv)
    inf = json.loads(Path(a.informe).read_text(encoding="utf-8")) if a.informe else informe_de_supabase()
    mes = (inf or {}).get("mes")
    if not mes:
        print("No hay informe (o no trae «mes»): no se escribe la lista de la compra.")
        return 1
    ahora = datetime.now(timezone.utc)
    sal = Path(a.salida)
    archivo = sal / f"lista-{mes}.json"
    if not a.forzar:
        if archivo.exists():
            print(f"La lista real de {mes} ya está publicada: no se toca (el iPhone ya puede haber comprado).")
            return 0
        if mes != f"{ahora:%Y-%m}":
            print(f"El último informe es de {mes}, no de este mes: no se publica lista (no se compra nada).")
            return 0
    vivos, precios_de = {}, None
    if a.precios_vivos:
        from providers import FinnhubProvider
        fh = FinnhubProvider()
        if not a.forzar and not mercado_abierto(fh):
            print("La bolsa de EE. UU. está cerrada ahora (fin de semana o festivo): se intenta el próximo día.")
            return 0
        tickers = sorted({str(c.get("ticker") or "").upper() for s in secciones_de(inf).values() for c in s["cands"]} - {""})
        vivos = precios_vivos(fh, tickers)
        precios_de = f"{ahora:%Y-%m-%d %H:%M} UTC ({len(vivos)} de {len(tickers)} en vivo)"
        print(f"Precios en vivo: {len(vivos)} de {len(tickers)}")
    fx = a.eurusd or eurusd()
    doc = lista_mes(inf, fx, no_disponibles(), vivos, precios_de, prueba=a.forzar)
    sal.mkdir(parents=True, exist_ok=True)
    texto = json.dumps(doc, ensure_ascii=False, indent=1)
    if a.forzar:
        (sal / "lista-prueba.json").write_text(texto, encoding="utf-8")
        if not (sal / "lista.json").exists():
            (sal / "lista.json").write_text(json.dumps(LISTA_VACIA, ensure_ascii=False), encoding="utf-8")
    else:
        archivo.write_text(texto, encoding="utf-8")
        (sal / "lista.json").write_text(texto, encoding="utf-8")
    print(f"Lista de la compra {mes}{' (PRUEBA: solo para la cuenta Practice)' if a.forzar else ''} · 1 € = {fx:.4f} $")
    for k, s in doc["secciones"].items():
        extra = " (en pausa)" if s["pausada"] else ""
        if s["quitadas_no_en_trading212"]:
            extra += f" (quitadas, no están en Trading 212: {', '.join(s['quitadas_no_en_trading212'])})"
        print(f"  {k}: {len(s['ordenes'])} empresas{extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
