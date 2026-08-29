"""
Cliente para la API de Gemini (Google AI Studio) — capa gratuita.

Sustituye a la llamada que antes iba a Anthropic. Devuelve la misma forma de
salida ({"text", "input_tokens", "output_tokens", "cached_tokens", "model"})
para que `pipeline.py` y `macro_layer.py` no tengan que cambiar su lógica,
solo el proveedor.

Clave gratuita, sin tarjeta: https://aistudio.google.com/apikey

Cadena de modelos: primero se prueba GEMINI_MODEL (por defecto
"gemini-flash-latest") un par de veces; si Google sigue devolviendo errores
de servidor con ese modelo, se pasa automáticamente a GEMINI_MODEL_FALLBACK
("gemini-2.5-flash-lite" por defecto), que suele tener menos carga en el
nivel gratuito. Solo si los dos fallan se lanza un error.

Los límites del nivel gratuito son modestos y Google los cambia de vez en
cuando. Límites actuales: https://ai.google.dev/gemini-api/docs/rate-limits
"""

from __future__ import annotations

import os
import time

import requests

GEMINI_MODEL = os.getenv("LLM_MODEL", "gemini-flash-latest")
GEMINI_MODEL_FALLBACK = os.getenv("LLM_MODEL_FALLBACK", "gemini-flash-lite-latest")
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Errores de servidor: transitorios, casi siempre desaparecen si esperas y reintentas.
_REINTENTABLES = {500, 502, 503, 504}


def _intentar_modelo(
    model: str,
    system_prompt: str,
    payload: str,
    max_tokens: int,
    temperature: float,
    intentos: int,
) -> dict:
    """Prueba UN modelo concreto, hasta `intentos` veces si Google devuelve
    un error de servidor transitorio. Devuelve el resultado o lanza
    RuntimeError describiendo el fallo (para que `call_gemini` decida si
    pasa al siguiente modelo de la cadena)."""
    body = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": payload}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    url = GEMINI_URL.format(model=model)

    for intento in range(1, intentos + 1):
        r = requests.post(url, params={"key": GEMINI_KEY}, json=body, timeout=120)

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
            raise RuntimeError(f"{model}: límite de peticiones gratuitas superado (HTTP 429)")
        if r.status_code in (400, 403):
            raise RuntimeError(
                f"{model}: petición rechazada (HTTP {r.status_code}). Lo más probable es "
                "que GEMINI_API_KEY esté mal copiada o caducada."
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
    intentos_por_modelo: int = 2,
) -> dict:
    """Llama a Gemini con `system_prompt` como instrucción de sistema y
    `payload` como mensaje de usuario. Prueba primero GEMINI_MODEL; si falla
    `intentos_por_modelo` veces seguidas por errores de servidor, pasa a
    GEMINI_MODEL_FALLBACK. Lanza RuntimeError con un mensaje legible solo si
    fallan los dos."""
    if not GEMINI_KEY:
        raise RuntimeError(
            "Falta GEMINI_API_KEY. Consíguela gratis, sin tarjeta, en "
            "https://aistudio.google.com/apikey"
        )

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
    resp = call_gemini(
        "Responde siempre en una sola frase, muy breve, en español.",
        "¿Qué es el ROIC y por qué importa a un inversor a largo plazo?",
    )
    print(resp["text"])
    print(f"\n[modelo: {resp['model']} · tokens: {resp['input_tokens']} entrada / "
          f"{resp['output_tokens']} salida]")
