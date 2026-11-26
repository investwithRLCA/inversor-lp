"""¿Qué empresas se pueden comprar en Trading 212?

Trading 212 no publica una lista descargable: la única fuente fiable es su API
(`GET /api/v0/equity/metadata/instruments`, como mucho 1 vez cada 50 s), con una clave que
tenga SOLO el permiso «Metadata». Esa clave únicamente lee el catálogo de valores: no ve tu
cartera ni tu saldo y no puede comprar, vender ni mover dinero. Se guarda como secreto de
GitHub (T212_METADATA_KEY y T212_METADATA_SECRET).

El informe mensual lo usa al final del análisis: una empresa que lo cumple todo pero que
Trading 212 no tiene NO ocupa plaza en la inversión (la ocupa la siguiente mejor) y pasa a la
lista «buenas, pero no están en Trading 212», que se enseña aparte y no se invierte. Las que
sí están llevan su ticker exacto de Trading 212 («t212»), que usan la tarta y la compra
automática.

Si la API no responde se usa la última lista buena guardada (t212_instrumentos.json, como
mucho de hace MAX_DIAS_CACHE días). Sin ninguna lista, no se puede comprobar: el informe lo
avisa y solo se quitan las de t212_no_disponibles.txt (lista a mano).

Solo datos públicos del catálogo de Trading 212: nada de tu cuenta.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

AQUI = Path(__file__).resolve().parent
CACHE = AQUI / "t212_instrumentos.json"
MANUAL = AQUI / "t212_no_disponibles.txt"
URL = "https://live.trading212.com/api/v0/equity/metadata/instruments"
MAX_DIAS_CACHE = 60
MIN_EE_UU = 1500        # una lista con menos acciones de EE. UU. sería un error de la API: no se usa


def normalizar(simbolo: str) -> str:
    """BRK-B, BRK.B y BRK_B → BRKB (solo letras y números, en mayúsculas)."""
    return re.sub(r"[^A-Z0-9]", "", str(simbolo or "").upper())


def ticker_ingenuo(t: str) -> str:
    """AAPL → AAPL_US_EQ, BRK.B → BRK_B_US_EQ (cuando no hay lista con la que comprobarlo)."""
    s = re.sub(r"[.\-/ ]+", "_", str(t or "").strip().upper())
    return s + "_US_EQ"


def manual_no_disponibles() -> set[str]:
    if not MANUAL.exists():
        return set()
    out = set()
    for linea in MANUAL.read_text(encoding="utf-8").splitlines():
        linea = linea.split("#", 1)[0].strip().upper()
        if linea:
            out.add(linea)
    return out


def solo_lectura(i: dict) -> bool:
    """Valor de solo lectura (no se puede abrir posición): Trading 212 lo marca con
    maxOpenQuantity = 0. Comprobado con el catálogo real (nov. 2026): warrants muertos y
    alguna acción en ese estado. Los que tu cuenta no puede operar en absoluto ni aparecen."""
    q = i.get("maxOpenQuantity")
    return isinstance(q, (int, float)) and q <= 0


def solo_lectura_ee_uu(instrumentos: list[dict]) -> list[str]:
    return sorted(str(i.get("ticker")) for i in instrumentos or []
                  if isinstance(i, dict) and str(i.get("ticker") or "").endswith("_US_EQ") and solo_lectura(i))


def indice_de(instrumentos: list[dict]) -> dict[str, str]:
    """{símbolo normalizado: ticker de Trading 212} de las acciones y ETF de EE. UU. (en dólares)
    que se pueden comprar. Si el símbolo del ticker y el «shortName» difieren, vale cualquiera de
    los dos; manda el del ticker. Los de solo lectura NO entran (como si no estuvieran)."""
    por_ticker: dict[str, str] = {}
    por_nombre: dict[str, str] = {}
    for i in instrumentos or []:
        if not isinstance(i, dict):
            continue
        t = str(i.get("ticker") or "")
        if not t.endswith("_US_EQ") or (i.get("currencyCode") or "USD") != "USD":
            continue
        if i.get("type") not in (None, "STOCK", "ETF"):
            continue
        if solo_lectura(i):
            continue
        base = normalizar(t[: -len("_US_EQ")])
        if base:
            por_ticker.setdefault(base, t)
        sn = normalizar(i.get("shortName") or "")
        if sn:
            por_nombre.setdefault(sn, t)
    indice = dict(por_nombre)
    indice.update(por_ticker)
    return indice


def descargar(clave: str, secreto: str, url: str = URL, timeout: int = 60) -> list[dict]:
    auth = base64.b64encode(f"{clave}:{secreto}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": "Basic " + auth, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        datos = json.load(r)
    if not isinstance(datos, list):
        raise RuntimeError("la API de Trading 212 no devolvió una lista")
    return datos


class Disponibilidad:
    """Qué se puede comprar en Trading 212. `ticker(t)` devuelve el ticker de Trading 212 si está,
    o None si NO está (y entonces no se invierte)."""

    def __init__(self, indice: dict[str, str] | None, manual: set[str] | None = None,
                 fuente: str = "", actualizado: str | None = None, aviso: str | None = None,
                 solo_lectura: list[str] | None = None):
        self.indice = indice
        self.solo_lectura = list(solo_lectura or [])
        self.manual = {normalizar(x) for x in (manual or set())}
        self.fuente = fuente
        self.actualizado = actualizado
        self.aviso = aviso

    @property
    def comprobado(self) -> bool:
        return self.indice is not None

    def ticker(self, t: str) -> str | None:
        n = normalizar(t)
        if not n or n in self.manual:
            return None
        if self.indice is None:
            return ticker_ingenuo(t)            # sin lista: no se puede comprobar (el informe lo avisa)
        return self.indice.get(n)

    def resumen(self) -> dict:
        return {"comprobado": self.comprobado, "fuente": self.fuente, "actualizado": self.actualizado,
                "n_ee_uu": len(set(self.indice.values())) if self.indice is not None else None,
                "n_solo_lectura": len(self.solo_lectura),
                "manual": sorted(self.manual), "aviso": self.aviso}


def leer_cache(hoy: date | None = None) -> tuple[dict[str, str] | None, str | None, list[str]]:
    """La última lista buena guardada, si no es demasiado vieja."""
    hoy = hoy or date.today()
    try:
        d = json.loads(CACHE.read_text(encoding="utf-8"))
        f = date.fromisoformat(d["actualizado"])
        idx = d["indice"]
        if isinstance(idx, dict) and len(set(idx.values())) >= MIN_EE_UU and (hoy - f).days <= MAX_DIAS_CACHE:
            return idx, d["actualizado"], list(d.get("solo_lectura") or [])
    except Exception:  # noqa: BLE001
        pass
    return None, None, []


def guardar_cache(indice: dict[str, str], hoy: date | None = None, solo_lectura: list[str] | None = None) -> None:
    hoy = hoy or date.today()
    CACHE.write_text(json.dumps({"actualizado": hoy.isoformat(), "fuente": "API de Trading 212 (metadata/instruments)",
                                 "solo_lectura": sorted(solo_lectura or []),
                                 "indice": dict(sorted(indice.items()))}, ensure_ascii=False, indent=0),
                     encoding="utf-8")


def cargar(hoy: date | None = None, *, online: bool = True, f_descargar=None) -> Disponibilidad:
    """Lista de Trading 212: de la API si hay clave; si falla, la guardada; si no, sin comprobar."""
    hoy = hoy or date.today()
    manual = manual_no_disponibles()
    clave, secreto = os.getenv("T212_METADATA_KEY"), os.getenv("T212_METADATA_SECRET")
    aviso = None
    if online and clave and secreto:
        try:
            inst = (f_descargar or descargar)(clave, secreto)
            idx = indice_de(inst)
            n = len(set(idx.values()))
            if n < MIN_EE_UU:
                raise RuntimeError(f"solo {n} acciones de EE. UU. en la respuesta")
            sl = solo_lectura_ee_uu(inst)
            guardar_cache(idx, hoy, sl)
            return Disponibilidad(idx, manual, "Trading 212 (consultado hoy)", hoy.isoformat(), solo_lectura=sl)
        except Exception as e:  # noqa: BLE001
            log.warning("Trading 212 no responde: %s", e)
            aviso = f"No se pudo consultar Trading 212 ({e})"
    elif online:
        aviso = "Falta la clave de solo lectura de Trading 212 (secretos T212_METADATA_KEY y T212_METADATA_SECRET)"
    idx, f, sl = leer_cache(hoy)
    if idx is not None:
        return Disponibilidad(idx, manual, f"lista de Trading 212 guardada el {f}", f,
                              (aviso + ": se usa la última lista guardada.") if aviso else None, solo_lectura=sl)
    return Disponibilidad(None, manual, "sin comprobar", None,
                          (aviso or "Sin lista de Trading 212") +
                          ": no se ha podido comprobar qué empresas están en Trading 212; "
                          "solo se quitan las de la lista a mano.")


def diagnostico(instrumentos: list[dict]) -> None:
    """Cómo viene el catálogo (datos públicos): campos, tipos y el campo maxOpenQuantity."""
    from collections import Counter
    ee = [i for i in instrumentos if isinstance(i, dict) and str(i.get("ticker") or "").endswith("_US_EQ")]
    print(f"Catálogo: {len(instrumentos)} valores; {len(ee)} de EE. UU.")
    print("Campos:", sorted({k for i in instrumentos if isinstance(i, dict) for k in i}))
    print("Tipos EE. UU.:", dict(Counter(i.get("type") for i in ee)))
    moq = [i.get("maxOpenQuantity") for i in ee]
    num = sorted(float(x) for x in moq if isinstance(x, (int, float)))
    print(f"maxOpenQuantity: {sum(1 for x in moq if x is None)} vacíos, {sum(1 for x in num if x <= 0)} a cero o menos, "
          f"{sum(1 for x in num if x > 0)} positivos" + (f" (mín {num[0]}, mediana {num[len(num) // 2]}, máx {num[-1]})" if num else ""))
    print("Valores más repetidos:", Counter(moq).most_common(8))
    cero = [i for i in ee if isinstance(i.get("maxOpenQuantity"), (int, float)) and i["maxOpenQuantity"] <= 0]
    print("Ejemplos a cero:", [(i.get("ticker"), i.get("shortName"), i.get("type"), i.get("name")) for i in cero[:25]])
    for k in ("extendedHours", "currencyCode"):
        print(k, dict(Counter(i.get(k) for i in ee)))
    otros = {k for i in ee for k in i} - {"ticker", "type", "workingScheduleId", "isin", "currencyCode", "name", "shortName",
                                          "maxOpenQuantity", "addedOn", "extendedHours"}
    for k in sorted(otros):
        print(f"campo {k}:", Counter(str(i.get(k))[:30] for i in ee).most_common(6))


def main() -> int:
    """Descarga la lista y dice cuántas hay (para probar la clave desde GitHub Actions)."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    import sys
    if "--diagnostico" in sys.argv:
        clave, secreto = os.getenv("T212_METADATA_KEY"), os.getenv("T212_METADATA_SECRET")
        diagnostico(descargar(clave, secreto))
        import time
        time.sleep(55)                    # la API admite 1 consulta cada 50 s
    d = cargar()
    r = d.resumen()
    print(f"Trading 212: {'comprobado' if r['comprobado'] else 'SIN COMPROBAR'} · {r['fuente']} · "
          f"{r['n_ee_uu'] or 0} acciones/ETF de EE. UU. que se pueden comprar · {r['n_solo_lectura']} de solo lectura (excluidas)"
          + (f" · {r['aviso']}" if r["aviso"] else ""))
    return 0 if d.comprobado else 1


if __name__ == "__main__":
    raise SystemExit(main())
