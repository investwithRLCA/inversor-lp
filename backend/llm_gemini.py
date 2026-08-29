"""
Cliente para la API de Gemini (Google AI Studio) — capa gratuita.

Sustituye a la llamada que antes iba a Anthropic. Devuelve la misma forma de
salida ({"text", "input_tokens", "output_tokens", "cached_tokens", "model"})
para que `pipeline.py` y `macro_layer.py` no tengan que cambiar su lógica,
solo el proveedor.

Clave gratuita, sin tarjeta: https://aistudio.google.com/apikey

Cadena de modelos: primero se prueba GEMINI_MODEL (por defecto
"gemini-flash-latest"); si Google sigue devolviendo errores de servidor con
ese modelo, se pasa automáticamente a GEMINI_MODEL_FALLBACK
("gemini-flash-lite-latest" por defecto), que suele tener menos carga en el
nivel gratuito. Solo si los dos fallan se lanza un error.

Varias claves (opcional): el límite del nivel gratuito es POR PROYECTO de
Google Cloud, no por clave — dos claves del mismo proyecto comparten cupo y
no sirven de nada. Si defines GEMINI_API_KEYS con varias claves separadas
por comas, CADA UNA de un proyecto distinto, las llamadas se reparten entre
ellas por turnos (round-robin), multiplicando el cupo efectivo en vez de
solo usarlas como respaldo. Si solo hay una clave (GEMINI_API_KEY, como
hasta ahora), el comportamiento es idéntico al de siempre.

Los límites del nivel gratuito son modestos y Google los cambia de vez en
cuando. Límites actuales: https://ai.google.dev/gemini-api/docs/rate-limits
"""

from __future__ import annotations

import itertools
import os
import time

import requests

GEMINI_MODEL = os.getenv("LLM_MODEL", "gemini-flash-latest")
GEMINI_MODEL_FALLBACK = os.getenv("LLM_MODEL_FALLBACK", "gemini-flash-lite-latest")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# GEMINI_API_KEYS="clave1,clave2,clave3" tiene prioridad si está definida;
# si no, se cae a la variable de siempre con una sola clave.
GEMINI_KEYS: list[str] = [
    k.strip()
    for k in os.getenv("GEMINI_API_KEYS", os.getenv("GEMINI_API_KEY", "")).split(",")
    if k.strip()
]
_rotacion_claves = itertools.cycle(GEMINI_KEYS) if GEMINI_KEYS else None

# Errores de servidor: transitorios, casi siempre desaparecen si esperas y reintentas.
_REINTENTABLES = {500, 502, 503, 504}


def _siguiente_clave() -> str:
    if _rotacion_claves is None:
        raise RuntimeError(
            "Falta GEMINI_API_KEY (o GEMINI_API_KEYS). Consíguela gratis, sin tarjeta, en "
            "https://aistudio.google.com/apikey"
        )
    return next(_rotacion_claves)


def _quitar_valla_markdown(texto: str) -> str:
    """A veces el modelo envuelve TODA la respuesta en una valla de código
    ```markdown ... ``` (aunque el system prompt le pide responder solo en
    Markdown, no meterlo dentro de un bloque de código). Si no se quita, un
    renderizador de Markdown lo trata como un único bloque preformateado
    gigante — sin ajuste de línea, se sale de cualquier contenedor por el
    lado derecho. Si el texto no lleva valla, se devuelve tal cual."""
    t = texto.strip()
    if not t.startswith("```"):
        return t
    fin_primera_linea = t.find("\n")
    if fin_primera_linea == -1:
        return t
    resto = t[fin_primera_linea + 1 :].rstrip()
    if resto.endswith("```"):
        return resto[:-3].strip()
    return t


def _intentar_modelo(
    model: str,
    system_prompt: str,
    payload: str,
    max_tokens: int,
    temperature: float,
    intentos: int,
) -> dict:
    """Prueba UN modelo concreto, hasta `intentos` veces. Cada intento usa la
    SIGUIENTE clave de la rotación: si hay varias, un 429 (cupo agotado en
    esa clave/proyecto concreto) se soluciona probando con otra clave de
    inmediato, sin esperar — es un proyecto distinto, con su propio cupo.
    Un error de servidor (503...) si espera, porque cambiar de clave no
    arregla que Google esté saturado en general.

    Devuelve el resultado o lanza RuntimeError describiendo el fallo (para
    que `call_gemini` decida si pasa al siguiente modelo de la cadena)."""
    body = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": payload}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    url = GEMINI_URL.format(model=model)

    for intento in range(1, intentos + 1):
        clave = _siguiente_clave()
        r = requests.post(url, params={"key": clave}, json=body, timeout=120)

        if r.status_code in _REINTENTABLES:
            if intento < intentos:
                espera = 5 * (2 ** (intento - 1))       # 5, 10, 20... segundos
                print(f"  [{model}] no disponible (HTTP {r.status_code}). "
                      f"Reintentando en {espera}s... ({intento}/{intentos})")
                time.sleep(espera)
                continue
            raise RuntimeError(f"{model}: error de servidor (HTTP {r.status_code}) "
                               f"tras {intentos} intento(s)")

        if r.status_code == 429:
            if intento < intentos and len(GEMINI_KEYS) > 1:
                print(f"  [{model}] esa clave no tiene cupo (429). Probando con otra clave...")
                continue
            raise RuntimeError(f"{model}: límite de peticiones gratuitas superado (HTTP 429)")
        if r.status_code in (400, 403):
            raise RuntimeError(
                f"{model}: petición rechazada (HTTP {r.status_code}). Lo más probable es "
                "que la clave usada esté mal copiada o caducada."
            )
        if r.status_code != 200:
            # Cualquier otro código (404 = modelo no disponible en tu proyecto/nivel,
            # u otro que Google añada en el futuro): mensaje legible, nunca un
            # traceback en crudo. Así `call_gemini` puede pasar al siguiente modelo
            # de la cadena en vez de que el programa entero se pare aquí.
            raise RuntimeError(
                f"{model}: HTTP {r.status_code} inesperado. "
                f"Respuesta de Google: {r.text[:200]}"
            )
        data = r.json()

        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError):
            finish = data.get("candidates", [{}])[0].get("finishReason", "desconocido")
            raise RuntimeError(f"{model}: no ha devuelto texto (finishReason={finish})")

        if not text.strip():
            raise RuntimeError(f"{model}: ha devuelto una respuesta vacía")

        text = _quitar_valla_markdown(text)

        usage = data.get("usageMetadata", {})
        return {
            "text": text.strip(),
            "input_tokens": usage.get("promptTokenCount", 0),
            "output_tokens": usage.get("candidatesTokenCount", 0),
            # Gemini solo rellena esto si usas caché explícita de contexto
            # (una función aparte, de pago). En el nivel gratuito siempre es 0.
            "cached_tokens": usage.get("cachedContentTokenCount", 0),
            "model": model,
        }

    # Inalcanzable: el bucle siempre devuelve o lanza antes de agotar los intentos.
    raise RuntimeError(f"{model}: fallo inesperado tras los reintentos.")


def call_gemini(
    system_prompt: str,
    payload: str,
    max_tokens: int = 2000,
    temperature: float = 0.2,
    intentos_por_modelo: int | None = None,
) -> dict:
    """Llama a Gemini con `system_prompt` como instrucción de sistema y
    `payload` como mensaje de usuario. Prueba primero GEMINI_MODEL, rotando
    entre las claves configuradas; si sigue fallando, pasa a
    GEMINI_MODEL_FALLBACK. Lanza RuntimeError con un mensaje legible solo si
    fallan los dos.

    `intentos_por_modelo`, si no se especifica, se ajusta solo al número de
    claves disponibles (al menos 2), para que cada clave tenga su turno
    antes de rendirse con ese modelo."""
    if _rotacion_claves is None:
        raise RuntimeError(
            "Falta GEMINI_API_KEY (o GEMINI_API_KEYS). Consíguela gratis, sin tarjeta, en "
            "https://aistudio.google.com/apikey"
        )
    if intentos_por_modelo is None:
        intentos_por_modelo = max(2, len(GEMINI_KEYS))

    modelos = [GEMINI_MODEL]
    if GEMINI_MODEL_FALLBACK and GEMINI_MODEL_FALLBACK != GEMINI_MODEL:
        modelos.append(GEMINI_MODEL_FALLBACK)

    errores: list[str] = []
    for i, modelo in enumerate(modelos):
        if i > 0:
            print(f"  Cambiando al modelo de reserva: {modelo}")
        try:
            return _intentar_modelo(
                modelo, system_prompt, payload, max_tokens, temperature, intentos_por_modelo
            )
        except RuntimeError as e:
            errores.append(str(e))
            continue

    raise RuntimeError(
        "Gemini ha fallado con todos los modelos probados: " + " · ".join(errores)
    )


if __name__ == "__main__":
    # Prueba mínima, sin depender de ningún otro fichero del proyecto.
    print(f"Claves configuradas: {len(GEMINI_KEYS)}")
    resp = call_gemini(
        "Responde siempre en una sola frase, muy breve, en español.",
        "¿Qué es el ROIC y por qué importa a un inversor a largo plazo?",
    )
    print(resp["text"])
    print(f"\n[modelo: {resp['model']} · tokens: {resp['input_tokens']} entrada / "
          f"{resp['output_tokens']} salida]")
