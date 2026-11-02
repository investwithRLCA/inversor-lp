"""
«¿Por qué ha caído?» — la pregunta que separa un bajón de un derrumbe.

Para las pocas finalistas del mes (cuatro, ocho como mucho), se le dan a la IA
solo hechos que están en el mensaje: cuánto ha caído y frente a qué, el último
trimestre publicado, la tesis de largo plazo y los titulares de las últimas
semanas (Finnhub, gratis). Se le pide un JSON corto: causa, si parece temporal
o estructural, y con qué confianza.

Reglas de uso (importante, para no engañarse):
  · La IA no tiene acceso a internet aquí: solo ve esos titulares. Si no
    explican la caída, la respuesta correcta es «incierto», y así se le pide.
  · Solo VETA cuando dice «estructural» con confianza «alta». En cualquier otro
    caso la candidata se queda y la explicación aparece como información.
  · Nunca AÑADE candidatas. El núcleo de la selección es la regla de precio
    que se probó en el backtest; esto es un freno, no un acelerador.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger(__name__)

TIPOS = ("temporal", "estructural", "incierto")
CONFIANZAS = ("alta", "media", "baja")

SYSTEM = """Eres un analista de inversión prudente, de largo plazo. Te doy UNA empresa \
que ya pasó un cribado fundamental y cuyo precio está ahora bajo frente a su propio \
historial de 5 años. Tu trabajo: decidir, usando SOLO los datos del mensaje, si la caída \
parece un susto temporal del mercado o un problema estructural del negocio.

Reglas:
- No inventes hechos que no estén en los datos. No tienes acceso a internet.
- Si los titulares y el trimestre no explican la caída, el tipo es "incierto". Es una \
respuesta legítima y preferible a adivinar.
- "estructural" solo si hay evidencia concreta en los datos (ingresos o márgenes cayendo, \
pérdida de un cliente o producto clave, regulación, deuda, fraude...).
- Sé breve y concreto, en español.

Responde ÚNICAMENTE con un objeto JSON válido, sin texto antes ni después, con estas claves:
{"causa": "1-2 frases", "tipo": "temporal" | "estructural" | "incierto", \
"confianza": "alta" | "media" | "baja", "riesgo_clave": "1 frase", \
"que_vigilar": "1 frase"}"""


# --------------------------------------------------------------------------- #
# Titulares
# --------------------------------------------------------------------------- #

def titulares(ticker: str, dias: int = 30, max_n: int = 12) -> list[dict]:
    """Titulares recientes de Finnhub (gratis). Nunca lanza: sin clave o con
    error devuelve []."""
    try:
        from providers import FinnhubProvider
        hasta = date.today()
        desde = hasta - timedelta(days=dias)
        datos = FinnhubProvider()._get(
            "company-news", symbol=ticker, **{"from": desde.isoformat(), "to": hasta.isoformat()}
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Sin titulares para %s: %s", ticker, e)
        return []
    return _limpiar_titulares(datos, max_n)


def _limpiar_titulares(datos, max_n: int) -> list[dict]:
    if not isinstance(datos, list):
        return []
    vistos: set[str] = set()
    out: list[dict] = []
    for d in sorted(datos, key=lambda x: x.get("datetime", 0), reverse=True):
        h = (d.get("headline") or "").strip()
        clave = re.sub(r"\W+", " ", h.lower())[:70]
        if not h or clave in vistos:
            continue
        vistos.add(clave)
        try:
            f = datetime.fromtimestamp(int(d.get("datetime", 0)), tz=timezone.utc).date().isoformat()
        except (ValueError, OSError, OverflowError):
            f = ""
        out.append({"fecha": f, "titular": h[:200], "fuente": (d.get("source") or "")[:30]})
        if len(out) >= max_n:
            break
    return out


# --------------------------------------------------------------------------- #
# Payload y respuesta
# --------------------------------------------------------------------------- #

def _pct(x) -> str:
    return "n/d" if x is None else f"{x * 100:+.0f}%"


def construir_payload(ctx: dict) -> str:
    """Formato compacto con «|», como el resto del proyecto (menos tokens)."""
    L = [
        f"EMPRESA|{ctx['ticker']}|{ctx.get('nombre', '')}|{ctx.get('sector') or 'sin sector'}",
        f"TESIS_LARGO_PLAZO|{ctx.get('veredicto', '')}|{(ctx.get('tesis') or 'sin resumen')[:300]}",
        "PRECIO|actual {precio:.2f}|mediana 5a {mediana:.2f}|bajo su mediana {desc}|"
        "desde mínimo 21 sesiones {reb}|más barata que el {pct:.0f}% de los últimos 5 años".format(
            precio=ctx["precio"], mediana=ctx["mediana"], desc=_pct(-ctx["descuento"]),
            reb=_pct(ctx.get("rebote")), pct=(1 - ctx["percentil"]) * 100),
        f"MOVIMIENTO|1 mes {_pct(ctx.get('ret_1m'))}|3 meses {_pct(ctx.get('ret_3m'))}|"
        f"desde máximo 52 semanas {_pct(ctx.get('desde_maximo_52s'))}",
    ]
    s = ctx.get("salud")
    L.append("ULTIMO_TRIMESTRE|" + (s["resumen"] if s else "sin datos trimestrales") +
             ("|ALERTA: " + "; ".join(s["avisos"]) if s and s.get("avisos") else ""))
    tit = ctx.get("titulares") or []
    if tit:
        L.append("TITULARES_30_DIAS")
        L.extend(f"{t['fecha']}|{t['titular']}" for t in tit)
    else:
        L.append("TITULARES_30_DIAS|ninguno disponible")
    return "\n".join(L)


def interpretar(texto: str) -> dict:
    """Saca el JSON de la respuesta de la IA y lo valida. Si no se puede,
    devuelve una respuesta neutra («incierto», confianza baja) marcada con
    error=True: una IA que falla nunca debe vetar nada."""
    neutra = {"causa": "No se pudo interpretar la respuesta de la IA.", "tipo": "incierto",
              "confianza": "baja", "riesgo_clave": "", "que_vigilar": "", "error": True}
    if not texto:
        return neutra
    i, j = texto.find("{"), texto.rfind("}")
    if i < 0 or j <= i:
        return neutra
    try:
        d = json.loads(texto[i:j + 1])
    except json.JSONDecodeError:
        return neutra
    if not isinstance(d, dict):
        return neutra
    tipo = str(d.get("tipo", "")).strip().lower()
    conf = str(d.get("confianza", "")).strip().lower()
    if tipo not in TIPOS or conf not in CONFIANZAS:
        return neutra
    return {
        "causa": str(d.get("causa", "")).strip()[:400],
        "tipo": tipo,
        "confianza": conf,
        "riesgo_clave": str(d.get("riesgo_clave", "")).strip()[:300],
        "que_vigilar": str(d.get("que_vigilar", "")).strip()[:300],
        "error": False,
    }


def veta(ia: dict | None) -> bool:
    """Único caso en que la IA frena una candidata."""
    return bool(ia) and not ia.get("error") and ia["tipo"] == "estructural" and ia["confianza"] == "alta"


def explicar_caida(ctx: dict) -> dict:
    """Llama a Gemini y devuelve el dict de `interpretar`. Nunca lanza."""
    try:
        from llm_gemini import call_gemini
        resp = call_gemini(SYSTEM, construir_payload(ctx), max_tokens=1500, temperature=0.2)
        return interpretar(resp.get("text", ""))
    except Exception as e:  # noqa: BLE001
        log.warning("IA no disponible para %s: %s", ctx.get("ticker"), e)
        r = interpretar("")
        r["causa"] = "Análisis de IA no disponible en esta ejecución."
        return r
