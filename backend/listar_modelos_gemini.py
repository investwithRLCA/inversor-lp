"""
Diagnóstico: ¿qué modelos acepta tu GEMINI_API_KEY ahora mismo?

Google va renombrando y retirando modelos con el tiempo, así que en vez de
adivinar el nombre exacto, este script se lo pregunta directamente a la API.

    python listar_modelos_gemini.py

Si falla con 400/403, el problema está en la clave (mal copiada, caducada,
o no es del tipo correcto). Si funciona pero la lista sale vacía o rara,
copia aquí el resultado completo.
"""

from __future__ import annotations

import os

import requests

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")

if not GEMINI_KEY:
    raise SystemExit(
        "Falta GEMINI_API_KEY en esta ventana. Ejecuta primero ..\\entorno.ps1 "
        "(o define la clave a mano) y vuelve a intentarlo."
    )

r = requests.get(
    "https://generativelanguage.googleapis.com/v1beta/models",
    params={"key": GEMINI_KEY},
    timeout=30,
)

print(f"Código de estado HTTP: {r.status_code}\n")

if r.status_code != 200:
    print("La clave no ha funcionado. Respuesta completa de Google:")
    print(r.text)
    raise SystemExit(1)

data = r.json()
modelos = data.get("models", [])

print(f"Tu clave da acceso a {len(modelos)} modelos. Los que sirven para texto (generateContent):\n")

utiles = []
for m in modelos:
    nombre = m.get("name", "").removeprefix("models/")
    metodos = m.get("supportedGenerationMethods", [])
    if "generateContent" in metodos:
        utiles.append(nombre)
        print(f"  - {nombre}")

if not utiles:
    print("  (ninguno soporta generateContent — algo raro en la cuenta/clave)")
else:
    # Prioriza los alias "-latest": Google los mantiene apuntando siempre al
    # modelo Flash recomendado del momento, así que son los que menos veces
    # dan un 404 por nombre de modelo retirado o renombrado.
    candidato = next(
        (m for m in utiles if "flash" in m and "latest" in m and "lite" not in m),
        next((m for m in utiles if "flash" in m and "lite" not in m), utiles[0]),
    )
    print(f"\nPrueba con este, por ejemplo:")
    print(f'  $env:LLM_MODEL="{candidato}"')
    print(f"  python probar_ia.py MSFT --ia")
