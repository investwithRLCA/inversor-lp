"""
Diagnóstico: empresas de los índices que el bot no puede analizar porque «EDGAR no
devuelve serie de ingresos utilizable». Para cada una mira en la SEC qué conceptos
anuales (10-K) tiene que parezcan ingresos (bancos, aseguradoras, energía…) y resume
cuáles servirían. Solo datos públicos de empresas.

    python diag_ingresos.py            # lee la lista de Supabase (universe_progreso)
    python diag_ingresos.py XOM GS     # o tickers sueltos
"""
from __future__ import annotations

import collections
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

from providers import SecEdgarProvider

PATRON = re.compile(r"(Revenue|Sales|InterestAndDividendIncome|InterestIncome|NoninterestIncome|Premiums|"
                    r"InterestExpense|LeaseIncome|BenefitsLossesAndExpenses|OperatingExpenses|NetIncomeLoss$)", re.I)
DETALLE = 25


def anuales(nodo: dict) -> list[tuple[str, str, float]]:
    out = []
    for vals in nodo.get("units", {}).values():
        for it in vals:
            if it.get("form") not in ("10-K", "10-K/A", "20-F"):
                continue
            ini, fin = it.get("start"), it.get("end")
            if not ini or not fin:
                continue
            d = (datetime.fromisoformat(fin) - datetime.fromisoformat(ini)).days
            if 300 <= d <= 400 and it.get("val") is not None:
                out.append((fin, it.get("filed", ""), float(it["val"])))
    return out


def lista_supabase() -> list[str]:
    from pipeline import Repo
    js = json.loads((Path(__file__).resolve().parent.parent / "docs" / "mercados.json").read_text())
    indices = set()
    for m in ("sp500", "sp400", "sp600", "r2000", "ndx"):
        indices.update((js["mercados"].get(m) or {}).get("tickers") or [])
    repo = Repo()
    filas, desde = [], 0
    while True:
        r = (repo.db.table("universe_progreso").select("ticker, ultimo_error")
             .ilike("ultimo_error", "%ingresos%").range(desde, desde + 999).execute().data or [])
        filas += r
        if len(r) < 1000:
            break
        desde += 1000
    return sorted({f["ticker"] for f in filas if f["ticker"] in indices})


def main() -> None:
    tickers = sys.argv[1:] or lista_supabase()
    print(f"Empresas a revisar: {len(tickers)}")
    sec = SecEdgarProvider()
    hoy = date.today().isoformat()
    resumen = collections.Counter()
    tipos = collections.Counter()
    for t in tickers:
        try:
            cik, nombre = sec.resolve_cik(t)
            facts = sec._get(f"{sec.BASE}/api/xbrl/companyfacts/CIK{cik}.json")
            sub = sec._get(f"{sec.BASE}/submissions/CIK{cik}.json")
        except Exception as e:  # noqa: BLE001
            print(f"{t:6} ERROR {type(e).__name__}: {str(e)[:120]}")
            resumen["error_descarga"] += 1
            continue
        sic = str(sub.get("sic", ""))
        formas = (sub.get("filings", {}).get("recent", {}) or {}).get("form", [])
        n10k = sum(1 for f in formas if f in ("10-K", "10-K/A"))
        n20f = sum(1 for f in formas if f in ("20-F", "40-F"))
        gaap = facts.get("facts", {}).get("us-gaap", {})
        ifrs = bool(facts.get("facts", {}).get("ifrs-full"))
        cands = []
        for tag, nodo in gaap.items():
            if not PATRON.search(tag):
                continue
            a = anuales(nodo)
            if not a:
                continue
            ult = max(a)
            años = len({x[0][:4] for x in a})
            cands.append((ult[0], años, tag, ult[2]))
        cands.sort(key=lambda x: (x[0], x[1]), reverse=True)
        recientes = [c for c in cands if c[0] >= str(int(hoy[:4]) - 2)]
        clase = ("sin_us_gaap_ifrs" if not gaap and ifrs else "sin_us_gaap" if not gaap
                 else "sin_10k" if n10k == 0 and n20f == 0 else "con_datos" if recientes else "datos_viejos")
        resumen[clase] += 1
        for c in recientes:
            tipos[c[2]] += 1
        sucesor = [f for f in formas if f.startswith("8-K12")]
        print(f"{t:6} sic={sic:4} 10K={n10k} 20F/40F={n20f} {clase:16} {sub.get('sicDescription','')[:40]} · {nombre[:40]}"
              f"{' · SUCESORA (' + ','.join(sorted(set(sucesor))) + ')' if sucesor else ''}")
        for fin, años, tag, val in cands[:DETALLE]:
            print(f"         {fin} años={años:2} {tag} = {val/1e6:,.0f} M")
    print("\nResumen:", dict(resumen))
    print("Conceptos con datos recientes (nº de empresas):")
    for tag, n in tipos.most_common(40):
        print(f"  {n:4}  {tag}")


if __name__ == "__main__":
    main()
