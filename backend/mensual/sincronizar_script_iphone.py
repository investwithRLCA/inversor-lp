"""Copia el reparto de docs/mensual.js dentro de docs/vigia-compra.js (script del iPhone).

El iPhone tiene que repartir exactamente igual que la pantalla «Este mes» de Vigía, así que
las funciones de reparto se copian tal cual entre las marcas del script. Ejecutar tras
cambiar cualquiera de ellas en docs/mensual.js:

    python -m mensual.sincronizar_script_iphone          (desde backend/)

La prueba del repositorio comprueba que la copia está al día.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
MENSUAL = RAIZ / "docs" / "mensual.js"
SCRIPT = RAIZ / "docs" / "vigia-compra.js"
FUNCIONES = ("llenarAPartesIguales", "repartir", "importeDelMes", "normalizarReparto", "planCompra")
INICIO = "// ───────────── reparto: COPIA EXACTA de docs/mensual.js (una prueba lo comprueba) ─────────────\n"
FIN = "// ───────────── fin de la copia ─────────────\n"


def funcion(fuente: str, nombre: str) -> str:
    """Texto de `function nombre(...) {...}` de mensual.js, sin la sangría de 2 espacios."""
    m = re.search(rf"^(  /\*\*[^\n]*(?:\n   \*[^\n]*)*\n)?  function {nombre}\(", fuente, re.M)
    if not m:
        raise SystemExit(f"No encuentro la función {nombre} en docs/mensual.js")
    ini = m.start()
    fin = fuente.index("\n  }\n", m.end()) + len("\n  }\n")
    bloque = fuente[ini:fin]
    return "\n".join(l[2:] if l.startswith("  ") else l for l in bloque.split("\n"))


def bloque_reparto() -> str:
    fuente = MENSUAL.read_text(encoding="utf-8")
    return "\n".join(funcion(fuente, n) for n in FUNCIONES)


def copia_actual(script: str) -> str:
    i = script.index(INICIO) + len(INICIO)
    j = script.index(FIN, i)
    return script[i:j]


def main() -> int:
    script = SCRIPT.read_text(encoding="utf-8")
    nuevo = bloque_reparto()
    if "/*__REPARTO__*/\n" in script:
        script = script.replace("/*__REPARTO__*/\n", nuevo, 1)
    else:
        i = script.index(INICIO) + len(INICIO)
        j = script.index(FIN, i)
        script = script[:i] + nuevo + script[j:]
    SCRIPT.write_text(script, encoding="utf-8")
    print("Reparto copiado en docs/vigia-compra.js:", ", ".join(FUNCIONES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
