"""
Prueba el embudo completo SIN base de datos. Sirve para validar que todo
funciona antes de montar Supabase.

    python3 probar_ia.py MSFT              # solo cribado cuantitativo (gratis)
    python3 probar_ia.py MSFT --ia         # + análisis fundamental (gratis, nivel free de Gemini)
    python3 probar_ia.py MSFT --ia --macro # + capa macro y veredicto final
    python3 probar_ia.py MSFT KO F --ia    # varias empresas

Variables de entorno necesarias:
    SEC_USER_AGENT     obligatoria siempre  -> "TuNombre/1.0 (tu@email.com)"
    FINNHUB_API_KEY    opcional  (sin ella no hay precio, ni PER, ni valoración)
    GEMINI_API_KEY     solo con --ia. Gratis en https://aistudio.google.com/apikey
"""

from __future__ import annotations

import argparse
import os
import sys

from providers import build_fundamentals
from quant_filter import build_llm_payload, screen

SEP = "=" * 66


def aviso_entorno(usa_ia: bool) -> None:
    if not os.getenv("SEC_USER_AGENT"):
        print("AVISO: falta SEC_USER_AGENT. La SEC puede bloquear las peticiones.")
        print('       export SEC_USER_AGENT="TuNombre/1.0 (tu@email.com)"\n')
    if not os.getenv("FINNHUB_API_KEY"):
        print("AVISO: sin FINNHUB_API_KEY no hay precio → sin PER ni valoración.\n")
    if usa_ia and not os.getenv("GEMINI_API_KEY"):
        sys.exit(
            "ERROR: --ia necesita GEMINI_API_KEY.\n"
            "       Consíguela gratis, sin tarjeta, en https://aistudio.google.com/apikey"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description="Prueba local del embudo, sin Supabase.")
    ap.add_argument("tickers", nargs="+", help="p. ej. MSFT KO F")
    ap.add_argument("--ia", action="store_true", help="llama al analista fundamental")
    ap.add_argument("--macro", action="store_true", help="añade la capa macro de sector")
    args = ap.parse_args()

    aviso_entorno(args.ia)
    llamadas_ia = 0

    for tk in args.tickers:
        print(f"\n{SEP}\n{tk}\n{SEP}")

        # ---- capas 1 y 2: datos + filtro cuantitativo (coste 0) ----
        try:
            f = build_fundamentals(tk)
        except Exception as e:  # noqa: BLE001
            print(f"  No se pudieron obtener datos: {e}")
            continue

        r = screen(f)
        print(f"  {f.name} · {f.sector or 'sector n/d'}")
        print(f"  Años disponibles: {f.fiscal_years}")
        print(f"  Cribado: {'PASA' if r.passed else 'DESCARTA'} (score {r.score}/100)")
        for v in r.vetoes:
            print(f"    veto:  {v}")
        for w in r.warnings:
            print(f"    aviso: {w}")
        print(f"  Desglose: {r.breakdown}")

        if not r.passed:
            print("  → No llega a la IA. Coste: 0 tokens.")
            continue

        payload = build_llm_payload(f, r)
        print(f"\n  --- payload al LLM ({len(payload)} caracteres) ---")
        print("  " + payload.replace("\n", "\n  "))

        if not args.ia:
            print("\n  (añade --ia para lanzar el análisis)")
            continue

        # ---- capa macro (opcional) ----
        macro_score, macro_md = None, None
        if args.macro:
            from macro_layer import (
                build_macro_payload,
                call_macro,
                parse_macro,
                target_from_company,
            )

            t = target_from_company(f.sector, f.name)
            print(f"\n  --- MACRO: {t.sector} ---")
            try:
                m = call_macro(build_macro_payload(t))
            except RuntimeError as e:
                print(f"  Gemini ha fallado en la capa macro: {e}")
            else:
                macro_md = m["text"]
                parsed_m = parse_macro(macro_md)
                macro_score = parsed_m.get("score")
                print(macro_md)
                print(f"\n  [macro: {m['input_tokens']} tok entrada / "
                      f"{m['output_tokens']} salida]")

        # ---- capa 3: análisis fundamental ----
        from pipeline import call_claude, parse_report

        print("\n  --- ANÁLISIS FUNDAMENTAL ---")
        try:
            resp = call_claude(payload)
        except RuntimeError as e:
            print(f"  Gemini ha fallado: {e}")
            continue
        print(resp["text"])
        llamadas_ia += 1

        parsed = parse_report(resp["text"])
        print(f"\n  Campos extraídos: {parsed}")
        print(f"  Modelo que ha respondido: {resp.get('model', '?')}")
        print(f"  Tokens: {resp['input_tokens']} entrada / {resp['output_tokens']} salida "
              f"(nivel gratuito de Gemini: coste 0)")

        # ---- veredicto final combinado ----
        if macro_score is not None:
            from macro_layer import combine

            final = combine(
                macro_score,
                parsed.get("verdict", "NO_INVERTIBLE"),
                moat=parsed.get("score_moat"),
            )
            print(f"\n  VEREDICTO FUNDAMENTAL: {final['verdict_fundamental']}")
            print(f"  PUNTUACIÓN MACRO:      {macro_score}/10")
            print(f"  VEREDICTO FINAL:       {final['verdict']}  ({final['ajuste']})")

    if llamadas_ia:
        print(f"\n{SEP}\n{llamadas_ia} llamada(s) a Gemini · nivel gratuito · coste 0 USD")


if __name__ == "__main__":
    main()
