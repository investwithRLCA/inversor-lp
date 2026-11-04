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
  let cfg = { impuesto: 0.21, importe: "", paquete: "lynch_p" };
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

  const NOMBRE_PAQ = { corto: "1–3 años", medio: "3–5 años", largo: "5+ años", lynch_g: "Lynch grandes", lynch_p: "Pequeñas (Lynch)", calidad_g: "Grandes (calidad)", dividendos: "Dividendos" };
  // Paquetes ACTIVOS (los que se enseñan, se miden y se pueden elegir). Los retirados no superaron
  // la prueba histórica: solo se conservan sus nombres para las operaciones antiguas de la cartera.
  const PAQS_ORDEN = ["lynch_p", "calidad_g", "dividendos"];
  const CON_IWM = ["dividendos", "lynch_p"];      // se comparan con el Russell 2000
  const TIPO_LYNCH = { rapida: "crecimiento rápido", solida: "sólida", lenta: "lenta con dividendo" };
  const PAQS_RETIRADOS = ["corto", "medio", "largo", "lynch_g"];
  // La autocorrección puede reactivar un paquete retirado (p. ej. Lynch en grandes) si la revalidación mensual lo aprueba.
  const acActivos = () => ((informe && informe.autocorreccion && informe.autocorreccion.activos) || []);
  const retirado = (k) => PAQS_RETIRADOS.includes(k) && !acActivos().includes(k);
  const ordenPaqs = () => [...PAQS_ORDEN.slice(0, -1), ...acActivos().filter((k) => !PAQS_ORDEN.includes(k)), PAQS_ORDEN[PAQS_ORDEN.length - 1]];
  const nombrePaq = (k) => (retirado(k) ? NOMBRE_PAQ[k] + " (retirado)" : NOMBRE_PAQ[k] || k);
  const GRUPO_PAQ = { lynch_p: "pequenas", calidad_g: "grandes", lynch_g: "grandes" };
  const NOMBRE_GRUPO = { pequenas: "Pequeñas", grandes: "Grandes" };
  const INDICE_GRUPO = { pequenas: "Russell 2000", grandes: "S&P 500" };
  const NOMBRE_VARIANTE = { lynch_cribado: "Lynch + calidad", lynch: "Lynch (sin cribado de calidad)", calidad: "las de mejor nota de calidad",
                            calidad_precio: "calidad + más baratas", magica: "fórmula mágica de Greenblatt" };
  const VARIANTE_INICIAL = { pequenas: "lynch_cribado", grandes: "calidad" };

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
    if (k.ev_ebit != null && k.ev_ebit > 0) p.push(`valor/beneficio operativo ${num(k.ev_ebit, 0)}x`);
    return p.join(" · ");
  }

  /** Cambio de veredicto en el último mes (verde si sube, naranja si baja); helpers de index.html. */
  const cambioDe = (antes, ahora, fechaISO) => (window.VigiaCambio ? window.VigiaCambio.cambioVeredicto(antes, ahora, fechaISO) : null);
  const etiquetaCambio = (cv) => (window.VigiaCambio && cv ? window.VigiaCambio.etiquetaCambio(cv) : "");

  function tarjetaCandidata(c, paquete, reparto, valorPos, totalTras, hayImporte) {
    const comp = reparto.compras[c.ticker] || 0;
    const dosMeses = M.avisosDosMeses(c.ticker, ops, M.hoyISO());
    const ia = c.ia && !c.ia.error ? c.ia : null;
    const clave = `c:${paquete}:${c.ticker}`;
    const abierta = abiertos.has(clave);
    const avisos = (c.avisos || []).concat(dosMeses);
    const esCalidad = paquete !== "corto";
    const esDiv = paquete === "dividendos";
    const esLynch = String(paquete).startsWith("lynch");
    const ly = c.lynch || {};
    const dv = c.dividendo || {}, emp = c.empresa || {};
    const r5 = (c.rend || {})["5a"] || (c.rend || {})["3a"];
    const nr5 = (c.rend || {})["5a"] ? "5 años" : "3 años";
    const cagrDiv = dv.cagr_5a != null ? dv.cagr_5a : dv.cagr_3a;
    const gr = c.grandes || {};
    const subPrecio = esDiv ? (c.cap != null ? `cap. ${num(c.cap, 0)} M$` : "")
      : esLynch ? (ly.cap != null ? `cap. ${num(ly.cap, 0)} M$` : "")
      : paquete === "calidad_g" ? (gr.cap != null ? `cap. ${num(gr.cap, 0)} M$` : "")
      : (c.descuento != null ? `${signo(-c.descuento * 100, 0)} % vs su mediana 5a` : "");
    const cv = cambioDe(c.v_ant, c.veredicto, c.v_cambio);
    return `<article class="m-cand ${abierta ? "abierta" : ""} ${cv ? "cambio-" + cv.tipo : ""}" data-t="${esc(c.ticker)}">
      <div class="m-cab">
        <div><div class="m-tk">${esc(c.ticker)} ${c.doble_descuento ? '<span title="Por debajo del precio de entrada de la tesis">⭐</span>' : ""}
            ${esDiv ? chip(num(dv.rentab_div, 1) + " % dividendo", "m-bien")
              : esLynch && !tieneTesis(c) ? `<span class="m-chip" title="Solo ha pasado el filtro Lynch (los números); aún sin análisis de IA">sin IA aún</span>` : pillNivel(c)}</div>
            <div class="m-nombre">${esc(c.nombre)}${c.sector ? " · " + esc(c.sector) : ""}</div>${etiquetaCambio(cv)}</div>
        <div class="m-precio num">${num(c.precio)}<small>${esCalidad && c.puntos != null ? "puntos " + num(c.puntos, 0) + " · " : ""}${esc(subPrecio)}</small></div>
      </div>
      <div class="m-compra">${hayImporte
        ? (comp > 0
          ? `Comprar <strong class="num">${usd(comp)}</strong> <span class="m-dim">≈ ${num(redondeaAbajo(comp / c.precio, 2), 2)} acc. · quedaría en el ${pct((((valorPos[c.ticker] || 0) + comp) / totalTras))} de tu cartera</span>`
          : `<span class="m-dim">Ya pesa el tope en tu cartera: no añadas más.</span>`)
        : `<span class="m-dim">Reparto orientativo: ${pct(c.peso)} del importe</span>`}</div>
      ${esCalidad && c.clave ? `<div class="m-dim m-linea">${esc(lineaClave(c.clave))}</div>` : ""}
      ${esLynch ? `<div class="m-dim m-linea">PER ${num(ly.per, 0)} · beneficio por acción ${signo(ly.crec, 0)} %/año${ly.div ? " · dividendo " + num(ly.div, 1) + " %" : ""}${ly.de != null ? " · deuda/patrimonio " + num(ly.de * 100, 0) + " %" : ""}${ly.caja != null && ly.caja > 0 ? " · caja neta " + num(ly.caja, 0) + " % de lo que vale" : ""}</div>` : ""}
      ${esDiv ? `<div class="m-dim m-linea">Dividendo ${usd(dv.dps)} por acción al año · ${num(dv.anios_sin_recorte, 0)} años sin recorte${cagrDiv != null ? " · crece " + signo(cagrDiv, 1) + " %/año" : ""}${emp.payout != null ? " · reparte el " + num(emp.payout, 0) + " % de su " + (emp.financiera ? "beneficio/caja" : "caja libre") : ""}${emp.dilucion_5a != null ? " · acciones 5a " + signo(emp.dilucion_5a, 0) + " %" : ""}</div>` : ""}
      <div class="m-chips">
        ${esCalidad && c.calidad != null ? chip("calidad " + num(c.calidad, 0)) + chip("crecimiento " + num(c.crecimiento, 0)) + chip("precio " + num(c.valoracion, 0)) : ""}
        ${esLynch ? chip(TIPO_LYNCH[ly.cat] || "—") + chip("PEG " + num(ly.peg_usado, 2), ly.peg_usado != null && ly.peg_usado <= 0.5 ? "m-bien" : "") : ""}
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
          : esLynch ? `<p class="m-dim">Sin tesis de IA todavía: el bot la analizará en los próximos días.</p>`
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

  // ───────────── tarta de Trading 212 (vía la app Atajos del iPhone) ─────────────
  const URL_T212 = "https://live.trading212.com/api/v0/equity/pies";
  const mesCorto = (mes) => /^\d{4}-\d{2}$/.test(mes || "")
    ? new Date(mes + "-01T00:00:00").toLocaleDateString("es-ES", { month: "short", year: "numeric" }).replace(".", "") : (mes || "");

  /** Lo que lleva la tarta de esta sección: {pesos, cuerpo, enlace, total}. Sin importe, el reparto orientativo. */
  function tartaDe(sel, P, cands, reparto, hayImporte) {
    const base = {};
    // sin importe: el reparto orientativo del informe (a partes iguales si no lo trae, como en Dividendos)
    cands.forEach((c) => (base[c.ticker] = hayImporte ? (reparto.compras[c.ticker] || 0) : (c.peso > 0 ? c.peso : 1)));
    const fuera = (cfg.t212Fuera || []).filter((t) => t in base);
    const pesos = M.pesosTarta(base, fuera);
    const nombre = `Vigía ${P.etiqueta || NOMBRE_PAQ[sel] || sel} ${mesCorto(informe.mes)}`;
    const cuerpo = M.cuerpoTarta(nombre, pesos, cfg.t212Div);
    const total = Object.entries(base).filter(([t]) => pesos[t]).reduce((s, [, v]) => s + v, 0);
    return { pesos, cuerpo, nombre, enlace: M.enlaceAtajoT212(cuerpo), total, fuera: new Set(fuera) };
  }

  function bloqueT212(sel, P, cands, reparto, hayImporte) {
    const T = tartaDe(sel, P, cands, reparto, hayImporte);
    const n = Object.keys(T.pesos).length;
    const filas = cands.map((c) => {
      const w = T.pesos[c.ticker], quitada = T.fuera.has(c.ticker);
      const tope = hayImporte && !(reparto.compras[c.ticker] > 0);
      const casilla = tope ? "" : `<input type="checkbox" data-accion="t212fuera" data-t="${esc(c.ticker)}" ${quitada ? "" : "checked"} aria-label="Incluir ${esc(c.ticker)}">`;
      return `<tr><td><label class="m-check">${casilla}<strong>${esc(c.ticker)}</strong></label></td>
        <td class="num">${w ? pct(w, 1) : "—"}</td>
        <td class="m-dim">${quitada ? "quitada" : tope ? "ya pesa el tope en tu cartera" : esc(M.tickerT212(c.ticker))}</td></tr>`;
    }).join("");
    const pasos = `<ol class="m-pasos">
        <li><strong>Clave de Trading 212.</strong> En Trading 212: Ajustes → <em>API (Beta)</em> → generar una clave nueva. En los permisos marca <strong>solo los de «Pies»</strong> (así la clave solo puede crear tartas: no puede comprar, vender ni sacar dinero). Te da una <em>clave</em> y un <em>secreto</em>: cópialos (el secreto solo se ve una vez).</li>
        <li><strong>Atajo nuevo.</strong> Abre la app <em>Atajos</em> → «+» → ponle de nombre exactamente <code>${esc(M.NOMBRE_ATAJO_T212)}</code>.</li>
        <li>Añade la acción <strong>Texto</strong> y escribe tu clave, dos puntos y tu secreto, todo junto: <code>CLAVE:SECRETO</code>.</li>
        <li>Añade la acción de <strong>Base64</strong> (escribe «Base64» en el buscador de acciones; es la de <em>codificar</em>). Toca la flecha y pon <em>Saltos de línea</em>: <strong>Ninguno</strong> (importante).</li>
        <li>Añade la acción <strong>Obtener contenido de URL</strong> (búscala por «contenido»). URL: <code>${esc(URL_T212)}</code>. Toca la flecha:
          <ul><li><em>Método</em>: <strong>POST</strong>.</li>
              <li><em>Encabezados</em> → añadir dos: <code>Authorization</code> con el texto <code>Basic </code> (con un espacio detrás) seguido de la variable que sale del paso de Base64 (tócala en la barra de variables); y <code>Content-Type</code> con <code>application/json</code>.</li>
              <li><em>Cuerpo de la solicitud</em>: <strong>Archivo</strong> → elige la variable <em>Entrada del atajo</em>.</li></ul></li>
        <li>Añade la acción <strong>Mostrar resultado</strong>, con la variable que sale del paso anterior (la respuesta de Trading 212). Listo.</li>
      </ol>
      <p class="m-dim">Tu clave queda solo dentro de ese atajo, en tu iPhone: Vigía no la ve ni la guarda, y no pasa por GitHub ni por Supabase. Si algún día quieres cortar el acceso, borra la clave en Trading 212.</p>`;
    const totalTxt = hayImporte && T.total > 0 ? `<p class="m-dim">Con tu importe: ${usd(T.total)} repartidos así. En Trading 212 metes el dinero en la tarta (en euros si tu cuenta es en euros) y ella lo reparte con estos porcentajes.</p>` : "";
    return `<details class="m-t212" data-k="t212" ${abiertos.has("t212") ? "open" : ""}>
      <summary><strong>Crear esta tarta en Trading 212</strong> <span class="m-dim">(${n} empresa${n === 1 ? "" : "s"})</span></summary>
      <p class="m-dim">Crea en tu Trading 212 una tarta (pie) con las empresas de esta sección y estos porcentajes${hayImporte ? "" : " (los orientativos; si escribes arriba cuánto vas a invertir, tienen en cuenta lo que ya tienes)"}. <strong>No compra nada:</strong> la creas, la revisas en Trading 212 y, cuando quieras, metes el dinero y ella lo reparte. Quita (desmarca) las que Trading 212 no tenga: se recordará.</p>
      ${totalTxt}
      <table class="m-tabla"><tr><th>Empresa</th><th>En la tarta</th><th>Ticker en Trading 212</th></tr>${filas}</table>
      <p class="m-dim">Nombre de la tarta: <strong>${esc(T.cuerpo.name)}</strong></p>
      <div>
        <label class="m-campo">Dividendos<select id="m-t212-div">
          <option value="REINVEST" ${T.cuerpo.dividendCashAction === "REINVEST" ? "selected" : ""}>Reinvertir en la tarta</option>
          <option value="TO_ACCOUNT_CASH" ${T.cuerpo.dividendCashAction === "TO_ACCOUNT_CASH" ? "selected" : ""}>Al efectivo de la cuenta</option></select></label>
      </div>
      <div class="m-acciones">
        ${n ? `<a class="m-btn m-btn-ppal" href="${esc(T.enlace)}">Crear la tarta en Trading 212</a>` : `<span class="m-dim">No queda ninguna empresa en la tarta.</span>`}
        ${n && hayImporte ? `<button class="m-btn" data-accion="t212apuntar">Apuntar estas ${n} compras en mi cartera</button>` : ""}
      </div>
      <p class="m-nota">Funciona en iPhone o iPad con la app Atajos (abajo, cómo prepararlo una vez). Al pulsar, se abre el atajo, envía la tarta y te enseña la respuesta de Trading 212. Si le das dos veces, se crean dos tartas: borra la que sobre. Si Trading 212 contesta con un error, haz una captura y pásamela.</p>
      <details class="m-det" data-k="t212-config" ${abiertos.has("t212-config") ? "open" : ""}><summary>Preparar el atajo del iPhone (solo la primera vez)</summary>${pasos}</details>
      <details class="m-det" data-k="t212-json" ${abiertos.has("t212-json") ? "open" : ""}><summary>Ver lo que se envía</summary><pre class="m-pre">${esc(JSON.stringify(T.cuerpo, null, 1))}</pre></details>
    </details>`;
  }

  function seccionPaquetes() {
    const inf = informe;
    const paqs = paquetesDe(inf);
    let claves = ordenPaqs().filter((k) => paqs[k]);
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
    const ac = inf.autocorreccion || null;
    const grupoSel = GRUPO_PAQ[sel], valSel = ac && grupoSel && ((ac.validacion || {}).grupos || {})[grupoSel];
    if (retirado(sel)) {
      h += `<div class="m-aviso"><strong>Paquete retirado:</strong> no superó la prueba histórica. Sale porque este informe es anterior al cambio: lanza el «Recomendador mensual» (GitHub → Actions) para tener el informe nuevo. No compres por él.</div>`;
    } else if (sel === "dividendos") {
      h += `<div class="m-aviso">Esta sección <strong>no se ha podido probar en el pasado</strong> (faltan las empresas que quebraron o salieron del índice, y las cifras históricas de abajo son de supervivientes). Mira «Seguimiento» más abajo antes de poner dinero serio. Ojo fiscal: los dividendos tributan cada año como ahorro (en EE. UU. retienen un 15 % que normalmente puedes descontar), la revalorización solo al vender.</div>`;
    } else if (valSel && valSel.variante && valSel.variante !== VARIANTE_INICIAL[grupoSel]) {
      h += `<div class="m-aviso">La <strong>autocorrección</strong> ha cambiado el filtro de esta sección a <strong>${esc(NOMBRE_VARIANTE[valSel.variante] || valSel.variante)}</strong>: con los datos al día es el que mejor bate al ${esc(INDICE_GRUPO[grupoSel])} en la prueba histórica (${signo((valSel.ex12 ?? NaN) * 100, 1)} puntos a 1 año, ${signo((valSel.ex24 ?? NaN) * 100, 1)} a 2 años). Faltan las empresas que quebraron: el juez es «Seguimiento», más abajo.</div>`;
    } else if (sel === "calidad_g") {
      h += `<div class="m-aviso">Este paquete <strong>sí se ha probado en el pasado</strong> (cuentas tal como se conocían en cada fecha, 2015-2025, ~670 empresas grandes de hoy): las 15 de mejor nota de calidad rindieron <strong>+2,4 puntos más que el S&amp;P 500</strong> a 1 año, +6,3 a 2 años y +5,7 a 3 años, y más que las grandes elegidas sin criterio. Acertó el 48 % de las compras a 1 año (sin criterio, 44 %), pero las que aciertan ganan más de lo que pierden las que fallan. Ventaja modesta: en 2015-2019 quedó empatado con el índice. Faltan las que quebraron: el juez es «Seguimiento», más abajo.</div>`;
    } else if (sel === "lynch_p") {
      h += `<div class="m-aviso">Este paquete <strong>sí se ha probado en el pasado</strong> (cuentas tal como se conocían en cada fecha, 2015-2025, ~1.100 empresas pequeñas de hoy): rindió <strong>+5,9 puntos al año más que el Russell 2000</strong> a 1 año y +14,9 a 2 años, y algo más que elegir sin criterio entre las mismas pequeñas (+1,7 a 1 año, dentro del margen de error; +5,3 a 2 años). Acertó (batió al Russell 2000) el 54 % de las compras a 1 año y el 60 % a 3 años, frente a un 47-48 % sin criterio. Faltan las empresas que quebraron, así que la realidad será algo peor: el juez es «Seguimiento», más abajo.</div>`;
    }
    if (valSel && valSel.activo && valSel.ex24 != null && ac.validacion.actualizado) {
      h += `<p class="m-nota">Revalidado el ${esc(fecha(ac.validacion.actualizado))} con las cuentas y precios al día: ${signo(valSel.ex12 * 100, 1)} puntos a 1 año y ${signo(valSel.ex24 * 100, 1)} a 2 años frente al ${esc(INDICE_GRUPO[grupoSel])}. Se repite cada mes (ver «Autocorrección»).</p>`;
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
      if (!retirado(sel)) h += bloqueT212(sel, P, cands, reparto, hayImporte);
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
    const cohortes = ((seg && seg.cohortes) || []).filter((c) => ordenPaqs().includes(c.paquete));
    if (!seg || !cohortes.length) {
      h += `<p class="m-vacio">Aún no hay historial: la primera cifra aparece cuando haya un informe anterior con al menos unos días de recorrido, y es fiable de verdad a partir de 12-24 meses.</p>`;
      return `<section class="m-card">${h}</section>`;
    }
    const fila = (k) => {
      const s = (seg.resumen || {})[k]; if (!s || !s.cohortes) return "";
      const nom = CON_IWM.includes(k) ? `${esc(nombrePaq(k))} <small class="m-dim">vs Russell 2000</small>` : nombrePaq(k);
      if (!s.cohortes_maduras) return `<tr><td>${nom}</td><td colspan="3" class="m-dim">${s.cohortes} cohorte(s), aún sin 3 meses</td></tr>`;
      const cl = s.exceso_medio >= 0 ? "m-pos" : "m-neg";
      return `<tr><td>${nom}</td><td class="num">${s.cohortes_maduras}</td>
        <td class="num"><span class="${cl}">${signo(s.exceso_medio * 100, 1)} pp</span></td>
        <td class="num">${s.superan_spy}/${s.cohortes_maduras}</td></tr>`;
    };
    h += `<table class="m-tabla"><tr><th></th><th>Cohortes ≥3 m</th><th>Exceso vs referencia</th><th>Superan</th></tr>${ordenPaqs().map(fila).join("")}</table>`;
    const conPool = ordenPaqs().filter((k) => seg.resumen && seg.resumen[k] && seg.resumen[k].exceso_medio_pool != null);
    if (conPool.length) {
      h += `<p class="m-dim">Frente al resto del universo: ${conPool.map((k) => `${nombrePaq(k)} ${signo(seg.resumen[k].exceso_medio_pool * 100, 1)} pp`).join(" · ")}. Si los paquetes baten al S&amp;P pero no al resto del universo, lo que funciona es el filtro de calidad, no la selección.</p>`;
    }
    const ta = ordenPaqs().map((k) => (seg.resumen || {})[k]).filter((s) => s && s.tier_a && s.tier_b);
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

  function seccionAutocorreccion() {
    const ac = informe && informe.autocorreccion;
    let h = `<h2>Autocorrección <span class="m-sub">se ajusta sola, con reglas fijadas de antemano</span></h2>
      <p class="m-dim">Según se acumulan datos, el sistema corrige su método solo. Para no dar bandazos por una racha de dos meses, cada ajuste exige un mínimo de datos y una diferencia clara (intervalo de confianza al 90 %). Las reglas están escritas en <code>backend/mensual/autocorreccion.py</code> y no se tocan a la vista de los resultados.</p>`;
    if (!ac) {
      h += `<p class="m-vacio">Se activa con el próximo informe mensual.</p>`;
      return `<section class="m-card m-autocorr">${h}</section>`;
    }
    const val = ac.validacion || {}, grupos = val.grupos || {};
    const pp = (x) => (x == null ? "—" : signo(x * 100, 1) + " pp");
    // 1) revalidación histórica
    h += `<h3>1 · Revalidación histórica <span class="m-sub">cada mes</span></h3>
      <p class="m-dim">La prueba del pasado se repite cada mes con las cuentas y los precios al día. Si el filtro en uso deja de batir al índice, se cambia a otro de los probados que sí lo haga; si ninguno lo hace, esa sección se pone en pausa. Si otro le saca al menos ${num(ac.reglas && ac.reglas.margen_cambio_pp, 0)} puntos a 2 años, se cambia a ese.</p>`;
    const gs = Object.keys(NOMBRE_GRUPO).filter((g) => grupos[g]);
    if (gs.length) {
      h += `<table class="m-tabla"><tr><th></th><th>Filtro en uso</th><th>1 año</th><th>2 años</th></tr>${gs.map((g) => {
        const x = grupos[g];
        const estado = x.activo ? `<span class="m-chip m-bien">en marcha</span>` : `<span class="m-chip m-mal">en pausa</span>`;
        return `<tr><td>${esc(NOMBRE_GRUPO[g])}<br><small class="m-dim">vs ${esc(INDICE_GRUPO[g])}</small></td>
          <td>${esc(NOMBRE_VARIANTE[x.variante] || x.variante || "—")} ${estado}${x.motivo && x.motivo !== "sigue aprobando" ? `<br><small class="m-dim">${esc(x.motivo)}</small>` : ""}</td>
          <td class="num"><span class="${(x.ex12 ?? 0) >= 0 ? "m-pos" : "m-neg"}">${pp(x.ex12)}</span></td>
          <td class="num"><span class="${(x.ex24 ?? 0) >= 0 ? "m-pos" : "m-neg"}">${pp(x.ex24)}</span></td></tr>`;
      }).join("")}</table>
      <p class="m-nota">Última revalidación: ${esc(fecha(val.actualizado))}. Puntos por encima (o por debajo) del índice en la prueba histórica.</p>`;
    } else {
      h += `<p class="m-vacio">Aún no hay revalidación guardada: la primera llega con la próxima «Prueba histórica de los filtros» (se lanza sola entre el 24 y el 28 de cada mes).</p>`;
    }
    const hist = (val.historial || []).slice().reverse();
    if (hist.length) {
      h += `<details class="m-det"><summary>Cambios hechos por la autocorrección (${hist.length})</summary><ul>${hist.map((c) =>
        `<li>${esc(fecha(c.fecha))} · ${esc(NOMBRE_GRUPO[c.grupo] || c.grupo)}: ${c.activo ? `de «${esc(NOMBRE_VARIANTE[c.de] || c.de || "—")}» a «${esc(NOMBRE_VARIANTE[c.a] || c.a)}»` : "en pausa"} — ${esc(c.motivo)}</li>`).join("")}</ul></details>`;
    }
    // 2) freno por resultados reales
    const frenos = ac.frenos || {};
    const ks = ordenPaqs().filter((k) => frenos[k] && (acActivos().includes(k) || frenos[k].pausa));
    h += `<h3>2 · Freno por resultados reales</h3>
      <p class="m-dim">Si lo que elige una sección rinde menos que su índice de forma clara, con al menos ${frenos[ks[0]] ? frenos[ks[0]].necesarias : 12} meses de elecciones con más de un año de recorrido, la sección se pone en pausa hasta que vuelva a ir por delante.</p>`;
    h += ks.length ? `<ul class="m-ac-lista">${ks.map((k) => {
      const f = frenos[k];
      const txt = f.pausa ? `<span class="m-chip m-mal">en pausa</span> ${pp(f.exceso_medio)} de media`
        : f.estado === "en_marcha" ? `<span class="m-chip m-bien">en marcha</span> ${pp(f.exceso_medio)} de media`
        : `acumulando datos: <strong>${f.n} de ${f.necesarias}</strong> meses con más de un año${f.exceso_medio != null ? ` (de momento ${pp(f.exceso_medio)})` : ""}`;
      return `<li><strong>${esc(nombrePaq(k))}</strong>: ${txt}</li>`;
    }).join("")}</ul>` : "";
    // 3) VIGILAR y sin IA
    const vs = ac.veredictos || {};
    h += `<h3>3 · ¿Hay que hacer caso a la IA?</h3>
      <p class="m-dim">Hoy el reparto incluye también las VIGILAR y las «sin IA aún» (la prueba histórica no usaba la IA). Se mide cuánto rinden frente a las COMPRA de la misma sección; si rinden menos de forma clara durante ${vs.VIGILAR ? vs.VIGILAR.necesarias : 24} meses de elecciones con más de un año, dejan de recibir dinero.</p>`;
    h += `<ul class="m-ac-lista">${[["VIGILAR", "VIGILAR"], ["SIN_TESIS", "«sin IA aún»"]].filter(([k]) => vs[k]).map(([k, nom]) => {
      const r = vs[k];
      const txt = r.excluir ? `<span class="m-chip m-mal">fuera del reparto</span> ${pp(r.diferencia_media)} frente a las COMPRA`
        : r.estado === "dentro" ? `<span class="m-chip m-bien">siguen dentro</span> ${pp(r.diferencia_media)} frente a las COMPRA`
        : `acumulando datos: <strong>${r.n} de ${r.necesarias}</strong> meses${r.diferencia_media != null ? ` (de momento ${pp(r.diferencia_media)} frente a las COMPRA)` : ""}`;
      return `<li><strong>${nom}</strong>: ${txt}</li>`;
    }).join("")}</ul>`;
    if ((ac.notas || []).length) h += ac.notas.map((n) => `<div class="m-aviso">${esc(n)}</div>`).join("");
    else h += `<p class="m-nota">Este mes la autocorrección no ha cambiado nada.</p>`;
    return `<section class="m-card m-autocorr">${h}</section>`;
  }

  function seccionCartera() {
    const { calc, val } = valoresPos();
    let h = `<h2>Mi cartera</h2>`;
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
      h += `<table class="m-tabla"><tr><th>Por paquete</th><th>Valor</th><th>Coste</th><th>Resultado</th></tr>${[...ordenPaqs(), ...PAQS_RETIRADOS.filter((k) => !ordenPaqs().includes(k)), "sin"].filter((k) => porPaq[k]).map((k) => {
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
      const ep = (informe && informe.empresas && informe.empresas[f.ticker]) || {};
      const cvp = cambioDe(ep.va, ep.v, ep.vc);
      h += `<article class="m-posicion ${abierta ? "abierta" : ""} ${cvp ? "cambio-" + cvp.tipo : ""}">
        <div class="m-cab">
          <div><div class="m-tk">${esc(f.ticker)} ${chip(etiqueta, clase)}${f.paq ? " " + chip(nombrePaq(f.paq)) : ""}</div><div class="m-nombre">${esc(f.nombre)}</div>${etiquetaCambio(cvp)}</div>
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
      <tr><td>Tu cartera</td><td class="num">${usd(mk.usuario.valor, 0)}</td><td class="num">${signo(mk.usuario.ganancia, 0)} $</td><td class="num">${mk.anualizable ? pct(mk.usuario.xirr, 1) : "—"}</td></tr>
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
          <select name="paq"><option value="">—</option>${[...ordenPaqs(), ...PAQS_RETIRADOS.filter((k) => p.paq === k && !ordenPaqs().includes(k))].map((k) => `<option value="${k}" ${p.paq === k ? "selected" : ""}>${esc(nombrePaq(k))}</option>`).join("")}</select></label>
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
        <li><strong>Qué hay:</strong> tres secciones, por tamaño de empresa (no por años: todo es para largo plazo). <em>Pequeñas</em> (300 a 5.000 M$): las que cumplen las reglas de Peter Lynch («One Up on Wall Street») y además el cribado de calidad del bot. <em>Grandes</em> (más de 5.000 M$): las 15 con mejor nota del cribado de calidad. Y <em>Dividendos</em>: small caps que pagan dividendo sin recortarlo.</li>
        <li><strong>Las reglas de Lynch:</strong> beneficio por acción que crece (20–50 %/año las «rápidas»; 10–20 % las «sólidas» grandes) sin años de pérdidas; PEG ≤ 1 (PER dividido entre el crecimiento: no pagar más de lo que crece; 0,5 es una ganga); deuda/patrimonio ≤ 80 %; inventario que no crece más que las ventas. Lo que Lynch hacía además (hablar con la empresa, conocer el producto, compras de directivos) no está en los números: mira qué hace cada empresa antes de comprar.</li>
        <li><strong>El cribado de calidad:</strong> rentabilidad sobre el capital ≥ 8 %, caja libre positiva, ingresos que no caen, deuda e intereses bajo control, sin diluir al accionista y un PER no disparatado; con todo eso, una nota de 0 a 100.</li>
        <li><strong>Lo que dijo la prueba histórica</strong> (2015-2025, cuentas tal como se conocían en cada fecha). <em>Pequeñas</em>: Lynch + calidad batió al Russell 2000 (+5,9 puntos a 1 año, +14,9 a 2 años) y acertó el 54-61 % de las veces (sin criterio, 47-48 %); a 3 años duplicó el 22 % de las compras y solo el 2 % perdió más de la mitad. <em>Grandes</em>: la calidad batió al S&amp;P 500 (+2,4 a 1 año, +6,3 a 2 años), con ventaja modesta. Lynch en grandes, «calidad + más baratas» y la «fórmula mágica» de Greenblatt NO batieron al S&amp;P 500, así que no se usan.</li>
        <li><strong>Qué se ha quitado:</strong> los paquetes por años (1–3, 3–5 y 5+). Ninguno batió a elegir sin criterio entre las mismas empresas. El de 3–5 años parecía aprobar por poco, pero la prueba tenía un fallo (precios antiguos ajustados por splits que hacían parecer baratas a las futuras ganadoras); corregido, suspende (−1,7 puntos a 1 año). Las compras que ya tengas anotadas con ellos se conservan, marcadas «retirado».</li>
        <li><strong>Cuánto fiarse:</strong> a la prueba le faltan las empresas que quebraron (sesgo a favor), y el pasado no se repite. Es experimental: el juez es el «Seguimiento» (pequeñas frente al Russell 2000, grandes frente al S&amp;P 500) y tu marcador frente al S&amp;P 500. Dale 12-24 meses con importes que puedas permitirte. Las pequeñas se mueven más que las grandes: espera años malos. Vender por precio rindió peor que el índice: aquí no se manda vender por precio, sino si la empresa deja de cumplir las reglas (tu cartera te avisa).</li>
        <li><strong>Dividendos (small caps):</strong> empresas pequeñas del Russell 2000 que pagan dividendo y <em>no lo recortan</em>, con caja que lo cubre, poca deuda y sin diluir. La rentabilidad anual es precio + dividendo y se compara con el Russell 2000. <strong>No está probada en el pasado</strong> (faltan las que quebraron). Si una que tienes recorta o deja de pagar el dividendo, «Mi cartera» la marca como tesis rota. Tu cartera de aquí no suma los dividendos que cobres: llévalos aparte.</li>
        <li><strong>Autocorrección:</strong> cada mes se repite la prueba histórica con los datos al día y, si el filtro de una sección deja de batir a su índice, se cambia o se pone en pausa. Con el tiempo, además, los resultados reales pueden pausar una sección o dejar fuera del reparto a las VIGILAR, pero solo con 1-2 años de datos y una diferencia clara. Todo queda a la vista en la tarjeta «Autocorrección».</li>
        <li>Los análisis de tesis del bot y los chequeos solo pueden quitar o avisar sobre una candidata, nunca añadirla. Las marcadas «sin IA aún» solo han pasado los números.</li>
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
    else h = (aviso ? `<div class="m-aviso">${esc(aviso)}</div>` : "") + seccionPaquetes() + seccionCartera() + seccionMarcador() + seccionSeguimiento() + seccionAutocorreccion();
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
    const div = document.getElementById("m-t212-div");
    if (div) div.addEventListener("change", (e) => { cfg.t212Div = e.target.value; guardar(CLAVE_CFG, cfg); pintar(); });
    $m.querySelectorAll("details[data-k]").forEach((d) => d.addEventListener("toggle", () => (d.open ? abiertos.add(d.dataset.k) : abiertos.delete(d.dataset.k))));
  }

  /** Apunta en la cartera las compras de la tarta (al precio del informe), tras confirmar. */
  function apuntarTarta() {
    const paqs = paquetesDe(informe), sel = cfg.paquete, P = paqs[sel];
    if (!P) return;
    const importe = parseFloat(String(cfg.importe).replace(",", "."));
    if (!(importe > 0)) return;
    const cands = P.candidatas || [];
    const { valorPos } = valoresPos();
    const reparto = M.repartir(importe, cands.map((c) => c.ticker), valorPos, informe.parametros.tope_posicion);
    const T = tartaDe(sel, P, cands, reparto, true);
    const precios = {};
    cands.forEach((c) => (precios[c.ticker] = c.precio));
    const nuevas = M.comprasDeTarta(T.pesos, T.total, precios, M.hoyISO(), sel).map((o) => M.normalizarOperacion(o).op).filter(Boolean);
    if (!nuevas.length) return;
    const txt = nuevas.map((o) => `${o.ticker}: ${num(o.acciones, 4)} acc. × ${num(o.precio)} $`).join("\n");
    if (!window.confirm(`Se apuntarán ${nuevas.length} compras por ${usd(T.total)} en total, al precio del informe (${fecha(informe.datos_a)}):\n\n${txt}\n\nEl precio real en Trading 212 será algo distinto. Apúntalas solo cuando hayas metido el dinero en la tarta. ¿Seguir?`)) return;
    ops = [...ops, ...nuevas]; guardar(CLAVE_OPS, ops);
    aviso = `Apuntadas ${nuevas.length} compras de la tarta (${nuevas.map((o) => o.ticker).join(", ")}). Si alguna se ejecutó a otro precio, bórrala abajo y apúntala a mano.`;
    pintar();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function onAccion(e) {
    const b = e.currentTarget, a = b.dataset.accion, t = b.dataset.t;
    if (a === "paquete") {
      cfg.paquete = b.dataset.paq; guardar(CLAVE_CFG, cfg); pintar();
      return;
    }
    if (a === "t212fuera") {
      const s = new Set(cfg.t212Fuera || []);
      b.checked ? s.delete(t) : s.add(t);
      cfg.t212Fuera = [...s]; guardar(CLAVE_CFG, cfg); pintar();
      return;
    }
    if (a === "t212apuntar") { apuntarTarta(); return; }
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
  // «Este mes» es la vista principal (la de la aportación mensual); «Fichas» (#fichas o #cartera) es la consulta.
  activarModo(location.hash === "#fichas" || location.hash === "#cartera" ? "cartera" : "mensual");
})();
