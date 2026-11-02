"""
Recomendador mensual — la parte «de verdad» del módulo mensual.

    python -m mensual.recomendador                       # informe completo (con IA)
    python -m mensual.recomendador --sin-ia --sin-supabase --salida informe.json

Una vez al mes (GitHub Actions) y cuando tú quieras a mano:

  1. Toma TU lista del bot de largo plazo: las empresas con veredicto
     COMPRA_FUERTE, COMPRA o VIGILAR (ya pasaron el cribado fundamental).
  2. Descarga sus precios (Yahoo, gratis) y aplica la regla que se probó en el
     backtest: baratas frente a su propio rango de 5 años, sin estar hundidas,
     habiendo dejado de caer, máx. 4 y máx. 2 por sector.
  3. Pasa las finalistas por tres frenos que SOLO pueden quitar o avisar,
     nunca añadir:  (a) precio por encima del rango de valor de la tesis,
     (b) chequeo del último trimestre publicado (ingresos y margen),
     (c) la IA lee los titulares y dice si la caída parece temporal o
     estructural (veta solo «estructural + confianza alta»).
  4. Guarda el informe en Supabase (tabla `mensual_informes`, migración 006)
     y escribe un resumen en Markdown para el Issue mensual.

LO QUE NO HACE, a propósito:
  · No sabe qué tienes en cartera ni cuánto dinero vas a meter: eso es privado,
    se queda en tu móvil (pestaña «Mensual» del Vigía) y allí se calcula cuánto
    comprar de cada una y qué revisar.
  · No manda vender por precio. El backtest mostró que las tres reglas de venta
    por precio probadas pierden frente al S&P 500; solo se marca como «muy
    cara» a título informativo. Se vende por tesis rota (decisión tuya).

La lógica de decisión es una función pura (`generar_informe`) que recibe los
datos y las funciones externas por parámetro, así se prueba sin red.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd

from .estrategia import Params, decidir_ventas, elegir_candidatas, senales

log = logging.getLogger(__name__)

BENCHMARK = "SPY"
ELEGIBLES = ("COMPRA_FUERTE", "COMPRA", "VIGILAR")
TESIS_ANTIGUA_DIAS = 200          # el bot refresca a los ~180; más allá, tesis vieja
DIAS_SPY = 780                    # ~3 años de cierres diarios del S&P 500 para el marcador
DIAS_PRECIO_VIEJO = 6             # si el último precio es más viejo, aviso


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #

def yahoo(ticker: str) -> str:
    """Formato de Yahoo: BRK.B → BRK-B."""
    return ticker.strip().upper().replace(".", "-")


def primera_frase(md: str | None, largo: int = 280) -> str:
    """Primera frase útil de un informe Markdown (sin cabeceras ni la línea de
    veredicto). Igual que `primeraFrase` del dashboard."""
    if not md:
        return ""
    t = md.strip()
    if t.startswith("```"):
        nl = t.find("\n")
        t = t[nl + 1:] if nl >= 0 else t
        t = t.rstrip("`").strip()
    t = re.sub(r"^#.*$", "", t, flags=re.M)
    for parrafo in re.split(r"\n\s*\n", t):
        p = parrafo.strip()
        if p and not p.startswith("**Veredicto"):
            return re.sub(r"\s+", " ", p.replace("**", ""))[:largo]
    return ""


def _num(x, dec: int = 4):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(x) else round(x, dec)


def _dias_desde(iso: str | None, hoy: date) -> int | None:
    if not iso:
        return None
    try:
        return (hoy - datetime.fromisoformat(iso.replace("Z", "+00:00")).date()).days
    except ValueError:
        return None


def _movimientos(serie: pd.Series) -> dict:
    """Variación a 1 y 3 meses y distancia al máximo de 52 semanas."""
    s = serie.dropna()
    if len(s) < 30:
        return {"ret_1m": None, "ret_3m": None, "desde_maximo_52s": None}
    ult = float(s.iloc[-1])

    def ret(n: int):
        return ult / float(s.iloc[-1 - n]) - 1.0 if len(s) > n else None

    return {"ret_1m": ret(21), "ret_3m": ret(63),
            "desde_maximo_52s": ult / float(s.iloc[-252:].max()) - 1.0}


# --------------------------------------------------------------------------- #
# Núcleo: puro, sin red
# --------------------------------------------------------------------------- #

def generar_informe(
    tesis: list[dict],
    precios: pd.DataFrame,
    p: Params,
    hoy: date,
    *,
    usar_ia: bool = True,
    max_ia: int = 8,
    f_salud=None,          # (ticker) -> dict | None
    f_titulares=None,      # (ticker) -> list[dict]
    f_ia=None,             # (ctx: dict) -> dict
    f_tesis_texto=None,    # (ticker) -> str | None
) -> dict:
    """Construye el informe mensual. `tesis` son filas de `v_tesis_completa`
    (ticker, name, sector, veredicto_final, precio_entrada, valor_min,
    valor_max, nota_fundamental, confianza, analizado_el). `precios` tiene una
    columna por ticker de Yahoo más SPY."""
    from .explicacion import veta

    avisos: list[str] = []
    acciones = precios.drop(columns=[BENCHMARK], errors="ignore")
    if acciones.empty:
        raise ValueError("No hay precios de ninguna empresa.")
    fecha_datos = acciones.index[-1]
    if (pd.Timestamp(hoy) - fecha_datos).days > DIAS_PRECIO_VIEJO:
        avisos.append(f"Los precios más recientes son del {fecha_datos:%d/%m/%Y}: "
                      "mercado cerrado varios días o Yahoo con retraso.")

    sen = senales(acciones, fecha_datos, p)
    cols = list(acciones.columns)
    pos = {c: k for k, c in enumerate(cols)}

    # Datos de la tesis por columna de precios
    por_col: dict[str, dict] = {}
    for fila in tesis:
        y = yahoo(fila["ticker"])
        if y in pos:
            por_col[y] = fila

    sectores = sorted({(f.get("sector") or "").strip() for f in tesis if (f.get("sector") or "").strip()})
    cod = {s: k for k, s in enumerate(sectores)}
    sector_col = np.array(
        [cod.get(((por_col.get(c) or {}).get("sector") or "").strip(), -1) for c in cols], dtype=int)

    desc = sen["descuento"].to_numpy(dtype=float).copy()
    pct = sen["percentil"].to_numpy(dtype=float)
    reb = sen["rebote"].to_numpy(dtype=float)
    elegible = np.array(
        [((por_col.get(c) or {}).get("veredicto_final") in ELEGIBLES) for c in cols], dtype=bool)
    desc_elegibles = np.where(elegible, desc, np.nan)

    # Regla de precio, SIN recorte (n_max/sector se aplican al recorrer, junto con los frenos)
    p_sin_recorte = dataclasses.replace(p, n_max=len(cols) or 1, n_max_sector=0)
    ordenadas = elegir_candidatas(desc_elegibles, pct, reb, p_sin_recorte, None)

    # --- recorrer de mejor a peor aplicando frenos hasta tener n_max --------- #
    aceptadas: list[dict] = []
    vetadas: list[dict] = []
    en_espera: list[dict] = []
    cuenta_sector: dict[int, int] = {}
    llamadas_ia = 0
    ia_agotada = False

    def base(c: str) -> dict:
        f = por_col[c]
        k = pos[c]
        precio = float(sen["precio"].iloc[k])
        vmin, vmax = _num(f.get("valor_min"), 2), _num(f.get("valor_max"), 2)
        pe = _num(f.get("precio_entrada"), 2)
        mid = (vmin + vmax) / 2 if vmin and vmax else None
        return {
            "ticker": f["ticker"], "nombre": f.get("name") or "", "sector": f.get("sector") or "",
            "veredicto": f["veredicto_final"],
            "precio": _num(precio, 2), "mediana": _num(sen["mediana"].iloc[k], 2),
            "descuento": _num(desc[k]), "percentil": _num(pct[k]), "rebote": _num(reb[k]),
            "precio_entrada": pe, "valor_min": vmin, "valor_max": vmax,
            "margen_valor": _num(1 - precio / mid) if mid else None,
            "doble_descuento": bool(pe and precio <= pe),
            "nota": _num(f.get("nota_fundamental"), 1), "confianza_tesis": f.get("confianza"),
            "tesis_dias": _dias_desde(f.get("analizado_el"), hoy),
        }

    for k in ordenadas:
        c = cols[int(k)]
        if c not in por_col:
            continue
        reg = base(c)
        reg["tesis_antigua"] = bool(reg["tesis_dias"] is not None and reg["tesis_dias"] > TESIS_ANTIGUA_DIAS)

        if len(aceptadas) >= p.n_max:
            en_espera.append({"ticker": reg["ticker"], "motivo": f"cumple, pero fuera de las {p.n_max} mejores"})
            continue
        s_cod = int(sector_col[int(k)])
        if p.n_max_sector > 0 and s_cod >= 0 and cuenta_sector.get(s_cod, 0) >= p.n_max_sector:
            en_espera.append({"ticker": reg["ticker"],
                              "motivo": f"cumple, pero ya hay {p.n_max_sector} de su sector"})
            continue
        if reg["valor_max"] and reg["precio"] > reg["valor_max"]:
            vetadas.append({"ticker": reg["ticker"], "motivo":
                            f"precio {reg['precio']:.2f} por encima del rango de valor de su tesis "
                            f"(máx. {reg['valor_max']:.2f})"})
            continue

        # chequeo trimestral (gratis, SEC)
        reg["salud"] = f_salud(reg["ticker"]) if f_salud else None

        # IA: por qué ha caído
        reg["ia"] = None
        if usar_ia and f_ia is not None:
            if llamadas_ia < max_ia:
                llamadas_ia += 1
                tit = f_titulares(reg["ticker"]) if f_titulares else []
                ctx = {**reg, **_movimientos(acciones[c]),
                       "tesis": f_tesis_texto(reg["ticker"]) if f_tesis_texto else None,
                       "titulares": tit}
                reg["ia"] = f_ia(ctx)
                reg["titulares"] = tit[:6]
                if veta(reg["ia"]):
                    vetadas.append({"ticker": reg["ticker"], "motivo":
                                    "la IA ve un problema estructural con confianza alta: "
                                    + (reg["ia"].get("causa") or "")})
                    continue
            else:
                ia_agotada = True

        cuenta_sector[s_cod] = cuenta_sector.get(s_cod, 0) + 1
        aceptadas.append(reg)

    if ia_agotada:
        avisos.append(f"Se agotó el límite de {max_ia} análisis de IA: las últimas candidatas "
                      "van sin explicación de la IA.")
    if usar_ia and f_ia is None:
        avisos.append("Sin IA en esta ejecución (falta la clave de Gemini): las candidatas van "
                      "sin la explicación de «por qué ha caído».")

    for reg in aceptadas:
        reg["peso"] = round(1.0 / len(aceptadas), 4)
        reg["avisos"] = _avisos_candidata(reg)

    # --- todas las empresas, en formato compacto, para revisar la cartera ----- #
    estado_de: dict[str, str] = {r["ticker"]: "candidata" for r in aceptadas}
    estado_de.update({v["ticker"]: "vetada" for v in vetadas})
    estado_de.update({e["ticker"]: "en_espera" for e in en_espera})

    empresas: dict[str, dict] = {}
    for fila in tesis:
        t = fila["ticker"]
        y = yahoo(t)
        e = {"n": fila.get("name") or "", "v": fila["veredicto_final"],
             "pe": _num(fila.get("precio_entrada"), 2), "vmin": _num(fila.get("valor_min"), 2),
             "vmax": _num(fila.get("valor_max"), 2), "sec": fila.get("sector") or "",
             "td": _dias_desde(fila.get("analizado_el"), hoy)}
        k = pos.get(y)
        if k is None or not np.isfinite(sen["precio"].iloc[k]):
            e["e"] = "sin_datos"
        else:
            precio = float(sen["precio"].iloc[k])
            cara = bool(decidir_ventas(
                np.array([True]), np.array([precio]), np.array([float(sen["mediana"].iloc[k])]),
                np.array([float(sen["percentil"].iloc[k])]), np.array([False]), np.array([0.0]),
                dataclasses.replace(p, venta="cara", vender_por_tiempo=False))[0] > 0)
            e.update({"p": _num(precio, 2), "m": _num(sen["mediana"].iloc[k], 2),
                      "d": _num(desc[k], 3), "q": _num(pct[k], 3), "r": _num(reb[k], 3),
                      "c": cara,
                      "sv": bool(e["vmax"] and precio > e["vmax"]),
                      "e": estado_de.get(t, "normal")})
        empresas[t] = e

    n_con_senal = int(np.sum(elegible & np.isfinite(desc)))
    n_elegibles = int(sum(1 for f in tesis if f["veredicto_final"] in ELEGIBLES))
    if n_elegibles and n_con_senal < 0.5 * n_elegibles:
        avisos.append(f"Solo {n_con_senal} de {n_elegibles} empresas de tu lista tienen 5 años de "
                      "precios utilizables; el resto no se puede valorar frente a su historia.")

    spy = precios[BENCHMARK].dropna().iloc[-DIAS_SPY:] if BENCHMARK in precios.columns else pd.Series(dtype=float)

    return {
        "version": 1,
        "generado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mes": f"{hoy:%Y-%m}",
        "datos_a": f"{fecha_datos:%Y-%m-%d}",
        "parametros": {
            "n_max": p.n_max, "n_max_sector": p.n_max_sector, "tope_posicion": p.tope_posicion,
            "min_descuento": p.min_descuento, "max_descuento": p.max_descuento,
            "max_percentil": p.max_percentil, "rebote_min": p.rebote_min,
            "estabilizacion": p.estabilizacion, "cara_percentil": p.cara_percentil,
            "cara_prima": p.cara_prima,
        },
        "resumen": {"n_lista": n_elegibles, "n_con_senal": n_con_senal,
                    "n_candidatas": len(aceptadas), "n_vetadas": len(vetadas),
                    "n_en_espera": len(en_espera)},
        "candidatas": aceptadas,
        "vetadas": vetadas,
        "en_espera": en_espera,
        "empresas": empresas,
        "spy": {"f": [d.strftime("%Y-%m-%d") for d in spy.index], "c": [round(float(x), 2) for x in spy]},
        "avisos": avisos,
    }


def _avisos_candidata(reg: dict) -> list[str]:
    a: list[str] = []
    if reg.get("tesis_antigua"):
        a.append(f"La tesis tiene {reg['tesis_dias']} días: se refrescará en el próximo ciclo; "
                 "mírala antes de comprar.")
    s = reg.get("salud")
    if s and s.get("deterioro"):
        a.append("Último trimestre con deterioro: " + "; ".join(s["avisos"]) + ".")
    if reg["veredicto"] == "VIGILAR" and not reg.get("doble_descuento") and reg.get("precio_entrada"):
        a.append(f"Es VIGILAR y aún está por encima de su precio de entrada ({reg['precio_entrada']:.2f}).")
    ia = reg.get("ia")
    if ia and not ia.get("error") and ia["tipo"] == "estructural":
        a.append("La IA ve un posible problema estructural (confianza " + ia["confianza"] + ").")
    return a


# --------------------------------------------------------------------------- #
# Markdown para el Issue (solo información pública: nada de tu cartera)
# --------------------------------------------------------------------------- #

def issue_markdown(inf: dict, url_dashboard: str | None = None) -> str:
    L: list[str] = []
    A = L.append
    r = inf["resumen"]
    A(f"## Oportunidades de {inf['mes']}\n")
    A(f"Precios a {inf['datos_a']}. De las {r['n_lista']} empresas de tu lista "
      f"(COMPRA/VIGILAR), {r['n_con_senal']} tienen historia suficiente para compararlas "
      "con su propio rango de 5 años.\n")
    cands = inf["candidatas"]
    if not cands:
        A("**Este mes no hay nada claramente barato frente a su propio historial.** "
          "No hace falta forzar ninguna compra: el dinero puede esperar.\n")
    else:
        A("| Empresa | Precio | Bajo su mediana 5a | Rango de valor (tesis) | Tesis | ¿Por qué ha caído? (IA) |")
        A("|---|---:|---:|---:|---|---|")
        for c in cands:
            rango = (f"{c['valor_min']:.0f}–{c['valor_max']:.0f}" if c["valor_min"] and c["valor_max"] else "—")
            ia = c.get("ia")
            tipo = "—" if not ia or ia.get("error") else f"{ia['tipo']} ({ia['confianza']})"
            estrella = " ⭐" if c.get("doble_descuento") else ""
            A(f"| **{c['ticker']}**{estrella} · {c['nombre']} | {c['precio']:.2f} | "
              f"{c['descuento'] * 100:.0f} % | {rango} | {c['veredicto'].replace('_', ' ')} | {tipo} |")
        A("")
        A("⭐ = además está por debajo del precio de entrada que calculó la tesis (doble descuento).\n")
        for c in cands:
            A(f"### {c['ticker']} — {c['nombre']}")
            ia = c.get("ia")
            if ia and not ia.get("error"):
                A(f"- **Causa probable:** {ia['causa']}")
                if ia.get("riesgo_clave"):
                    A(f"- **Riesgo clave:** {ia['riesgo_clave']}")
                if ia.get("que_vigilar"):
                    A(f"- **Qué vigilar:** {ia['que_vigilar']}")
            s = c.get("salud")
            if s:
                A(f"- **Último trimestre:** {s['resumen']}")
            for av in c.get("avisos", []):
                A(f"- ⚠️ {av}")
            A("")
        A(f"**Reparto orientativo:** a partes iguales ({100 / len(cands):.0f} % cada una), con tope "
          f"por empresa y máximo {inf['parametros']['n_max_sector']} por sector. "
          "Cuánto compras de cada una depende de tu importe y de lo que ya tengas: "
          "se calcula en tu móvil.\n")
    if inf["vetadas"]:
        A("**Pasaron el filtro de precio pero las frenó otro chequeo:**")
        for v in inf["vetadas"]:
            A(f"- {v['ticker']}: {v['motivo']}")
        A("")
    for av in inf["avisos"]:
        A(f"> ⚠️ {av}")
    if inf["avisos"]:
        A("")
    if url_dashboard:
        A(f"👉 [Abrir el Vigía → pestaña Mensual]({url_dashboard}) para ver cuánto comprar con tu "
          "importe y revisar tu cartera.\n")
    A("_Información automática, no asesoramiento financiero. Esta regla (comprar descuentos en "
      "empresas ya cribadas) está en fase de prueba; el marcador del Vigía compara tu resultado "
      "real con el S&P 500._")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# E/S: Supabase y Yahoo
# --------------------------------------------------------------------------- #

COLUMNAS_TESIS = ("ticker,name,sector,veredicto_final,precio_entrada,valor_min,valor_max,"
                  "nota_fundamental,confianza,analizado_el")


def cargar_tesis(repo) -> list[dict]:
    filas: list[dict] = []
    a = 0
    while True:
        res = (repo.db.table("v_tesis_completa").select(COLUMNAS_TESIS).range(a, a + 999).execute())
        parte = res.data or []
        filas.extend(parte)
        if len(parte) < 1000:
            return filas
        a += 1000


def texto_tesis(repo, ticker: str) -> str | None:
    try:
        res = (repo.db.table("v_tesis_completa").select("report_md").eq("ticker", ticker).limit(1).execute())
        return primera_frase(res.data[0]["report_md"]) if res.data else None
    except Exception as e:  # noqa: BLE001
        log.warning("Sin texto de tesis para %s: %s", ticker, e)
        return None


def descargar(tickers: list[str], anios: int = 6) -> pd.DataFrame:
    from .precios import MIN_COBERTURA, descargar_precios, limpiar

    ys = sorted({yahoo(t) for t in tickers} | {BENCHMARK})
    desde = f"{date.today().year - anios}-{date.today():%m-%d}"
    bruto = descargar_precios(ys, desde)
    if bruto.empty or BENCHMARK not in bruto.columns:
        raise RuntimeError("No se pudo descargar ni el SPY: Yahoo Finance está bloqueando o caído. "
                           "Reintenta en unas horas.")
    cobertura = len(set(bruto.columns) & set(ys)) / len(ys)
    if cobertura < MIN_COBERTURA:
        raise RuntimeError(f"Solo se descargó el {cobertura:.0%} de los precios (mínimo "
                           f"{MIN_COBERTURA:.0%}). Un informe con tantos datos a medias no sería fiable.")
    return limpiar(bruto)


def main() -> None:
    ap = argparse.ArgumentParser(description="Recomendador mensual de oportunidades.")
    ap.add_argument("--sin-ia", action="store_true", help="no llamar a Gemini ni a Finnhub")
    ap.add_argument("--max-ia", type=int, default=8, help="máximo de análisis de IA por ejecución")
    ap.add_argument("--sin-supabase", action="store_true", help="no guardar el informe en la base de datos")
    ap.add_argument("--salida", default="informe_mensual.json", help="dónde escribir el JSON")
    ap.add_argument("--md", default="issue_mensual.md", help="dónde escribir el Markdown del Issue")
    ap.add_argument("--url-dashboard", default=None, help="enlace al Vigía para el Issue")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from pipeline import Repo
    from .explicacion import explicar_caida, titulares
    from .salud import obtener_salud

    repo = Repo()
    tesis = cargar_tesis(repo)
    if not tesis:
        sys.exit("No hay ninguna empresa analizada en Supabase todavía.")
    print(f"{len(tesis)} empresas analizadas; descargando precios...")
    precios = descargar([f["ticker"] for f in tesis])

    hay_ia = (not args.sin_ia) and bool(os.getenv("GEMINI_API_KEY") or os.getenv("GEMINI_API_KEYS"))
    inf = generar_informe(
        tesis, precios, Params(), date.today(),
        usar_ia=not args.sin_ia, max_ia=args.max_ia,
        f_salud=obtener_salud,
        f_titulares=None if args.sin_ia else titulares,
        f_ia=explicar_caida if hay_ia else None,
        f_tesis_texto=lambda t: texto_tesis(repo, t),
    )

    with open(args.salida, "w", encoding="utf-8") as f:
        json.dump(inf, f, ensure_ascii=False)
    with open(args.md, "w", encoding="utf-8") as f:
        f.write(issue_markdown(inf, args.url_dashboard))

    if not args.sin_supabase:
        repo.db.table("mensual_informes").insert({"mes": inf["mes"], "informe": inf}).execute()
        print("Informe guardado en Supabase (mensual_informes).")

    r = inf["resumen"]
    print(f"\nCandidatas: {[c['ticker'] for c in inf['candidatas']]} · vetadas: "
          f"{[v['ticker'] for v in inf['vetadas']]} · lista {r['n_lista']} · con señal {r['n_con_senal']}")
    for a in inf["avisos"]:
        print("AVISO:", a)


if __name__ == "__main__":
    main()
