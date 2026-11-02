/*
 * Lógica de la pestaña «Mensual» del Vigía. Funciones PURAS (sin DOM, sin red),
 * para poder probarlas con Node igual que en el navegador.
 *
 * PRIVACIDAD: tus operaciones viven solo en localStorage de tu dispositivo. Este
 * fichero no envía nada a ningún sitio; recibe el informe público del mes
 * (empresas, precios, señales) y tus operaciones, y devuelve cálculos.
 *
 * El reparto (`repartir`) es un port exacto de backend/mensual/estrategia.py::repartir;
 * hay una prueba que compara ambos sobre casos aleatorios.
 */
(function (raiz) {
  "use strict";

  const DIA = 86400000;
  const TOPE_POSICION = 0.25;
  const MESES_REVISION = 24;
  const TESIS_ANTIGUA_DIAS = 200;

  // ───────────────────────── fechas ─────────────────────────

  function ms(fechaISO) {
    const [a, m, d] = String(fechaISO).slice(0, 10).split("-").map(Number);
    return Date.UTC(a, m - 1, d);
  }
  function iso(msUTC) { return new Date(msUTC).toISOString().slice(0, 10); }
  function hoyISO() {                       // fecha LOCAL (no UTC): la que ves en tu calendario
    const d = new Date(), z = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${z(d.getMonth() + 1)}-${z(d.getDate())}`;
  }
  function sumarDias(fechaISO, n) { return iso(ms(fechaISO) + n * DIA); }
  function mesesEntre(f1, f2) { return (ms(f2) - ms(f1)) / DIA / 30.4375; }

  // ───────────────────────── operaciones ─────────────────────────

  /** Normaliza una operación; devuelve {op} o {error}. */
  function normalizarOperacion(o) {
    const tipo = String(o.tipo || "").toLowerCase();
    const ticker = String(o.ticker || "").trim().toUpperCase();
    const acciones = Number(o.acciones), precio = Number(o.precio);
    const comision = o.comision === "" || o.comision == null ? 0 : Number(o.comision);
    const fecha = String(o.fecha || "").slice(0, 10);
    if (tipo !== "compra" && tipo !== "venta") return { error: "El tipo debe ser compra o venta." };
    if (!ticker) return { error: "Falta el ticker." };
    if (!(acciones > 0)) return { error: "El número de acciones debe ser mayor que 0." };
    if (!(precio > 0)) return { error: "El precio debe ser mayor que 0." };
    if (!(comision >= 0)) return { error: "La comisión no puede ser negativa." };
    if (!/^\d{4}-\d{2}-\d{2}$/.test(fecha) || isNaN(ms(fecha))) return { error: "La fecha no es válida." };
    return { op: { id: o.id || (Date.now().toString(36) + Math.random().toString(36).slice(2, 7)),
                   fecha, ticker, tipo, acciones, precio, comision } };
  }

  /** Posiciones abiertas y ventas realizadas, por el método FIFO (el que exige
   *  Hacienda para valores fungibles). Devuelve también los errores (p. ej. una
   *  venta de más acciones de las que había). */
  function calcular(ops) {
    const rango = (o) => (o.tipo === "compra" ? 0 : 1);          // el mismo día, primero las compras
    const orden = [...ops].sort((a, b) => a.fecha.localeCompare(b.fecha) || rango(a) - rango(b));
    const lotes = {};        // ticker -> [{fecha, acciones, coste}]
    const realizadas = [];
    const errores = [];
    for (const o of orden) {
      const L = (lotes[o.ticker] = lotes[o.ticker] || []);
      if (o.tipo === "compra") {
        L.push({ fecha: o.fecha, acciones: o.acciones, coste: o.acciones * o.precio + o.comision });
        continue;
      }
      let falta = o.acciones;
      const disponibles = L.reduce((s, l) => s + l.acciones, 0);
      if (o.acciones > disponibles + 1e-9) {
        errores.push(`Venta de ${o.acciones} ${o.ticker} el ${o.fecha}, pero solo había ${disponibles.toFixed(4)}.`);
        falta = disponibles;
      }
      const vendidas = falta;
      let coste = 0;
      while (falta > 1e-12 && L.length) {
        const l = L[0];
        const toma = Math.min(falta, l.acciones);
        coste += l.coste * (toma / l.acciones);
        l.coste -= l.coste * (toma / l.acciones);
        l.acciones -= toma;
        falta -= toma;
        if (l.acciones <= 1e-12) L.shift();
      }
      if (vendidas > 0) {
        const ingreso = vendidas * o.precio - o.comision * (vendidas / o.acciones);
        realizadas.push({ id: o.id, fecha: o.fecha, ticker: o.ticker, acciones: vendidas,
                          ingreso, coste, resultado: ingreso - coste });
      }
    }
    const posiciones = {};
    for (const [t, L] of Object.entries(lotes)) {
      const acciones = L.reduce((s, l) => s + l.acciones, 0);
      if (acciones > 1e-9) {
        const coste = L.reduce((s, l) => s + l.coste, 0);
        posiciones[t] = { acciones, coste, costeMedio: coste / acciones, desde: L[0].fecha, lotes: L.map(l => ({ ...l })) };
      }
    }
    return { posiciones, realizadas, errores };
  }

  // ───────────────────────── valoración y revisión ─────────────────────────

  /** Une tus posiciones con el informe del mes (precio actual, tesis...). */
  function valorar(posiciones, empresas, hoy) {
    hoy = hoy || hoyISO();
    const filas = [];
    let total = 0;
    for (const [t, p] of Object.entries(posiciones)) {
      const e = empresas[t] || null;
      const precio = e && e.p != null ? e.p : null;
      const valor = precio != null ? p.acciones * precio : null;
      if (valor != null) total += valor;
      filas.push({ ticker: t, nombre: e ? e.n : "", acciones: p.acciones, coste: p.coste,
                   costeMedio: p.costeMedio, desde: p.desde, precio, valor, emp: e,
                   pl: valor != null ? valor - p.coste : null,
                   plPct: valor != null && p.coste > 0 ? valor / p.coste - 1 : null });
    }
    for (const f of filas) {
      f.peso = f.valor != null && total > 0 ? f.valor / total : null;
      f.revision = revisar(f, hoy);
    }
    filas.sort((a, b) => (b.valor || 0) - (a.valor || 0));
    return { filas, total };
  }

  /** Qué mirar de cada posición. NO hay «vender porque está cara»: en el
   *  backtest esa regla perdió frente al S&P 500. Se vende, si acaso, por
   *  tesis rota (decisión tuya). Niveles: rota > revisar > info > ok. */
  function revisar(f, hoy, tope) {
    tope = tope || TOPE_POSICION;
    const e = f.emp;
    const m = [];
    let nivel = "ok";
    const sube = (n) => { const o = { ok: 0, info: 1, revisar: 2, rota: 3 }; if (o[n] > o[nivel]) nivel = n; };

    if (!e || e.e === "sin_datos") {
      m.push("Sin precio ni tesis en el Vigía: no puedo valorarla ni vigilarla."); sube("revisar");
    } else {
      if (e.v === "DESCARTE" || e.v === "NO_INVERTIBLE") {
        m.push("Tesis rota: el bot de largo plazo ya la marca como " + e.v.replace("_", " ") +
               ". Es el motivo serio para plantearse vender."); sube("rota");
      }
      if (e.td != null && e.td > TESIS_ANTIGUA_DIAS) {
        m.push(`La tesis tiene ${e.td} días: se refrescará en el próximo ciclo.`); sube("info");
      }
      if (e.sv) {
        m.push("El precio ya supera el rango de valor de la tesis (sin margen de seguridad). " +
               "No es motivo de venta por sí solo; sí de no añadir más."); sube("info");
      }
      if (e.c) {
        m.push("Muy cara frente a su propio historial de 5 años. Solo informativo: en la prueba " +
               "histórica, vender por esto rindió PEOR que el S&P 500."); sube("info");
      }
    }
    if (mesesEntre(f.desde, hoy) >= MESES_REVISION) {
      m.push(`Llevas ${Math.floor(mesesEntre(f.desde, hoy))} meses con ella: toca revisión periódica (releer la tesis).`);
      sube("revisar");
    }
    if (f.peso != null && f.peso > tope + 1e-9) {
      m.push(`Pesa el ${(f.peso * 100).toFixed(0)} % de tu cartera mensual (tope ${(tope * 100).toFixed(0)} %): no añadas más.`);
      sube("revisar");
    }
    return { nivel, motivos: m };
  }

  /** Impuesto estimado si vendieras `acciones` ahora (FIFO). Aproximación: tipo único. */
  function impuestoSiVende(pos, acciones, precio, tipo) {
    let falta = acciones, coste = 0;
    for (const l of pos.lotes) {
      const toma = Math.min(falta, l.acciones);
      coste += l.coste * (toma / l.acciones);
      falta -= toma;
      if (falta <= 1e-12) break;
    }
    const resultado = acciones * precio - coste;
    return { resultado, impuesto: Math.max(0, resultado) * tipo };
  }

  /** Regla de los 2 meses (art. 33.5.f LIRPF): una pérdida por venta no es
   *  deducible si compras el mismo valor en los 2 meses anteriores o posteriores.
   *  Devuelve avisos para una compra que vas a hacer hoy. */
  function avisosDosMeses(ticker, ops, hoy) {
    const calc = calcular(ops);
    const out = [];
    for (const r of calc.realizadas) {
      if (r.ticker === ticker && r.resultado < 0) {
        const limite = sumarDias(r.fecha, 61);
        if (hoy <= limite) {
          out.push(`Vendiste ${ticker} con pérdida el ${r.fecha}. Si la recompras antes del ${limite}, ` +
                   "esa pérdida no se podrá deducir (regla de los 2 meses).");
        }
      }
    }
    return out;
  }

  /** Avisos para una VENTA que vas a hacer hoy: si hay compras del mismo valor en los
   *  últimos 2 meses y la venta es con pérdida. */
  function avisosVentaConPerdida(ticker, ops, hoy, perdida) {
    if (!perdida) return [];
    const recientes = ops.filter(o => o.ticker === ticker && o.tipo === "compra" &&
                                 mesesEntre(o.fecha, hoy) <= 2 && o.fecha <= hoy);
    return recientes.length
      ? [`Tienes compras de ${ticker} de los últimos 2 meses: vender ahora con pérdida podría hacer esa pérdida no deducible (regla de los 2 meses).`]
      : [];
  }

  // ───────────────────────── reparto de la compra ─────────────────────────

  function llenarAPartesIguales(total, hueco) {
    const claves = Object.keys(hueco);
    const asign = {};
    claves.forEach(k => (asign[k] = 0));
    let abiertos = claves.filter(k => hueco[k] > 1e-12);
    let resto = total;
    while (abiertos.length && resto > 1e-9) {
      const cuota = resto / abiertos.length;
      const cerrados = [];
      for (const k of abiertos) {
        const dar = Math.min(cuota, hueco[k] - asign[k]);
        asign[k] += dar;
        resto -= dar;
        if (hueco[k] - asign[k] <= 1e-12) cerrados.push(k);
      }
      if (!cerrados.length) break;
      abiertos = abiertos.filter(k => !cerrados.includes(k));
    }
    return asign;
  }

  /** Port de estrategia.repartir: `disponible` a partes iguales entre `idx`,
   *  sin que ninguna pase de `tope` del valor total de las acciones tras comprar
   *  (nunca menos de 1/nº de empresas). Lo que no cabe NO se fuerza: sobrante. */
  function repartir(disponible, idx, valorPos, tope) {
    tope = tope == null ? TOPE_POSICION : tope;
    if (!(disponible > 0)) return { compras: {}, sobrante: 0 };
    if (!idx.length) return { compras: {}, sobrante: disponible };
    const enCartera = Object.keys(valorPos).filter(k => valorPos[k] > 0);
    const n = new Set([...idx, ...enCartera]).size;
    const t = Math.max(tope, 1 / n);
    const acciones = Object.values(valorPos).reduce((s, v) => s + v, 0);
    let comprado = disponible;
    let compras = {};
    for (let it = 0; it < 200; it++) {
      const techo = t * (acciones + comprado);
      const hueco = {};
      idx.forEach(k => (hueco[k] = Math.max(0, techo - (valorPos[k] || 0))));
      compras = llenarAPartesIguales(disponible, hueco);
      const nuevo = Object.values(compras).reduce((s, v) => s + v, 0);
      if (Math.abs(nuevo - comprado) < 1e-9) { comprado = nuevo; break; }
      comprado = nuevo;
    }
    const out = {};
    let suma = 0;
    for (const [k, v] of Object.entries(compras)) if (v > 1e-9) { out[k] = v; suma += v; }
    return { compras: out, sobrante: Math.max(disponible - suma, 0) };
  }

  // ───────────────────────── marcador frente al S&P 500 ─────────────────────────

  function xirr(flujos) {
    if (flujos.length < 2) return null;
    const t0 = flujos[0].t;
    const npv = (r) => flujos.reduce((s, f) => s + f.v / Math.pow(1 + r, (f.t - t0) / DIA / 365), 0);
    let lo = -0.95, hi = 10, flo = npv(lo), fhi = npv(hi);
    if (!isFinite(flo) || !isFinite(fhi) || flo * fhi > 0) return null;
    for (let i = 0; i < 200; i++) {
      const mid = (lo + hi) / 2, fm = npv(mid);
      if (flo * fm <= 0) hi = mid; else { lo = mid; flo = fm; }
    }
    return (lo + hi) / 2;
  }

  function precioSpy(spy, fecha) {
    const f = spy.f;
    if (!f.length) return null;
    if (fecha < f[0]) return { precio: spy.c[0], fuera: true };
    let a = 0, b = f.length - 1;
    while (a < b) { const m = (a + b + 1) >> 1; if (f[m] <= fecha) a = m; else b = m - 1; }
    return { precio: spy.c[a], fuera: false };
  }

  /** Tu cartera mensual frente a poner EXACTAMENTE los mismos flujos de dinero,
   *  en las mismas fechas, en el S&P 500. Compra = dinero que sale; venta = entra.
   *  `hoy` es la FECHA DE VALORACIÓN: la de los precios del informe (por defecto,
   *  el último cierre del S&P 500 disponible), para valorar todo al mismo día. */
  function marcador(ops, empresas, spy, hoy) {
    hoy = hoy || (spy.f.length ? spy.f[spy.f.length - 1] : hoyISO());
    if (!ops.length) return null;
    const calc = calcular(ops);
    const flujos = [];
    for (const o of ops) {
      const bruto = o.acciones * o.precio;
      flujos.push({ fecha: o.fecha, v: o.tipo === "compra" ? -(bruto + o.comision) : bruto - o.comision });
    }
    flujos.sort((a, b) => a.fecha.localeCompare(b.fecha));

    let valorActual = 0;
    const sinValorar = [];
    for (const [t, p] of Object.entries(calc.posiciones)) {
      const e = empresas[t];
      if (e && e.p != null) valorActual += p.acciones * e.p; else sinValorar.push(t);
    }

    let unidades = 0, fuera = false;
    for (const f of flujos) {
      const s = precioSpy(spy, f.fecha);
      if (!s) return null;
      if (s.fuera) fuera = true;
      unidades += -f.v / s.precio;                   // flujo negativo (compra) → compras SPY
    }
    const spyHoy = precioSpy(spy, hoy).precio;          // al cierre de la fecha de valoración
    const valorSpy = unidades * spyHoy;

    const cf = flujos.map(f => ({ t: ms(f.fecha), v: f.v }));
    const t1 = ms(hoy);
    const dias = (t1 - cf[0].t) / DIA;
    const xU = xirr([...cf, { t: t1, v: valorActual }]);
    const xS = xirr([...cf, { t: t1, v: valorSpy }]);
    const neto = -flujos.reduce((s, f) => s + f.v, 0);          // dinero neto puesto
    return {
      dias, anualizable: dias >= 180,
      aportadoNeto: neto,
      usuario: { valor: valorActual, ganancia: valorActual - neto, xirr: xU },
      spy: { valor: valorSpy, ganancia: valorSpy - neto, xirr: xS },
      diferencia: valorActual - valorSpy,
      sinValorar, spyFueraDeRango: fuera,
      spyHasta: spy.f[spy.f.length - 1],
    };
  }

  const API = {
    TOPE_POSICION, MESES_REVISION, TESIS_ANTIGUA_DIAS,
    ms, iso, hoyISO, sumarDias, mesesEntre,
    normalizarOperacion, calcular, valorar, revisar, impuestoSiVende,
    avisosDosMeses, avisosVentaConPerdida, repartir, xirr, precioSpy, marcador,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = API;
  else raiz.Mensual = API;
})(typeof window !== "undefined" ? window : globalThis);
