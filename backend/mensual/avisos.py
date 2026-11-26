"""Avisos al móvil con ntfy (https://ntfy.sh): app gratis, sin cuenta.

En la app ntfy te suscribes a un «tema» con un nombre difícil de adivinar (funciona como una
contraseña) y GitHub te manda ahí los avisos:

  · cuando sale la lista de la compra del mes (el primer día de bolsa, tras el informe);
  · del día 3 al 10, si todavía no hay informe del mes;
  · si el informe mensual falla;
  · cuando una empresa vigilada ENTRA en zona de compra (vigilancia de precio).

Cada aviso sale UNA vez aunque se intente varias veces (GitHub repite los workflows): antes de
mandarlo se mira en ntfy si ya se mandó ese mismo aviso en las últimas 12 horas (lo que ntfy
guarda).

El nombre del tema es el secreto de GitHub NTFY_TOPIC: no está en el código porque este
repositorio es público. Sin ese secreto no se manda nada (y no falla nada).

Los avisos solo dicen cosas públicas («Ya está la lista de diciembre»): nada de tu cartera ni de
tu importe.

Uso (desde backend/):
  python3 -m mensual.avisos lista --mes 2026-12 [--prueba]   # espera a que la web la publique y avisa
  python3 -m mensual.avisos sin-lista --mes 2026-12
  python3 -m mensual.avisos fallo-informe
  python3 -m mensual.avisos prueba                           # para comprobar que te llegan
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SERVIDOR = "https://ntfy.sh"
VIGIA = "https://investwithrlca.github.io/inversor-lp/"
LISTA = VIGIA + "compra/lista.json"
LISTA_PRUEBA = VIGIA + "compra/lista-prueba.json"
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
         "octubre", "noviembre", "diciembre"]


def nombre_mes(mes: str) -> str:
    try:
        a, m = (int(x) for x in str(mes).split("-"))
        return f"{MESES[m - 1]} {a}"
    except Exception:  # noqa: BLE001
        return str(mes)


def mes_actual(ahora: datetime | None = None) -> str:
    return f"{(ahora or datetime.now(timezone.utc)):%Y-%m}"


def ya_enviado(titulo: str, tema: str, *, horas: int = 12, abrir=urllib.request.urlopen) -> bool:
    """¿Se mandó ya este mismo aviso (mismo título) en las últimas horas? Lo mira en ntfy, que guarda
    los mensajes 12 horas. Si ntfy no contesta, se da por no enviado (mejor un aviso de más que ninguno)."""
    req = urllib.request.Request(f"{SERVIDOR}/{urllib.parse.quote(tema)}/json?poll=1&since={int(horas)}h")
    try:
        with abrir(req, timeout=20) as r:
            for linea in r.read().decode("utf-8", "replace").splitlines():
                try:
                    m = json.loads(linea)
                except ValueError:
                    continue
                if m.get("event", "message") == "message" and m.get("title") == titulo:
                    return True
    except Exception as e:  # noqa: BLE001
        print(f"No se pudo mirar en ntfy si ya se avisó ({type(e).__name__}).")
    return False


def enviar(titulo: str, mensaje: str, *, click: str | None = VIGIA + "#mensual", tags: list[str] | None = None,
           prioridad: int = 3, tema: str | None = None, una_vez: bool = False, abrir=urllib.request.urlopen,
           pausa=time.sleep) -> bool:
    """Manda el aviso (JSON, así los acentos llegan bien). True si ntfy lo aceptó. Nunca imprime el tema.
    Con una_vez=True no lo repite si ya salió en las últimas 12 horas."""
    tema = (tema if tema is not None else os.getenv("NTFY_TOPIC", "")).strip()
    if not tema:
        print("Sin el secreto NTFY_TOPIC: no se manda aviso al móvil.")
        return False
    if una_vez and ya_enviado(titulo, tema, abrir=abrir):
        print(f"Este aviso ya salió hace poco: no se repite ({titulo}).")
        return True
    cuerpo = {"topic": tema, "title": titulo, "message": mensaje, "tags": tags or [], "priority": prioridad}
    if click:
        cuerpo["click"] = click
    datos = json.dumps(cuerpo, ensure_ascii=False).encode("utf-8")
    for intento in range(3):
        req = urllib.request.Request(SERVIDOR, data=datos, method="POST",
                                     headers={"Content-Type": "application/json; charset=utf-8"})
        try:
            with abrir(req, timeout=20) as r:
                if 200 <= getattr(r, "status", 200) < 300:
                    print(f"Aviso enviado al móvil: {titulo}")
                    return True
                print(f"ntfy respondió {getattr(r, 'status', '?')}")
        except Exception as e:  # noqa: BLE001
            print(f"ntfy no responde ({type(e).__name__})")
        if intento < 2:
            pausa(5 * (intento + 1))
    print("No se pudo mandar el aviso al móvil (no es grave: la lista ya está publicada).")
    return False


def leer_json(url: str, abrir=urllib.request.urlopen) -> dict | None:
    req = urllib.request.Request(f"{url}?t={int(time.time())}", headers={"Cache-Control": "no-cache"})
    with abrir(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def esperar_publicada(url: str, mes: str, *, max_s: int = 600, cada: int = 15, leer=leer_json, pausa=time.sleep,
                      reloj=time.monotonic) -> bool:
    """Espera a que la web (GitHub Pages) ya sirva la lista de ese mes, para que al tocar el aviso esté."""
    fin = reloj() + max_s
    while True:
        try:
            j = leer(url)
            if isinstance(j, dict) and j.get("mes") == mes:
                return True
        except Exception:  # noqa: BLE001
            pass
        if reloj() >= fin:
            return False
        pausa(cada)


def aviso_lista(mes: str, prueba: bool = False, *, esperar=esperar_publicada, mandar=enviar) -> bool:
    lista = LISTA_PRUEBA if prueba else LISTA
    ya = esperar(lista, mes)
    if not ya:
        print("La web aún no muestra la lista nueva; se avisa igualmente.")
    if prueba:
        return mandar("Vigía · lista de PRUEBA",
                      f"Lista de prueba de {nombre_mes(mes)} publicada (solo para la cuenta Practice). "
                      "Si te llega este aviso, los avisos funcionan.", tags=["test_tube"])
    return mandar(f"Vigía · compra de {nombre_mes(mes)}",
                  f"Ya están el informe y la lista de la compra de {nombre_mes(mes)}. "
                  "Abre Vigía → Este mes → «Comprar en Trading 212» → «Comprar ahora».",
                  tags=["chart_with_upwards_trend"], prioridad=4, una_vez=True)


def aviso_sin_lista(mes: str, *, mandar=enviar) -> bool:
    return mandar(f"Vigía · aún no hay lista de {nombre_mes(mes)}",
                  "El informe de este mes todavía no ha salido, así que no hay lista de la compra. "
                  "Se sigue intentando cada día; si no llega, mira GitHub → Actions → «Recomendador mensual».",
                  tags=["warning"], prioridad=4, click="https://github.com/investwithRLCA/inversor-lp/actions", una_vez=True)


def aviso_fallo_informe(mes: str | None = None, *, mandar=enviar) -> bool:
    mes = mes or mes_actual()
    return mandar(f"Vigía · el informe de {nombre_mes(mes)} ha fallado",
                  "No se ha podido generar el informe mensual. Mira GitHub → Actions → «Recomendador mensual» "
                  "y vuelve a lanzarlo («Run workflow»). Se reintenta solo un par de veces más el día 1 y el 2.",
                  tags=["warning"], prioridad=4, click="https://github.com/investwithRLCA/inversor-lp/actions", una_vez=True)


def aviso_vigilancia(tickers: list[str], url: str | None = None, *, mandar=enviar) -> bool:
    """Una o más vigiladas acaban de entrar en zona de compra (precio ≤ su precio de entrada)."""
    tickers = [t for t in tickers if t]
    if not tickers:
        return False
    if tickers == ["PRUEBA"]:
        return mandar("Vigía · vigilancia (prueba)", "Si te llega este aviso, la vigilancia de precio también te avisa en el móvil.",
                      tags=["test_tube"], click=url or None)
    lista = ", ".join(tickers[:6]) + (f" y {len(tickers) - 6} más" if len(tickers) > 6 else "")
    return mandar(f"Vigía · en zona de compra: {lista}",
                  "Han bajado a su precio de entrada. Míralo en el aviso de vigilancia (toca para abrirlo).",
                  tags=["mag"], prioridad=4, click=url or None, una_vez=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Avisos de Vigía al móvil (ntfy)")
    sub = ap.add_subparsers(dest="que", required=True)
    pl = sub.add_parser("lista"); pl.add_argument("--mes", required=True); pl.add_argument("--prueba", action="store_true")
    s = sub.add_parser("sin-lista"); s.add_argument("--mes", required=True)
    f = sub.add_parser("fallo-informe"); f.add_argument("--mes")
    v = sub.add_parser("vigilancia"); v.add_argument("--tickers", required=True); v.add_argument("--url")
    sub.add_parser("prueba")
    a = ap.parse_args(argv)
    if a.que == "lista":
        aviso_lista(a.mes, a.prueba)
    elif a.que == "sin-lista":
        aviso_sin_lista(a.mes)
    elif a.que == "fallo-informe":
        aviso_fallo_informe(a.mes)
    elif a.que == "vigilancia":
        aviso_vigilancia([t.strip() for t in a.tickers.split(",")], a.url)
    else:
        ok = enviar("Vigía · prueba de avisos", "Si ves esto, los avisos de Vigía te llegan bien.", tags=["white_check_mark"])
        return 0 if ok else 1
    return 0   # un aviso que no sale nunca rompe la publicación de la lista


if __name__ == "__main__":
    sys.exit(main())
