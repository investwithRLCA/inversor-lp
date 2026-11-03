"""
Paquetes de inversión por horizonte — qué buscar según cuánto tiempo puedes
dejar el dinero quieto.

    corto   1–3 años   «Descuento con tesis»   (la regla de precio del backtest)
    medio   3–5 años   «Calidad a precio razonable»
    largo   5+ años    «Compounders»           (negocios que se multiplican solos)

La idea es la de siempre en inversión: cuanto más corto el plazo, más pesa que
el PRECIO sea bajo (el mercado tiene que corregirse en pocos años); cuanto más
largo, más pesa la CALIDAD y el CRECIMIENTO del negocio (el tiempo hace el
trabajo y pagar un precio normal por una empresa excelente sale bien).

LO QUE ESTO ES Y NO ES — importante para no engañarse:

  · Se probó cada paquete en el histórico (2015-2025, S&P 500) con las cuentas
    TAL COMO SE CONOCÍAN en cada fecha (`pit.py`, `prueba_calidad.py`, workflow
    «Prueba histórica de los paquetes»), con una regla de decisión fijada antes
    de ver los resultados: un paquete se mantiene solo si a 12 y a 24 meses
    supera al S&P 500 Y a una selección sin criterio del mismo listado.
        corto  → NO supera al listado a 24 meses (-8,4 pp)           → desactivado
        medio  → supera a ambos (por poco; indistinguible del listado) → ACTIVO, en prueba
        largo  → a 12 meses queda 0,4 pp por debajo del listado        → desactivado
    Aprobar no demuestra nada (el listado son las empresas que HOY siguen en el
    índice y ya ganan solas al S&P 500); suspender sí es una señal clara.
  · Los paquetes desactivados siguen programados: se reactivan añadiéndolos a
    `ACTIVOS` (aquí abajo). No se borran por si el seguimiento en vivo los
    rehabilita.
  · El seguimiento en vivo (`seguimiento.py`) sigue siendo el juez: cada mes
    apunta qué habría elegido cada paquete activo y lo compara con el S&P 500 y
    con el resto del universo.
  · La IA y los chequeos solo pueden QUITAR o AVISAR; no añaden empresas. Aquí
    todo parte de las empresas que ya pasaron el cribado cuantitativo del bot.

Todo son funciones puras (sin red): reciben las métricas ya guardadas en
Supabase y las señales de precio y devuelven puntuaciones. Se prueban sin red.
"""

from __future__ import annotations

from dataclasses import dataclass

PAQUETES = ("corto", "medio", "largo")      # todos los que el módulo sabe calcular
# Los que se ofrecen hoy. «corto» y «largo» quedaron desactivados por la prueba
# histórica (ver arriba); para volver a activarlos basta con añadirlos aquí.
ACTIVOS = ("medio",)

INFO = {
    "corto": {
        "nombre": "Descuento con tesis",
        "horizonte": "1–3 años",
        "etiqueta": "1–3 años",
        "descripcion": (
            "Empresas sólidas que cotizan claramente por debajo de su propio rango de los "
            "últimos 5 años, que no están hundidas y que han dejado de caer. La apuesta es "
            "que el mercado corrija el susto en pocos años. Es la regla de precio que se "
            "probó en el histórico, más los frenos de tesis, último trimestre y titulares."),
    },
    "medio": {
        "nombre": "Calidad a precio razonable",
        "horizonte": "3–5 años",
        "etiqueta": "3–5 años",
        "descripcion": (
            "Buenos negocios (rentables, con caja y poca deuda) que crecen a ritmo "
            "moderado y NO están caros: pagas un precio razonable por la calidad. Se "
            "ordenan sobre todo por lo baratos que están respecto a lo que ganan y generan."),
    },
    "largo": {
        "nombre": "Compounders",
        "horizonte": "5 años o más",
        "etiqueta": "5+ años",
        "descripcion": (
            "Negocios excelentes que reinvierten el dinero a tasas altas: mucha rentabilidad "
            "sobre el capital, crecimiento sostenido, caja de verdad, poca deuda y sin diluir "
            "al accionista. Con tantos años por delante importa más la calidad que el precio "
            "exacto de entrada; si está cara en su rango, entra escalonado."),
    },
}


@dataclass(frozen=True)
class ReglasPaquetes:
    # cuántas empresas por paquete (y máximo por sector)
    n_corto: int = 4
    n_medio: int = 6
    n_largo: int = 8
    n_max_sector: int = 2

    # --- LARGO: compounders --------------------------------------------- #
    largo_roic_min: float = 15.0        # ROIC medio ≥ 15 %
    largo_crec_min: float = 6.0         # ingresos ≥ 6 % anual (media 3 y 5 años)
    largo_fcf_anios_min: int = 4        # caja libre positiva los 4 últimos años
    largo_dilucion_max: float = 5.0     # ≤ +5 % de acciones en 5 años
    largo_deuda_max: float = 2.5        # deuda neta / EBITDA ≤ 2,5x
    # (sin tope de «precio alto en su rango»: un compounder sube casi siempre y
    #  vive en su máximo; el precio se mira frente a lo que gana, no a su gráfico)

    # --- MEDIO: calidad a precio razonable ------------------------------ #
    medio_roic_min: float = 12.0
    medio_crec_min: float = 3.0
    medio_fcf_margen_min: float = 6.0   # caja libre ≥ 6 % de las ventas
    medio_pfcf_max: float = 22.0        # al menos UNA de estas tres, razonable
    medio_per_max: float = 20.0
    medio_evebit_max: float = 14.0
    medio_deuda_max: float = 3.0
    medio_pct_max: float = 0.85         # no disparada en su rango de 5 años

    # --- común ------------------------------------------------------------ #
    mom_min: float = -0.25              # no en caída libre (rend. 12-1 meses ≥ -25 %)
    cobertura_min: float = 0.60         # mínimo de datos para puntuar

    # pesos: (calidad, crecimiento, valoración, momento)
    pesos_largo: tuple = (0.40, 0.35, 0.15, 0.10)
    pesos_medio: tuple = (0.30, 0.15, 0.45, 0.10)

    def n_de(self, paquete: str) -> int:
        return {"corto": self.n_corto, "medio": self.n_medio, "largo": self.n_largo}[paquete]


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #

def _f(x):
    """float o None (NaN/inf/no numérico → None)."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def esc(valor, malo: float, bueno: float):
    """Normaliza a 0–1: `malo` → 0, `bueno` → 1 (vale en ambos sentidos: si
    `bueno` < `malo`, menos es mejor). None si no hay dato."""
    v = _f(valor)
    if v is None or malo == bueno:
        return None
    t = (v - malo) / (bueno - malo)
    return max(0.0, min(1.0, t))


def media_ponderada(partes):
    """[(valor 0–1 | None, peso)] → (media ponderada 0–1 | None, cobertura 0–1).
    Los datos que faltan no cuentan como cero: se re-normaliza con los que hay,
    pero la cobertura baja y, si es poca, el paquete no puntúa."""
    total = sum(p for _, p in partes)
    con = [(v, p) for v, p in partes if v is not None]
    peso = sum(p for _, p in con)
    if not con or total <= 0:
        return None, 0.0
    return sum(v * p for v, p in con) / peso, peso / total


def _media(*xs):
    v = [x for x in map(_f, xs) if x is not None]
    return sum(v) / len(v) if v else None


# --------------------------------------------------------------------------- #
# Métricas clave (de lo que guarda `quant_filter.Metrics`)
# --------------------------------------------------------------------------- #

def metricas_clave(m: dict) -> dict:
    """Extrae del diccionario de métricas del cribado las cifras que usan los
    paquetes, con nombres claros. Todo puede ser None."""
    m = m or {}
    roic = _f(m.get("roic_medio"))
    if roic is None:
        roic = _f(m.get("roic"))
    serie = m.get("fcf_serie") or []
    return {
        "roic": roic,
        "crec": _media(m.get("rev_cagr_3y"), m.get("rev_cagr_5y")),
        "eps": _f(m.get("eps_cagr_5y")),
        "fcf_cagr": _f(m.get("fcf_cagr_5y")),
        "fcf_margen": _f(m.get("fcf_margin")),
        "margen_bruto": _f(m.get("gross_margin")),
        "deuda": _f(m.get("net_debt_ebitda")),
        "cobertura_int": _f(m.get("interest_coverage")),
        "conversion": _f(m.get("fcf_conversion")),
        "dilucion": _f(m.get("share_change_5y")),
        "per": _f(m.get("per")),
        "p_fcf": _f(m.get("p_fcf")),
        "ev_ebit": _f(m.get("ev_ebit")),
        "fcf_anios": int(m.get("fcf_positive_years") or 0),
        "n_fcf": len(serie),
    }


# --------------------------------------------------------------------------- #
# Puntuaciones 0–100 por faceta
# --------------------------------------------------------------------------- #

def puntuar(k: dict, senal: dict, financiera: bool = False, relativa: bool = True) -> dict:
    """Puntúa las cuatro facetas (0–100, None si no hay datos) y devuelve
    también la cobertura de datos de cada una.

      calidad      rentabilidad del capital, caja, margen, deuda, calidad del
                   beneficio y dilución
      crecimiento  ingresos, beneficio por acción y caja libre
      valoracion   barata en términos absolutos (caja, beneficio) y, si
                   `relativa`, también frente a su propio rango de 5 años (en el
                   paquete largo no: un compounder vive en sus máximos)
      momento      rendimiento de los últimos 12 meses sin el último (evita
                   «cuchillos cayendo»)
    """
    calidad, cq = media_ponderada([
        (esc(k["roic"], 8, 30), 0.30),
        (esc(k["fcf_margen"], 3, 25), 0.20),
        (None if financiera else esc(k["margen_bruto"], 20, 70), 0.10),
        (esc(k["deuda"], 4.0, 0.0), 0.15),
        (esc(k["conversion"], 0.5, 1.1), 0.10),
        (esc(k["dilucion"], 10, -5), 0.15),
    ])
    crecimiento, cg = media_ponderada([
        (esc(k["crec"], 0, 20), 0.40),
        (esc(k["eps"], 0, 20), 0.30),
        (esc(k["fcf_cagr"], 0, 20), 0.30),
    ])
    v_abs, _ = media_ponderada([
        (esc(k["p_fcf"], 40, 12), 0.45),
        (esc(k["per"], 40, 14), 0.30),
        (esc(k["ev_ebit"], 30, 10), 0.25),
    ])
    v_rel, _ = media_ponderada([
        (esc(senal.get("percentil"), 0.95, 0.10), 0.5),
        (esc(senal.get("descuento"), -0.15, 0.35), 0.5),
    ])
    if not relativa:
        valoracion = v_abs
    elif v_abs is not None and v_rel is not None:
        valoracion = 0.6 * v_abs + 0.4 * v_rel
    else:
        valoracion = v_abs if v_abs is not None else v_rel
    momento = esc(senal.get("mom"), -0.25, 0.30)

    def p100(x):
        return None if x is None else round(100.0 * x, 1)

    return {"calidad": p100(calidad), "crecimiento": p100(crecimiento),
            "valoracion": p100(valoracion), "momento": p100(momento),
            "cob_calidad": cq, "cob_crecimiento": cg}


# --------------------------------------------------------------------------- #
# Puertas (criterios duros) de cada paquete
# --------------------------------------------------------------------------- #

def _menor(v, tope) -> bool:
    return v is not None and v <= tope


def puerta_largo(k: dict, senal: dict, r: ReglasPaquetes):
    """(pasa, motivo_si_no)."""
    if k["roic"] is None or k["crec"] is None:
        return False, "faltan datos de rentabilidad o crecimiento"
    if k["roic"] < r.largo_roic_min:
        return False, f"ROIC {k['roic']:.0f} % < {r.largo_roic_min:.0f} %"
    if k["crec"] < r.largo_crec_min:
        return False, f"ingresos {k['crec']:.1f} %/año < {r.largo_crec_min:.0f} %"
    if k["fcf_anios"] < r.largo_fcf_anios_min:
        return False, f"caja libre positiva solo {k['fcf_anios']} años"
    if k["dilucion"] is not None and k["dilucion"] > r.largo_dilucion_max:
        return False, f"diluye a los accionistas ({k['dilucion']:+.0f} % acciones)"
    if k["deuda"] is not None and k["deuda"] > r.largo_deuda_max:
        return False, f"deuda neta/EBITDA {k['deuda']:.1f}x"
    mom = _f(senal.get("mom"))
    if mom is not None and mom < r.mom_min:
        return False, f"cae {mom * 100:.0f} % en 12 meses"
    return True, None


def puerta_medio(k: dict, senal: dict, r: ReglasPaquetes):
    if k["roic"] is None or k["fcf_margen"] is None or k["crec"] is None:
        return False, "faltan datos de rentabilidad, caja o crecimiento"
    if k["roic"] < r.medio_roic_min:
        return False, f"ROIC {k['roic']:.0f} % < {r.medio_roic_min:.0f} %"
    if k["crec"] < r.medio_crec_min:
        return False, f"ingresos {k['crec']:.1f} %/año < {r.medio_crec_min:.0f} %"
    if k["fcf_margen"] < r.medio_fcf_margen_min:
        return False, f"caja libre {k['fcf_margen']:.0f} % de ventas"
    if k["deuda"] is not None and k["deuda"] > r.medio_deuda_max:
        return False, f"deuda neta/EBITDA {k['deuda']:.1f}x"
    razonable = (_menor(k["p_fcf"], r.medio_pfcf_max) and k["p_fcf"] > 0) \
        or (_menor(k["per"], r.medio_per_max) and k["per"] > 0) \
        or (_menor(k["ev_ebit"], r.medio_evebit_max) and k["ev_ebit"] > 0)
    if not razonable:
        return False, "no está a un precio razonable (P/FCF, PER y EV/EBIT altos)"
    pct = _f(senal.get("percentil"))
    if pct is not None and pct > r.medio_pct_max:
        return False, "disparada en su rango de 5 años"
    mom = _f(senal.get("mom"))
    if mom is not None and mom < r.mom_min:
        return False, f"cae {mom * 100:.0f} % en 12 meses"
    return True, None


# --------------------------------------------------------------------------- #
# Razones en cristiano
# --------------------------------------------------------------------------- #

def razones(k: dict, senal: dict, paquete: str) -> list[str]:
    out: list[str] = []
    if k["roic"] is not None:
        out.append(f"Cada dólar invertido en el negocio rinde un {k['roic']:.0f} % al año (ROIC medio).")
    if k["crec"] is not None:
        out.append(f"Los ingresos crecen un {k['crec']:.1f} % anual (media de 3 y 5 años).")
    if k["fcf_margen"] is not None:
        out.append(f"Convierte el {k['fcf_margen']:.0f} % de las ventas en caja libre.")
    if k["deuda"] is not None:
        out.append("Sin deuda neta." if k["deuda"] <= 0 else f"Deuda neta de {k['deuda']:.1f} veces su EBITDA.")
    if k["dilucion"] is not None:
        out.append("Recompra acciones propias." if k["dilucion"] < 0
                   else f"Casi no diluye al accionista ({k['dilucion']:+.0f} % de acciones en 5 años).")
    val = []
    if k["p_fcf"] is not None and k["p_fcf"] > 0:
        val.append(f"{k['p_fcf']:.0f} veces su caja libre")
    if k["per"] is not None and k["per"] > 0:
        val.append(f"PER {k['per']:.0f}")
    if k["ev_ebit"] is not None and k["ev_ebit"] > 0:
        val.append(f"EV/EBIT {k['ev_ebit']:.0f}")
    if val:
        out.append("Precio: " + ", ".join(val) + ".")
    pct, desc = _f(senal.get("percentil")), _f(senal.get("descuento"))
    if pct is not None and desc is not None:
        if desc >= 0.05:
            out.append(f"Cotiza un {desc * 100:.0f} % por debajo de su mediana de 5 años.")
        elif pct >= 0.80:
            out.append("Está en la parte alta de su rango de 5 años: considera entrar escalonado.")
    # el orden de relevancia depende del paquete; se recorta a 5
    prioridad = {
        "largo": ("Cada dólar", "Los ingresos", "Convierte", "Sin deuda", "Recompra", "Casi no", "Deuda", "Precio", "Cotiza", "Está en"),
        "medio": ("Precio", "Cotiza", "Cada dólar", "Convierte", "Los ingresos", "Sin deuda", "Deuda", "Recompra", "Casi no", "Está en"),
    }.get(paquete, ())

    def clave(s: str) -> int:
        for i, ini in enumerate(prioridad):
            if s.startswith(ini):
                return i
        return 99
    return sorted(out, key=clave)[:5]


# --------------------------------------------------------------------------- #
# Evaluación de una empresa para Medio / Largo
# --------------------------------------------------------------------------- #

def evaluar(paquete: str, m: dict, senal: dict, financiera: bool,
            r: ReglasPaquetes | None = None) -> dict | None:
    """Devuelve None si la empresa no cumple las puertas del paquete (o faltan
    datos), o un dict con la puntuación total 0–100, el desglose y las razones.
    Solo 'medio' y 'largo': 'corto' se decide por precio en `recomendador`."""
    r = r or ReglasPaquetes()
    if paquete not in ("medio", "largo"):
        raise ValueError(f"evaluar solo vale para 'medio' y 'largo', no {paquete!r}")
    if financiera:
        return None          # los bancos y aseguradoras no se miden con ROIC/deuda
    if not m:
        return None
    k = metricas_clave(m)
    ok, motivo = (puerta_largo if paquete == "largo" else puerta_medio)(k, senal, r)
    if not ok:
        return None
    s = puntuar(k, senal, financiera, relativa=(paquete == "medio"))
    if s["cob_calidad"] < r.cobertura_min or s["cob_crecimiento"] < r.cobertura_min:
        return None
    w = r.pesos_largo if paquete == "largo" else r.pesos_medio
    partes = [(s["calidad"], w[0]), (s["crecimiento"], w[1]), (s["valoracion"], w[2]), (s["momento"], w[3])]
    total, cob = media_ponderada([(None if v is None else v / 100.0, p) for v, p in partes])
    if total is None or cob < r.cobertura_min:
        return None
    pts = 100.0 * total
    return {
        "puntos": round(max(pts, 0.0), 1),
        "calidad": s["calidad"], "crecimiento": s["crecimiento"],
        "valoracion": s["valoracion"], "momento": s["momento"],
        "clave": {n: (None if v is None else round(v, 2)) for n, v in k.items()
                  if n in ("roic", "crec", "fcf_margen", "deuda", "dilucion", "per", "p_fcf", "ev_ebit")},
        "razones": razones(k, senal, paquete),
    }


def criterios(paquete: str, r: ReglasPaquetes | None = None) -> list[str]:
    """Los criterios del paquete en palabras, para mostrarlos en el Vigía."""
    r = r or ReglasPaquetes()
    if paquete == "corto":
        return [
            "Ya ha pasado el cribado cuantitativo del bot (calidad, caja, solvencia).",
            "Cotiza entre un 15 % y un 60 % por debajo de su mediana de 5 años y en el 20 % bajo de su rango.",
            "Ha rebotado al menos un 3 % desde su mínimo del último mes (ha dejado de caer).",
            f"Máximo {r.n_corto} empresas y {r.n_max_sector} por sector.",
            "Frenos: precio sobre el rango de valor de su tesis, último trimestre, y la IA si ve un problema estructural con confianza alta.",
        ]
    if paquete == "medio":
        return [
            f"Rentabilidad sobre el capital (ROIC) ≥ {r.medio_roic_min:.0f} %; ingresos creciendo ≥ {r.medio_crec_min:.0f} % anual.",
            f"Caja libre ≥ {r.medio_fcf_margen_min:.0f} % de las ventas; deuda neta/EBITDA ≤ {r.medio_deuda_max:.0f}x.",
            f"Precio razonable: caja libre ≤ {r.medio_pfcf_max:.0f}x, o PER ≤ {r.medio_per_max:.0f}, o EV/EBIT ≤ {r.medio_evebit_max:.0f}.",
            "No disparada en su rango de 5 años (percentil ≤ 85 %) ni cayendo más de un 25 % en 12 meses.",
            f"Sin bancos ni aseguradoras. Máximo {r.n_medio} empresas y {r.n_max_sector} por sector.",
        ]
    return [
        f"ROIC medio ≥ {r.largo_roic_min:.0f} %; ingresos creciendo ≥ {r.largo_crec_min:.0f} % anual (media 3 y 5 años).",
        f"Caja libre positiva los {r.largo_fcf_anios_min} últimos años; deuda neta/EBITDA ≤ {r.largo_deuda_max}x.",
        f"No diluye: acciones en 5 años ≤ {r.largo_dilucion_max:+.0f} %.",
        "Sin caída libre: no pierde más de un 25 % en 12 meses. El precio se mide frente a lo que gana, no frente a su gráfico.",
        f"Sin bancos ni aseguradoras. Máximo {r.n_largo} empresas y {r.n_max_sector} por sector.",
    ]
