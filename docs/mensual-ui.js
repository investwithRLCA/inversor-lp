/*
 * Pestaña «Mensual» del Vigía — la parte con pantalla. La lógica de cálculo
 * está en mensual.js (pura, probada con Node).
 *
 * PRIVACIDAD: tus compras y ventas se guardan SOLO en este navegador
 * (localStorage). No se envían a Supabase, ni a GitHub, ni a ningún sitio:
 * el repositorio es público y por eso la cartera no puede vivir allí.
 * Consecuencia: si borras los datos del navegador se pierden → usa «Exportar».
 */
(function () {
  "use strict";
  const M = window.Mensual;
  const CLAVE_OPS = "vigia.mensual.ops.v1";
  const CLAVE_CFG = "vigia.mensual.cfg.v1";

  let informe = null;          // JSON público del mes
  let estadoCarga = "idle";    // idle | cargando | ok | vacio | sinTabla | error
  let mensajeError = "";
  let ops = [];
  let cfg = { impuesto: 0.21, importe: "", paquete: "medio" };
  let almacenamientoOk = true;
  let abiertos = new Set();    // tarjetas desplegadas
  let prefill = null;
  let aviso = "";

  const $m = document.getElementById("mensual");

  // ───────────── utilidades ─────────────
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (n, d = 2) => (n == null || !isFinite(n)) ? "—" : Number(n).toLocaleString("es-ES", { minimumFractionDigits: d, maximumFractionDigits: d });
  const usd = (n, d = 2) => (n == null || !isFinite(n)) ? "—" : num(n, d) + " $";
  const pct = (n, d = 0) => (n == null || !isFinite(n)) ? "—" : num(n * 100, d) + " %";
  const signo = (n, d = 2) => (n == null || !isFinite(n)) ? "—" : (n > 0 ? "+" : "") + num(n, d);
  const fecha = (iso) => iso ? new Date(iso + "T00:00:00").toLocaleDateString("es-ES", { day: "2-digit", month: "short", year: "numeric" }) : "—";
  const redondeaAbajo = (n, d = 4) => Math.floor(n * 10 ** d + 1e-9) / 10 ** d;

  function leer(clave, defecto) {
    try { const t = localStorage.getItem(clave); return t ? JSON.parse(t) : defecto; }
    catch (e) { almacenamientoOk = false; return defecto; }
  }
  function guardar(clave, valor) {
    try { localStorage.setItem(clave, JSON.stringify(valor)); }
    catch (e) { almacenamientoOk = false; }
  }
  function cargarLocal() {
    const o = leer(CLAVE_OPS, []);
    ops = (Array.isArray(o) ? o : []).map((x) => M.normalizarOperacion(x).op).filter(Boolean);
    cfg = { ...cfg, ...leer(CLAVE_CFG, {}) };
  }

  // ───────────── carga del informe público ─────────────
  async function cargarInforme() {
    if (estadoCarga === "cargando") return;
    estadoCarga = "cargando"; pintar();
    try {
      const { data, error } = await obtenerCliente().from("v_mensual_ultimo").select("*").limit(1);
      if (error) {
        const t = `${error.code || ""} ${error.message || ""}`;
        if (/42P01|PGRST205|does not exist|schema cache|404/i.test(t)) { estadoCarga = "sinTabla"; }
        else { estadoCarga = "error"; mensajeError = error.message || String(error); }
      } else if (!data || !data.length) { estadoCarga = "vacio"; }
      else { informe = data[0].informe; estadoCarga = "ok"; }
    } catch (e) { estadoCarga = "error"; mensajeError = (e && e.message) || String(e); }
    pintar();
  }

  // ───────────── piezas de pantalla ─────────────
  function chip(texto, clase) { return `<span class="m-chip ${clase || ""}">${esc(texto)}</span>`; }

  function valoresPos() {
    const calc = M.calcular(ops);
    const val = M.valorar(calc.posiciones, informe ? informe.empresas : {}, M.hoyISO());
    const valorPos = {};
    val.filas.forEach((f) => { if (f.valor != null) valorPos[f.ticker] = f.valor; });
    return { calc, val, valorPos };
  }

  const NOMBRE_PAQ = { corto: "1–3 años", medio: "3–5 años", largo: "5+ años", dividendos: "Dividendos" };
  // Paquetes ACTIVOS (los que se enseñan, se miden y se pueden elegir). Los retirados no superaron
  // la prueba histórica: solo se conservan sus nombres para las operaciones antiguas de la cartera.
  const PAQS_ORDEN = ["medio", "dividendos"];
  const PAQS_RETIRADOS = ["corto", "largo"];
  const nombrePaq = (k) => (PAQS_RETIRADOS.includes(k) ? NOMBRE_PAQ[k] + " (retirado)" : NOMBRE_PAQ[k] || k);

  /** Los paquetes del informe; si es un informe antiguo (solo candidatas), un paquete corto. */
  function paquetesDe(inf) {
    const base = baseDe(inf);
    const d = inf.dividendos;
    if (d && typeof d === "object" && !base.dividendos) {
      base.dividendos = { nombre: d.nombre, horizonte: d.horizonte, etiqueta: d.etiqueta || "Dividendos", descripcion: d.descripcion,
                          criterios: d.criterios || [], n_aptas: (d.resumen || {}).n_elegibles, candidatas: d.candidatas || [],
                          vetadas: [], en_espera: (d.en_espera || []).map((c) => ({ ticker: c.ticker, motivo: `${num(c.puntos, 0)} puntos; fuera del máximo o del tope por sector` })),
                          esDividendos: true };
    }
    return base;
  }
  function baseDe(inf) {
    if (inf.paquetes && Object.keys(inf.paquetes).length) return { ...inf.paquetes };
    return { corto: { nombre: "Descuento con tesis", horizonte: "1–3 años", etiqueta: "1–3 años", descripcion: "", criterios: [],
                      n_aptas: (inf.candidatas || []).length, candidatas: inf.candidatas || [],
                      vetadas: inf.vetadas || [], en_espera: inf.en_espera || [] } };
  }
  const tieneTesis = (c) => c.veredicto && c.veredicto !== "SIN_TESIS";
  const pillNivel = (c) => tieneTesis(c)
    ? `<span class="pill v-${esc(c.veredicto)}">${esc(c.veredicto.replace("_", " "))}</span>`
    : `<span class="m-chip" title="Solo ha pasado el cribado cuantitativo; aún sin análisis de IA">solo cribado</span>`;

  function lineaClave(k) {
    if (!k) return "";
    const p = [];
    if (k.roic != null) p.push(`ROIC ${num(k.roic, 0)} %`);
    if (k.crec != null) p.push(`ingresos ${num(k.crec, 1)} %/año`);
    if (k.fcf_margen != null) p.push(`caja libre ${num(k.fcf_margen, 0)} % de ventas`);
    if (k.deuda != null) p.push(k.deuda <= 0 ? "sin deuda neta" : `deuda/EBITDA ${num(k.deuda, 1)}x`);
    if (k.p_fcf != null && k.p_fcf > 0) p.push(`${num(k.p_fcf, 0)}x caja libre`);
    if (k.per != null && k.per > 0) p.push(`PER ${num(k.per, 0)}`);
    return p.join(" · ");
  }

  function tarjetaCandidata(c, paquete, reparto, valorPos, totalTras, hayImporte) {
    const comp = reparto.compras[c.ticker] || 0;
    const dosMeses = M.avisosDosMeses(c.ticker, ops, M.hoyISO());
    const ia = c.ia && !c.ia.error ? c.ia : null;
    const clave = `c:${paquete}:${c.ticker}`;
    const abierta = abiertos.has(clave);
    const avisos = (c.avisos || []).concat(dosMeses);
    const esCalidad = paquete !== "corto";
    const esDiv = paquete === "dividendos";
    const dv = c.dividendo || {}, emp = c.empresa || {};
    const r5 = (c.rend || {})["5a"] || (c.rend || {})["3a"];
    const nr5 = (c.rend || {})["5a"] ? "5 años" : "3 años";
    const cagrDiv = dv.cagr_5a != null ? dv.cagr_5a : dv.cagr_3a;
    const subPrecio = esDiv ? (c.cap != null ? `cap. ${num(c.cap, 0)} M$` : "")
      : (c.descuento != null ? `${signo(-c.descuento * 100, 0)} % vs su mediana 5a` : "");
    return `<article class="m-cand ${abierta ? "abierta" : ""}" data-t="${esc(c.ticker)}">
      <div class="m-cab">
        <div><div class="m-tk">${esc(c.ticker)} ${c.doble_descuento ? '<span title="Por debajo del precio de entrada de la tesis">⭐</span>' : ""}
            ${esDiv ? chip(num(dv.rentab_div, 1) + " % dividendo", "m-bien") : pillNivel(c)}</div>
            <div class="m-nombre">${esc(c.nombre)}${c.sector ? " · " + esc(c.sector) : ""}</div></div>
        <div class="m-precio num">${num(c.precio)}<small>${esCalidad && c.puntos != null ? "puntos " + num(c.puntos, 0) + " · " : ""}${esc(subPrecio)}</small></div>
      </div>
      <div class="m-compra">${hayImporte
        ? (comp > 0
          ? `Comprar <strong class="num">${usd(comp)}</strong> <span class="m-dim">≈ ${num(redondeaAbajo(comp / c.precio, 2), 2)} acc. · quedaría en el ${pct((((valorPos[c.ticker] || 0) + comp) / totalTras))} de tu cartera mensual</span>`
          : `<span class="m-dim">Ya pesa el tope en tu cartera mensual: no añadas más.</span>`)
        : `<span class="m-dim">Reparto orientativo: ${pct(c.peso)} del importe</span>`}</div>
      ${esCalidad && c.clave ? `<div class="m-dim m-linea">${esc(lineaClave(c.clave))}</div>` : ""}
      ${esDiv ? `<div class="m-dim m-linea">Dividendo ${usd(dv.dps)} por acción al año · ${num(dv.anios_sin_recorte, 0)} años sin recorte${cagrDiv != null ? " · crece " + signo(cagrDiv, 1) + " %/año" : ""}${emp.payout != null ? " · reparte el " + num(emp.payout, 0) + " % de su " + (emp.financiera ? "beneficio/caja" : "caja libre") : ""}${emp.dilucion_5a != null ? " · acciones 5a " + signo(emp.dilucion_5a, 0) + " %" : ""}</div>` : ""}
      <div class="m-chips">
        ${esCalidad && c.calidad != null ? chip("calidad " + num(c.calidad, 0)) + chip("crecimiento " + num(c.crecimiento, 0)) + chip("precio " + num(c.valoracion, 0)) : ""}
        ${esDiv && r5 ? chip(nr5 + ": precio " + signo(r5.precio, 1) + " + dividendo " + signo(r5.div, 1) + " = " + signo(r5.total, 1) + " %/año") : ""}
        ${ia ? chip("IA: " + ia.tipo + " (" + ia.confianza + ")", ia.tipo === "estructural" ? "m-mal" : ia.tipo === "temporal" ? "m-bien" : "") : ""}
        ${avisos.length ? chip("⚠ " + avisos.length + " aviso(s)", "m-warn") : ""}
      </div>
      <div class="m-acciones">
        <button class="m-btn" data-accion="detalle" data-t="${esc(c.ticker)}" data-k="${esc(clave)}">${abierta ? "Ocultar detalle" : "Ver detalle"}</button>
        <button class="m-btn m-btn-ppal" data-accion="registrar" data-t="${esc(c.ticker)}" data-p="${c.precio}" data-paq="${esc(paquete)}" data-a="${comp > 0 ? redondeaAbajo(comp / c.precio, 2) : ""}">Registrar compra</button>
      </div>
      <div class="m-detalle">
        ${esCalidad && (c.razones || []).length ? `<p><strong>Por qué está en este paquete:</strong></p><ul>${c.razones.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
        ${!esCalidad ? (ia ? `<p><strong>Por qué ha caído (IA):</strong> ${esc(ia.causa)}</p>
                ${ia.riesgo_clave ? `<p><strong>Riesgo clave:</strong> ${esc(ia.riesgo_clave)}</p>` : ""}
                ${ia.que_vigilar ? `<p><strong>Qué vigilar:</strong> ${esc(ia.que_vigilar)}</p>` : ""}`
              : `<p class="m-dim">Sin explicación de la IA en este informe.</p>`) : ""}
        ${c.salud ? `<p><strong>Último trimestre:</strong> ${esc(c.salud.resumen)}</p>` : ""}
        ${c.tesis ? `<p><strong>Tesis (IA):</strong> ${esc(c.tesis)}</p>` : ""}
        ${esDiv ? "" : tieneTesis(c)
          ? `<p><strong>Tesis:</strong> nota ${c.nota ?? "—"}/10 · rango de valor ${c.valor_min ?? "—"}–${c.valor_max ?? "—"} · precio de entrada ${c.precio_entrada ?? "—"}${c.tesis_dias != null ? ` · analizada hace ${c.tesis_dias} días` : ""}</p>`
          : `<p class="m-dim">Sin tesis de IA todavía: nota de cribado cuantitativo ${c.quant ?? "—"}/100.</p>`}
        ${avisos.map((a) => `<p class="m-aviso">⚠️ ${esc(a)}</p>`).join("")}
        ${(c.titulares || []).length ? `<p><strong>Titulares recientes</strong></p><ul>${c.titulares.map((t) => `<li>${esc(t.fecha)} · ${esc(t.titular)}</li>`).join("")}</ul>` : ""}
      </div>
    </article>`;
  }

  /** Comparador de las elegidas de la sección de dividendos: precio + dividendo = rentabilidad total. */
  function tablaDividendos(d) {
    const cs = d.candidatas || [];
    if (!cs.length) return "";
    const ref = ((d.referencia || {}).rend || {})["5a"];
    const gr = ((d.grupo || {})["5a"]) || {};
    const fila = (c) => {
      const dv = c.dividendo || {}, e = c.empresa || {}, r = (c.rend || {})["5a"];
      const cg = dv.cagr_5a != null ? dv.cagr_5a : dv.cagr_3a;
      const bate = r && ref ? r.total >= ref.total : null;
      return `<tr><td><strong>${esc(c.ticker)}</strong></td><td class="num">${num(dv.rentab_div, 1)} %</td><td class="num">${cg != null ? signo(cg, 1) + " %" : "—"}</td>
        <td class="num">${num(dv.anios_sin_recorte, 0)}</td><td class="num">${e.payout != null ? num(e.payout, 0) + " %" : "—"}</td>
        <td class="num">${e.dilucion_5a != null ? signo(e.dilucion_5a, 0) + " %" : "—"}</td>
        <td class="num">${r ? signo(r.precio, 1) : "—"}</td><td class="num">${r ? signo(r.div, 1) : "—"}</td>
        <td class="num"><strong class="${bate == null ? "" : bate ? "m-pos" : "m-neg"}">${r ? signo(r.total, 1) + " %" : "—"}</strong></td></tr>`;
    };
    return `<details class="m-det" open><summary>Comparar las ${cs.length} elegidas</summary>
      <p class="m-dim">Rentabilidad anual media de los últimos 5 años = lo que ha subido el precio + lo que ha dado de dividendo. Verde = mejor que el Russell 2000. Son empresas que hoy siguen cotizando (supervivientes): dice lo que pasó, no lo que pasará.</p>
      <div class="m-scroll"><table class="m-tabla m-comp"><tr><th>Empresa</th><th>Dividendo</th><th>Crece/año</th><th>Años sin recorte</th><th>Reparte</th><th>Acciones 5a</th><th>Precio 5a</th><th>+ Dividendo</th><th>= Total/año</th></tr>
      ${cs.map(fila).join("")}
      ${ref ? `<tr class="m-ref"><td>Russell 2000 (IWM)</td><td></td><td></td><td></td><td></td><td></td><td class="num">${signo(ref.precio, 1)}</td><td class="num">${signo(ref.div, 1)}</td><td class="num"><strong>${signo(ref.total, 1)} %</strong></td></tr>` : ""}
      ${gr.total != null ? `<tr class="m-ref"><td>Mediana de las elegibles${gr.n ? " (" + gr.n + ")" : ""}</td><td></td><td></td><td></td><td></td><td></td><td class="num">${signo(gr.precio, 1)}</td><td class="num">${gr.precio != null ? signo(gr.total - gr.precio, 1) : "—"}</td><td class="num"><strong>${signo(gr.total, 1)} %</strong></td></tr>` : ""}
      </table></div></details>`;
  }

  function seccionPaquetes() {
    const inf = informe;
    const paqs = paquetesDe(inf);
    let claves = PAQS_ORDEN.filter((k) => paqs[k]);
    if (!claves.length) claves = Object.keys(paqs);     // informe muy antiguo: solo trae un paquete retirado
    if (!claves.includes(cfg.paquete)) cfg.paquete = claves[0];
    const sel = cfg.paquete, P = paqs[sel];
    const importe = parseFloat(String(cfg.importe).replace(",", "."));
    const hayImporte = importe > 0;
    const { valorPos } = valoresPos();
    const cands = P.candidatas || [];
    let reparto = { compras: {}, sobrante: 0 };
    if (hayImporte && cands.length) {
      reparto = M.repartir(importe, cands.map((c) => c.ticker), valorPos, inf.parametros.tope_posicion);
    }
    const totalTras = Object.values(valorPos).reduce((s, v) => s + v, 0) + Object.values(reparto.compras).reduce((s, v) => s + v, 0);

    let h = `<h2>Este mes <span class="m-sub">${esc(inf.mes)} · precios a ${esc(fecha(inf.datos_a))}</span></h2>`;
    (inf.avisos || []).forEach((a) => (h += `<div class="m-aviso">⚠️ ${esc(a)}</div>`));
    h += `<div class="m-paquetes" role="group" aria-label="Paquete de inversión">${claves.map((k) =>
      `<button class="m-paq" data-accion="paquete" data-paq="${k}" aria-pressed="${k === sel}">
         <span class="m-paq-h">${esc(paqs[k].etiqueta || NOMBRE_PAQ[k])}</span>
         <span class="m-paq-n">${(paqs[k].candidatas || []).length} elegida${(paqs[k].candidatas || []).length === 1 ? "" : "s"}</span></button>`).join("")}</div>`;
    h += `<div class="m-paq-info"><strong>${esc(P.nombre)}</strong> <span class="m-sub">${esc(P.horizonte)}</span>
            ${P.descripcion ? `<p class="m-dim">${esc(P.descripcion)}</p>` : ""}
            ${(P.criterios || []).length ? `<details class="m-det"><summary>Criterios exactos</summary><ul>${P.criterios.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></details>` : ""}
          </div>`;
    if (sel === "dividendos") {
      h += `<div class="m-aviso">Esta sección <strong>no se ha podido probar en el pasado</strong> (faltan las empresas que quebraron o salieron del índice, y las cifras históricas de abajo son de supervivientes). Mira «Seguimiento» más abajo antes de poner dinero serio. Ojo fiscal: los dividendos tributan cada año como ahorro (en EE. UU. retienen un 15 % que normalmente puedes descontar), la revalorización solo al vender.</div>`;
    } else if (sel === "medio") {
      h += `<div class="m-aviso">Este paquete <strong>sí se ha probado en el pasado</strong> (cuentas tal como se conocían en cada fecha, 2015-2025, empresas del S&amp;P 500 de hoy): rindió más que el S&amp;P 500 a 1, 2 y 3 años, pero <strong>no más que una selección sin criterio del mismo listado</strong> (+0,5 pp a 1 año, +1,3 pp a 2 años, dentro del margen de error; −4,5 pp a 3 años). Como el listado son las ganadoras de hoy, esa prueba tiene el viento a favor: <strong>no demuestra ventaja</strong>. Se mantiene en prueba en vivo: mira «Seguimiento» más abajo antes de poner dinero serio.</div>`;
    }
    h += `<label class="m-campo">¿Cuánto vas a invertir ahora en este paquete? ($)
            <input id="m-importe" type="text" inputmode="decimal" placeholder="p. ej. 500" value="${esc(cfg.importe)}" autocomplete="off"></label>`;

    if (!cands.length) {
      h += `<p class="m-vacio"><strong>Ninguna empresa cumple hoy los criterios de este paquete.</strong> No fuerces ninguna compra: el dinero puede esperar.</p>`;
    } else {
      cands.forEach((c) => (h += tarjetaCandidata(c, sel, reparto, valorPos, totalTras, hayImporte)));
      if (reparto.sobrante > 0.005) {
        h += `<div class="m-aviso">${usd(reparto.sobrante)} no caben por los topes (máx. ${pct(inf.parametros.tope_posicion)} por empresa): guárdalos para el mes que viene, no los fuerces.</div>`;
      }
    }

    const otras = [].concat((P.vetadas || []).map((v) => `<li><strong>${esc(v.ticker)}</strong> (frenada): ${esc(v.motivo)}</li>`),
                            (P.en_espera || []).map((v) => `<li><strong>${esc(v.ticker)}</strong>: ${esc(v.motivo)}</li>`));
    if (otras.length) h += `<details class="m-det"><summary>Otras que cumplían los criterios (${otras.length})</summary><ul>${otras.join("")}</ul></details>`;
    if (sel === "dividendos") {
      h += tablaDividendos(inf.dividendos || {});
      const d = inf.dividendos || {}, rd = d.resumen || {};
      h += `<p class="m-nota">Universo: ${esc(d.fuente || "small caps")} · ${rd.n_universo ?? "—"} empresas · ${rd.n_pagan ?? "—"} pagan dividendo regular · ${rd.n_puerta_dividendo ?? "—"} cumplen la puerta del dividendo (≥5 años pagando, sin recortes) · ${rd.n_elegibles ?? "—"} elegibles tras revisar caja, deuda y dilución en la SEC.${d.descartes ? " Descartes más comunes: " + Object.entries(d.descartes).slice(0, 4).map(([k, v]) => esc(k) + " (" + v + ")").join(", ") + "." : ""}</p>`;
      return `<section class="m-card">${h}</section>`;
    }
    const r = inf.resumen || {};
    h += `<p class="m-nota">Universo de este mes: ${r.n_lista ?? "—"} empresas elegibles${r.n_tier_a != null ? ` (${r.n_tier_a} con análisis de IA favorable, ${r.n_tier_b} solo con cribado cuantitativo)` : ""}; ${r.n_con_senal ?? "—"} con 5 años de precios. Cumplen este paquete: ${P.n_aptas ?? "—"}.</p>`;
    return `<section class="m-card">${h}</section>`;
  }

  function seccionSeguimiento() {
    const seg = informe && informe.seguimiento;
    let h = `<h2>¿Funcionan los paquetes? <span class="m-sub">seguimiento automático</span></h2>
      <p class="m-dim">Cada mes la herramienta apunta qué habría elegido cada paquete y, con los meses, mide cuánto habría rendido frente al S&amp;P 500 (la sección de dividendos, frente al Russell 2000) y frente al resto del universo (todas las empresas elegibles a partes iguales). Es información pública de empresas: no usa tu cartera.</p>`;
    // informes guardados antes de retirar paquetes pueden traer cohortes de los retirados: no se enseñan
    const cohortes = ((seg && seg.cohortes) || []).filter((c) => PAQS_ORDEN.includes(c.paquete));
    if (!seg || !cohortes.length) {
      h += `<p class="m-vacio">Aún no hay historial: la primera cifra aparece cuando haya un informe anterior con al menos unos días de recorrido, y es fiable de verdad a partir de 12-24 meses.</p>`;
      return `<section class="m-card">${h}</section>`;
    }
    const fila = (k) => {
      const s = (seg.resumen || {})[k]; if (!s || !s.cohortes) return "";
      const nom = k === "dividendos" ? "Dividendos <small class=\"m-dim\">vs Russell 2000</small>" : nombrePaq(k);
      if (!s.cohortes_maduras) return `<tr><td>${nom}</td><td colspan="3" class="m-dim">${s.cohortes} cohorte(s), aún sin 3 meses</td></tr>`;
      const cl = s.exceso_medio >= 0 ? "m-pos" : "m-neg";
      return `<tr><td>${nom}</td><td class="num">${s.cohortes_maduras}</td>
        <td class="num"><span class="${cl}">${signo(s.exceso_medio * 100, 1)} pp</span></td>
        <td class="num">${s.superan_spy}/${s.cohortes_maduras}</td></tr>`;
    };
    h += `<table class="m-tabla"><tr><th></th><th>Cohortes ≥3 m</th><th>Exceso vs referencia</th><th>Superan</th></tr>${PAQS_ORDEN.map(fila).join("")}</table>`;
    const conPool = PAQS_ORDEN.filter((k) => seg.resumen && seg.resumen[k] && seg.resumen[k].exceso_medio_pool != null);
    if (conPool.length) {
      h += `<p class="m-dim">Frente al resto del universo: ${conPool.map((k) => `${nombrePaq(k)} ${signo(seg.resumen[k].exceso_medio_pool * 100, 1)} pp`).join(" · ")}. Si los paquetes baten al S&amp;P pero no al resto del universo, lo que funciona es el filtro de calidad, no la selección.</p>`;
    }
    const ta = PAQS_ORDEN.map((k) => (seg.resumen || {})[k]).filter((s) => s && s.tier_a && s.tier_b);
    if (ta.length) {
      const media = (t) => { let n = 0, sx = 0; ta.forEach((s) => { n += s[t].n; sx += s[t].rent_media * s[t].n; }); return n ? sx / n : null; };
      h += `<p class="m-dim">Rentabilidad media por empresa elegida: con análisis de IA ${signo(media("tier_a") * 100, 1)} % · solo con cribado ${signo(media("tier_b") * 100, 1)} % (así se verá con el tiempo si la IA aporta algo).</p>`;
    }
    const recientes = [...cohortes].sort((a, b) => b.datos_a.localeCompare(a.datos_a) || a.paquete.localeCompare(b.paquete)).slice(0, 9);
    h += `<details class="m-det"><summary>Últimas cohortes (${cohortes.length})</summary>
      <table class="m-tabla"><tr><th>Mes · paquete</th><th>Días</th><th>Paquete</th><th>Referencia</th></tr>
      ${recientes.map((c) => `<tr><td>${esc(c.mes)} · ${esc(nombrePaq(c.paquete))}${c.n_sin_precio ? ` <span class="m-dim">(${c.n_sin_precio} sin precio)</span>` : ""}</td>
        <td class="num">${c.dias}</td><td class="num">${c.rent != null ? signo(c.rent * 100, 1) + " %" : "—"}</td><td class="num">${c.spy != null ? signo(c.spy * 100, 1) + " % <span class=\"m-dim\">" + (c.ref === "IWM" ? "R2000" : "S&amp;P") + "</span>" : "—"}</td></tr>`).join("")}</table></details>`;
    h += `<div class="m-aviso">${esc(seg.aviso || "")}</div>`;
    return `<section class="m-card">${h}</section>`;
  }

  function seccionCartera() {
    const { calc, val } = valoresPos();
    let h = `<h2>Mi cartera mensual</h2>`;
    if (!val.filas.length) {
      h += `<p class="m-dim">Todavía no hay posiciones. Registra tu primera compra más abajo.</p>`;
      return `<section class="m-card">${h}</section>`;
    }
    const coste = val.filas.reduce((s, f) => s + f.coste, 0);
    const valorV = val.filas.filter((f) => f.valor != null);
    const valor = valorV.reduce((s, f) => s + f.valor, 0), costeV = valorV.reduce((s, f) => s + f.coste, 0);
    h += `<div class="m-resumen"><div><span class="m-et">Valor</span><span class="num">${usd(valor)}</span></div>
          <div><span class="m-et">Coste</span><span class="num">${usd(costeV)}</span></div>
          <div><span class="m-et">Resultado</span><span class="num ${valor - costeV >= 0 ? "m-pos" : "m-neg"}">${signo(valor - costeV)} $ (${signo(costeV > 0 ? (valor / costeV - 1) * 100 : null, 1)} %)</span></div></div>`;
    calc.errores.forEach((e) => (h += `<div class="m-aviso">⚠️ ${esc(e)}</div>`));
    const porPaq = M.resumenPorPaquete(val.filas);
    if (Object.keys(porPaq).length > 1 || (Object.keys(porPaq).length === 1 && !porPaq.sin)) {
      h += `<table class="m-tabla"><tr><th>Por paquete</th><th>Valor</th><th>Coste</th><th>Resultado</th></tr>${[...PAQS_ORDEN, ...PAQS_RETIRADOS, "sin"].filter((k) => porPaq[k]).map((k) => {
        const o = porPaq[k], r = o.coste > 0 ? o.valor / o.coste - 1 : null;
        return `<tr><td>${k === "sin" ? "Sin paquete" : esc(nombrePaq(k))}</td><td class="num">${usd(o.valor, 0)}</td><td class="num">${usd(o.coste, 0)}</td><td class="num"><span class="${(r ?? 0) >= 0 ? "m-pos" : "m-neg"}">${r != null ? signo(r * 100, 1) + " %" : "—"}</span></td></tr>`;
      }).join("")}</table>`;
    }
    val.filas.forEach((f) => {
      const r = f.revision;
      const abierta = abiertos.has("p:" + f.ticker);
      const clase = { rota: "m-mal", revisar: "m-warn", info: "", ok: "m-bien" }[r.nivel];
      const etiqueta = { rota: "Tesis rota", revisar: "Revisar", info: "Info", ok: "En orden" }[r.nivel];
      const pos = calc.posiciones[f.ticker];
      let imp = "";
      if (f.precio != null) {
        const s = M.impuestoSiVende(pos, pos.acciones, f.precio, cfg.impuesto);
        const avisos = M.avisosVentaConPerdida(f.ticker, ops, M.hoyISO(), s.resultado < 0);
        imp = `<p><strong>Si vendieras todo ahora:</strong> resultado ≈ ${signo(s.resultado)} $, impuesto estimado ≈ ${usd(s.impuesto)} (al ${pct(cfg.impuesto)}; aproximación, no asesoramiento fiscal).</p>` +
              avisos.map((a) => `<p class="m-aviso">⚠️ ${esc(a)}</p>`).join("");
      }
      h += `<article class="m-posicion ${abierta ? "abierta" : ""}">
        <div class="m-cab">
          <div><div class="m-tk">${esc(f.ticker)} ${chip(etiqueta, clase)}${f.paq ? " " + chip(nombrePaq(f.paq)) : ""}</div><div class="m-nombre">${esc(f.nombre)}</div></div>
          <div class="m-precio num">${f.valor != null ? usd(f.valor, 0) : "—"}<small class="${(f.pl ?? 0) >= 0 ? "m-pos" : "m-neg"}">${f.plPct != null ? signo(f.plPct * 100, 1) + " %" : "sin precio"}</small></div>
        </div>
        <div class="m-dim m-linea">${num(f.acciones, 4)} acc. · coste medio ${num(f.costeMedio)} · ${f.peso != null ? pct(f.peso) + " de la cartera" : ""} · desde ${esc(fecha(f.desde))}</div>
        <div class="m-acciones">
          <button class="m-btn" data-accion="detallepos" data-t="${esc(f.ticker)}">${abierta ? "Ocultar" : "Ver detalle"}</button>
          <button class="m-btn" data-accion="vender" data-t="${esc(f.ticker)}" data-p="${f.precio ?? ""}" data-a="${f.acciones}">Registrar venta</button>
        </div>
        <div class="m-detalle">${r.motivos.map((x) => `<p>${esc(x)}</p>`).join("") || '<p class="m-dim">Sin nada que señalar.</p>'}${imp}</div>
      </article>`;
    });
    return `<section class="m-card">${h}</section>`;
  }

  function seccionMarcador() {
    if (!ops.length || !informe) return "";
    const mk = M.marcador(ops, informe.empresas, informe.spy, informe.datos_a);
    if (!mk) return "";
    const adelante = mk.diferencia >= 0;
    let h = `<h2>Marcador frente al S&amp;P 500</h2>
      <p class="m-dim">Tus mismos flujos de dinero, en las mismas fechas, puestos en el S&amp;P 500 (SPY). Valorado a ${esc(fecha(informe.datos_a))}.</p>
      <table class="m-tabla"><tr><th></th><th>Valor</th><th>Ganancia</th><th>Anual</th></tr>
      <tr><td>Tu cartera mensual</td><td class="num">${usd(mk.usuario.valor, 0)}</td><td class="num">${signo(mk.usuario.ganancia, 0)} $</td><td class="num">${mk.anualizable ? pct(mk.usuario.xirr, 1) : "—"}</td></tr>
      <tr><td>S&amp;P 500</td><td class="num">${usd(mk.spy.valor, 0)}</td><td class="num">${signo(mk.spy.ganancia, 0)} $</td><td class="num">${mk.anualizable ? pct(mk.spy.xirr, 1) : "—"}</td></tr></table>
      <p class="${adelante ? "m-pos" : "m-neg"}"><strong>${adelante ? "Vas por delante" : "Vas por detrás"} del S&amp;P 500 en ${usd(Math.abs(mk.diferencia), 0)}.</strong></p>`;
    if (!mk.anualizable) h += `<p class="m-nota">Llevas menos de 6 meses: la rentabilidad anual aún no significa nada y la diferencia es ruido. Juzga a partir de 12-24 meses.</p>`;
    if (mk.sinValorar.length) h += `<div class="m-aviso">⚠️ No tengo precio de ${esc(mk.sinValorar.join(", "))} (no están en tu lista del Vigía): no cuentan en el valor y el marcador sale incompleto.</div>`;
    if (mk.spyFueraDeRango) h += `<div class="m-aviso">⚠️ Alguna operación es anterior al histórico del S&amp;P 500 guardado (unos 3 años): se usó el primer precio disponible.</div>`;
    return `<section class="m-card">${h}</section>`;
  }

  function seccionRegistro() {
    const p = prefill || {};
    const tickers = informe ? Object.keys(informe.empresas).sort() : [];
    let h = `<h2>Registrar operación</h2>
      <p class="m-dim">Se guarda solo en este dispositivo. Anota lo que hayas hecho de verdad en tu bróker.</p>
      <form id="m-form" class="m-form" autocomplete="off">
        <div class="m-fila2">
          <label>Tipo<select name="tipo"><option value="compra" ${p.tipo !== "venta" ? "selected" : ""}>Compra</option><option value="venta" ${p.tipo === "venta" ? "selected" : ""}>Venta</option></select></label>
          <label>Ticker<input name="ticker" list="m-tickers" required value="${esc(p.ticker || "")}" autocapitalize="characters"></label>
        </div>
        <datalist id="m-tickers">${tickers.map((t) => `<option value="${esc(t)}">`).join("")}</datalist>
        <div class="m-fila2">
          <label>Acciones<input name="acciones" type="text" inputmode="decimal" required value="${esc(p.acciones ?? "")}"></label>
          <label>Precio por acción ($)<input name="precio" type="text" inputmode="decimal" required value="${esc(p.precio ?? "")}"></label>
        </div>
        <div class="m-fila2">
          <label>Comisión total ($)<input name="comision" type="text" inputmode="decimal" placeholder="0"></label>
          <label>Fecha<input name="fecha" type="date" required value="${esc(p.fecha || M.hoyISO())}"></label>
        </div>
        <label>Paquete (opcional, para ver tu resultado por horizonte)
          <select name="paq"><option value="">—</option>${[...PAQS_ORDEN, ...PAQS_RETIRADOS.filter((k) => p.paq === k)].map((k) => `<option value="${k}" ${p.paq === k ? "selected" : ""}>${esc(nombrePaq(k))}</option>`).join("")}</select></label>
        <div id="m-form-msg"></div>
        <button class="m-btn m-btn-ppal" type="submit">Guardar operación</button>
      </form>`;
    const ult = [...ops].sort((a, b) => b.fecha.localeCompare(a.fecha)).slice(0, 40);
    h += `<h3>Operaciones (${ops.length})</h3>`;
    h += ult.length ? `<ul class="m-ops">${ult.map((o) => `<li><span class="${o.tipo === "compra" ? "m-pos" : "m-neg"}">${o.tipo === "compra" ? "Compra" : "Venta"}</span>
        <strong>${esc(o.ticker)}</strong> ${num(o.acciones, 4)} × ${num(o.precio)} · ${esc(fecha(o.fecha))}${o.paq ? " · " + esc(nombrePaq(o.paq)) : ""}
        <button class="m-link" data-accion="borrar" data-id="${esc(o.id)}">borrar</button></li>`).join("")}</ul>` : `<p class="m-dim">Aún no hay operaciones.</p>`;
    h += `<div class="m-acciones">
        <button class="m-btn" data-accion="exportar">Exportar copia (.json)</button>
        <label class="m-btn m-fichero">Importar copia<input id="m-importar" type="file" accept=".json,application/json" hidden></label>
      </div>
      ${almacenamientoOk ? "" : '<div class="m-aviso">⚠️ Este navegador no deja guardar datos (¿modo privado?). Lo que registres se perderá al cerrar: exporta la copia.</div>'}
      <p class="m-nota">Si borras los datos del navegador, o cambias de móvil, se pierden las operaciones: exporta una copia de vez en cuando y guárdala (nota, correo, nube). Importar suma las operaciones nuevas sin duplicar las que ya tienes.</p>
      <label class="m-campo m-pequeno">Tipo impositivo para las estimaciones (%)<input id="m-imp" type="text" inputmode="decimal" value="${esc(String(cfg.impuesto * 100).replace(".", ","))}"></label>`;
    return `<section class="m-card" id="m-registro">${h}</section>`;
  }

  function seccionNotas() {
    return `<section class="m-card m-notas"><h2>Cómo funciona esto (y cuánto fiarse)</h2>
      <ul>
        <li><strong>Qué mira:</strong> las empresas que pasan el cribado de calidad del bot y, de ellas, las que cumplen el paquete <em>3–5 años</em> (buen negocio a precio razonable). Aparte, la sección de <em>Dividendos</em> con small caps. Son los dos únicos paquetes activos.</li>
        <li><strong>Qué se ha quitado y por qué:</strong> se probaron en el pasado (2015-2025) con las cuentas tal como se conocían en cada fecha. <em>1–3 años</em> (barata frente a su historial) y <em>5+ años</em> (compounders) <strong>no superaron ni a una selección sin criterio del mismo listado</strong> (−8,4 pp a 2 años y −0,4 pp a 1 año), así que se retiraron. Las compras que ya tengas anotadas con esos paquetes se conservan, marcadas como «retirado».</li>
        <li><strong>Lo que queda en 3–5 años:</strong> rindió más que el S&amp;P 500 en la prueba, pero <em>no más que escoger sin criterio entre las mismas empresas</em> (+0,5 pp a 1 año, +1,3 pp a 2 años, dentro del margen de error). Y la prueba tiene el viento a favor: las empresas del listado son las que HOY siguen en el índice. Por eso se mantiene solo «en prueba»: no es una promesa de ganar al índice.</li>
        <li><strong>Dónde está la ventaja real:</strong> el propio cribado de calidad, tomado solo, rindió algo <em>menos</em> que el listado entero (−1,7 pp a 1 año, −3,3 pp a 2 años) aunque sí batió al S&amp;P 500. Ningún paquete ha demostrado todavía que elegir aporte más que no elegir.</li>
        <li><strong>Estado:</strong> experimental. Los jueces son el «Seguimiento» (se apunta cada mes lo que habría elegido el paquete y se compara con el S&amp;P 500 y con el resto del universo) y tu marcador: dale 12-24 meses con importes que puedas permitirte, y si no supera al S&amp;P 500, quédate con el índice. Vender por precio rindió peor que el índice: aquí no se manda vender por precio; se vende, si acaso, por tesis rota.</li>
        <li><strong>Dividendos (small caps):</strong> sección aparte con empresas pequeñas que pagan dividendo y <em>no lo recortan</em> (el dinero por acción se mantiene o sube), con caja que lo cubre, poca deuda y sin diluir; las recompras suman. La rentabilidad anual es precio + dividendo y se compara con el Russell 2000. <strong>No está probada en el pasado</strong> (faltan las empresas que quebraron): el seguimiento la compara cada mes con el Russell 2000. Si una acción de este paquete que tienes recorta o deja de pagar el dividendo, la pestaña «Mi cartera» la marca como tesis rota. Tu cartera de aquí no suma los dividendos que cobres: llévalos aparte.</li>
        <li>Los análisis de tesis del bot de largo plazo y los chequeos solo pueden quitar o avisar sobre una candidata, nunca añadirla. Las empresas «solo cribado» no tienen análisis de IA de su tesis: léelas con más cautela.</li>
        <li>No incluye bancos ni aseguradoras en el paquete de 3–5 años (sus cuentas no se miden con estos ratios) ni empresas que no presenten cuentas en EE. UU. bajo US-GAAP.</li>
        <li>Información automática; no es asesoramiento financiero ni fiscal.</li>
      </ul></section>`;
  }

  // ───────────── pintado ─────────────
  function pintar() {
    if (!$m || $m.hidden) return;
    let h = "";
    if (estadoCarga === "cargando" || estadoCarga === "idle") h = `<div class="carga">Cargando el informe del mes…</div>`;
    else if (estadoCarga === "sinTabla") h = `<section class="m-card"><h2>Falta un paso</h2><p>Todavía no existe la tabla del módulo mensual en Supabase. Aplica la migración <code>db/006_mensual.sql</code> (Supabase → SQL Editor → pegar → Run) y vuelve a abrir esta pestaña.</p></section>`;
    else if (estadoCarga === "vacio") h = `<section class="m-card"><h2>Aún no hay informe</h2><p>Lanza el workflow <strong>Recomendador mensual</strong> (GitHub → Actions → Run workflow). Tarda unos minutos y aquí aparecerán las candidatas del mes.</p></section>`;
    else if (estadoCarga === "error") h = `<div class="error">No se pudo cargar el informe.<br>${esc(mensajeError)}</div>`;
    else h = (aviso ? `<div class="m-aviso">${esc(aviso)}</div>` : "") + seccionPaquetes() + seccionCartera() + seccionMarcador() + seccionSeguimiento();
    if (estadoCarga === "ok" || estadoCarga === "vacio" || estadoCarga === "sinTabla") h += seccionRegistro();
    h += seccionNotas();
    const y = window.scrollY;
    const foco = document.activeElement && document.activeElement.id;
    $m.innerHTML = h;
    window.scrollTo(0, y);
    if (foco === "m-importe") { const i = document.getElementById("m-importe"); if (i) { i.focus(); const l = i.value.length; i.setSelectionRange(l, l); } }
    prefill = null; aviso = "";
    enlazar();
    const $est = document.getElementById("estado");
    if ($est && modoActual() === "mensual") $est.textContent = informe ? "informe " + informe.mes : "mensual";
  }

  function enlazar() {
    const imp = document.getElementById("m-importe");
    if (imp) imp.addEventListener("input", (e) => { cfg.importe = e.target.value; guardar(CLAVE_CFG, cfg); pintar(); });
    const tipoImp = document.getElementById("m-imp");
    if (tipoImp) tipoImp.addEventListener("change", (e) => {
      const v = parseFloat(String(e.target.value).replace(",", "."));
      if (v >= 0 && v <= 60) { cfg.impuesto = v / 100; guardar(CLAVE_CFG, cfg); pintar(); }
    });
    const form = document.getElementById("m-form");
    if (form) form.addEventListener("submit", onGuardar);
    const fic = document.getElementById("m-importar");
    if (fic) fic.addEventListener("change", onImportar);
    $m.querySelectorAll("[data-accion]").forEach((b) => b.addEventListener("click", onAccion));
  }

  function onAccion(e) {
    const b = e.currentTarget, a = b.dataset.accion, t = b.dataset.t;
    if (a === "paquete") {
      cfg.paquete = b.dataset.paq; guardar(CLAVE_CFG, cfg); pintar();
      return;
    }
    if (a === "detalle" || a === "detallepos") {
      const k = b.dataset.k || ((a === "detalle" ? "c:" : "p:") + t);
      abiertos.has(k) ? abiertos.delete(k) : abiertos.add(k);
      const card = b.closest("article"); card.classList.toggle("abierta", abiertos.has(k));
      b.textContent = abiertos.has(k) ? (a === "detalle" ? "Ocultar detalle" : "Ocultar") : "Ver detalle";
    } else if (a === "registrar" || a === "vender") {
      prefill = { tipo: a === "vender" ? "venta" : "compra", ticker: t, precio: b.dataset.p, acciones: b.dataset.a,
                  paq: a === "registrar" ? b.dataset.paq : "" };
      pintar();
      document.getElementById("m-registro").scrollIntoView({ behavior: "smooth", block: "start" });
    } else if (a === "borrar") {
      const o = ops.find((x) => x.id === b.dataset.id);
      if (o && window.confirm(`¿Borrar ${o.tipo} de ${o.ticker} del ${o.fecha}?`)) {
        ops = ops.filter((x) => x.id !== o.id); guardar(CLAVE_OPS, ops); pintar();
      }
    } else if (a === "exportar") {
      const blob = new Blob([JSON.stringify({ version: 1, exportado: new Date().toISOString(), operaciones: ops }, null, 1)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const l = document.createElement("a");
      l.href = url; l.download = `vigia-mensual-${M.hoyISO()}.json`; document.body.appendChild(l); l.click(); l.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
    }
  }

  function onGuardar(e) {
    e.preventDefault();
    const f = new FormData(e.target);
    const dec = (s) => String(s ?? "").trim().replace(",", ".");
    const r = M.normalizarOperacion({
      tipo: f.get("tipo"), ticker: f.get("ticker"), acciones: dec(f.get("acciones")),
      precio: dec(f.get("precio")), comision: dec(f.get("comision")), fecha: f.get("fecha"),
      paq: f.get("paq"),
    });
    const msg = document.getElementById("m-form-msg");
    if (r.error) { msg.innerHTML = `<div class="m-aviso">⚠️ ${esc(r.error)}</div>`; return; }
    const prueba = M.calcular([...ops, r.op]);
    if (prueba.errores.length && !window.confirm(prueba.errores.join("\n") + "\n\n¿Guardar de todas formas?")) return;
    const avisos = r.op.tipo === "compra" ? M.avisosDosMeses(r.op.ticker, ops, r.op.fecha) : [];
    ops = [...ops, r.op]; guardar(CLAVE_OPS, ops);
    aviso = `Operación guardada: ${r.op.tipo} de ${r.op.ticker}.` + (avisos.length ? " " + avisos.join(" ") : "");
    pintar();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  async function onImportar(e) {
    const fich = e.target.files && e.target.files[0];
    if (!fich) return;
    try {
      const j = JSON.parse(await fich.text());
      const lista = Array.isArray(j) ? j : j.operaciones;
      if (!Array.isArray(lista)) throw new Error("El archivo no tiene una lista de operaciones.");
      const ids = new Set(ops.map((o) => o.id));
      let nuevas = 0, malas = 0;
      lista.forEach((x) => {
        const r = M.normalizarOperacion(x);
        if (!r.op) { malas++; return; }
        if (!ids.has(r.op.id)) { ops.push(r.op); ids.add(r.op.id); nuevas++; }
      });
      guardar(CLAVE_OPS, ops);
      aviso = `Importadas ${nuevas} operación(es) nuevas` + (malas ? `; ${malas} descartadas por datos no válidos.` : ".");
    } catch (err) { aviso = "No se pudo importar: " + ((err && err.message) || err); }
    pintar();
  }

  // ───────────── cambio de vista ─────────────
  function modoActual() { return $m && !$m.hidden ? "mensual" : "cartera"; }

  function activarModo(modo) {
    const esMensual = modo === "mensual";
    document.querySelectorAll(".modo").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.modo === modo)));
    document.getElementById("lista").hidden = esMensual;
    document.getElementById("barra-cartera").hidden = esMensual;
    $m.hidden = !esMensual;
    if (esMensual) {
      if (estadoCarga === "idle" || estadoCarga === "error") cargarInforme(); else pintar();
    } else {
      const $est = document.getElementById("estado");
      if ($est && typeof datos !== "undefined") $est.textContent = `${datos.length} empresa${datos.length === 1 ? "" : "s"}`;
    }
    window.scrollTo(0, 0);
  }

  cargarLocal();
  document.querySelectorAll(".modo").forEach((b) => b.addEventListener("click", () => activarModo(b.dataset.modo)));
  if (location.hash === "#mensual") activarModo("mensual");
})();
