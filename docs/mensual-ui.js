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
  let cfg = { impuesto: 0.21, importe: "" };
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

  function seccionEsteMes() {
    const inf = informe;
    const importe = parseFloat(String(cfg.importe).replace(",", "."));
    const hayImporte = importe > 0;
    const { valorPos } = valoresPos();
    const cands = inf.candidatas || [];
    let reparto = { compras: {}, sobrante: 0 };
    if (hayImporte && cands.length) {
      reparto = M.repartir(importe, cands.map((c) => c.ticker), valorPos, inf.parametros.tope_posicion);
    }
    const totalTras = Object.values(valorPos).reduce((s, v) => s + v, 0) + Object.values(reparto.compras).reduce((s, v) => s + v, 0);

    let h = `<h2>Este mes <span class="m-sub">${esc(inf.mes)} · precios a ${esc(fecha(inf.datos_a))}</span></h2>`;
    (inf.avisos || []).forEach((a) => (h += `<div class="m-aviso">⚠️ ${esc(a)}</div>`));
    h += `<label class="m-campo">¿Cuánto vas a invertir este mes? ($)
            <input id="m-importe" type="text" inputmode="decimal" placeholder="p. ej. 500" value="${esc(cfg.importe)}" autocomplete="off"></label>`;

    if (!cands.length) {
      h += `<p class="m-vacio"><strong>Este mes no hay nada claramente barato</strong> frente a su propio historial en tu lista. No fuerces ninguna compra: el dinero puede esperar.</p>`;
    } else {
      cands.forEach((c) => {
        const comp = reparto.compras[c.ticker] || 0;
        const dosMeses = M.avisosDosMeses(c.ticker, ops, M.hoyISO());
        const ia = c.ia && !c.ia.error ? c.ia : null;
        const abierta = abiertos.has("c:" + c.ticker);
        h += `<article class="m-cand ${abierta ? "abierta" : ""}" data-t="${esc(c.ticker)}">
          <div class="m-cab">
            <div><div class="m-tk">${esc(c.ticker)} ${c.doble_descuento ? '<span title="Por debajo del precio de entrada de la tesis">⭐</span>' : ""}
                <span class="pill v-${esc(c.veredicto)}">${esc(c.veredicto.replace("_", " "))}</span></div>
                <div class="m-nombre">${esc(c.nombre)}</div></div>
            <div class="m-precio num">${num(c.precio)}<small>${pct(-c.descuento)} vs su mediana 5a</small></div>
          </div>
          <div class="m-compra">${hayImporte
            ? (comp > 0
              ? `Comprar <strong class="num">${usd(comp)}</strong> <span class="m-dim">≈ ${num(redondeaAbajo(comp / c.precio, 2), 2)} acc. · quedaría en el ${pct((((valorPos[c.ticker] || 0) + comp) / totalTras))} de tu cartera mensual</span>`
              : `<span class="m-dim">Ya pesa el tope en tu cartera mensual: no añadas más.</span>`)
            : `<span class="m-dim">Reparto orientativo: ${pct(c.peso)} del importe</span>`}</div>
          <div class="m-chips">
            ${ia ? chip("IA: " + ia.tipo + " (" + ia.confianza + ")", ia.tipo === "estructural" ? "m-mal" : ia.tipo === "temporal" ? "m-bien" : "") : ""}
            ${(c.avisos || []).concat(dosMeses).length ? chip("⚠ " + (c.avisos || []).concat(dosMeses).length + " aviso(s)", "m-warn") : ""}
          </div>
          <div class="m-acciones">
            <button class="m-btn" data-accion="detalle" data-t="${esc(c.ticker)}">${abierta ? "Ocultar detalle" : "Ver detalle"}</button>
            <button class="m-btn m-btn-ppal" data-accion="registrar" data-t="${esc(c.ticker)}" data-p="${c.precio}" data-a="${comp > 0 ? redondeaAbajo(comp / c.precio, 2) : ""}">Registrar compra</button>
          </div>
          <div class="m-detalle">
            ${ia ? `<p><strong>Por qué ha caído (IA):</strong> ${esc(ia.causa)}</p>
                    ${ia.riesgo_clave ? `<p><strong>Riesgo clave:</strong> ${esc(ia.riesgo_clave)}</p>` : ""}
                    ${ia.que_vigilar ? `<p><strong>Qué vigilar:</strong> ${esc(ia.que_vigilar)}</p>` : ""}`
                : `<p class="m-dim">Sin explicación de la IA en este informe.</p>`}
            ${c.salud ? `<p><strong>Último trimestre:</strong> ${esc(c.salud.resumen)}</p>` : ""}
            <p><strong>Tesis:</strong> nota ${c.nota ?? "—"}/10 · rango de valor ${c.valor_min ?? "—"}–${c.valor_max ?? "—"} · precio de entrada ${c.precio_entrada ?? "—"}${c.tesis_dias != null ? ` · analizada hace ${c.tesis_dias} días` : ""}</p>
            ${(c.avisos || []).concat(dosMeses).map((a) => `<p class="m-aviso">⚠️ ${esc(a)}</p>`).join("")}
            ${(c.titulares || []).length ? `<p><strong>Titulares recientes</strong></p><ul>${c.titulares.map((t) => `<li>${esc(t.fecha)} · ${esc(t.titular)}</li>`).join("")}</ul>` : ""}
          </div>
        </article>`;
      });
      if (reparto.sobrante > 0.005) {
        h += `<div class="m-aviso">${usd(reparto.sobrante)} no caben por los topes (máx. ${pct(inf.parametros.tope_posicion)} por empresa): guárdalos para el mes que viene, no los fuerces.</div>`;
      }
    }

    const otras = [].concat((inf.vetadas || []).map((v) => `<li><strong>${esc(v.ticker)}</strong>: ${esc(v.motivo)}</li>`),
                            (inf.en_espera || []).map((v) => `<li><strong>${esc(v.ticker)}</strong>: ${esc(v.motivo)}</li>`));
    if (otras.length) h += `<details class="m-det"><summary>Otras que cumplían el filtro de precio (${otras.length})</summary><ul>${otras.join("")}</ul></details>`;
    h += `<p class="m-nota">De ${inf.resumen.n_lista} empresas de tu lista, ${inf.resumen.n_con_senal} tienen 5 años de precios para compararlas con su propio rango.</p>`;
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
          <div><div class="m-tk">${esc(f.ticker)} ${chip(etiqueta, clase)}</div><div class="m-nombre">${esc(f.nombre)}</div></div>
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
        <div id="m-form-msg"></div>
        <button class="m-btn m-btn-ppal" type="submit">Guardar operación</button>
      </form>`;
    const ult = [...ops].sort((a, b) => b.fecha.localeCompare(a.fecha)).slice(0, 40);
    h += `<h3>Operaciones (${ops.length})</h3>`;
    h += ult.length ? `<ul class="m-ops">${ult.map((o) => `<li><span class="${o.tipo === "compra" ? "m-pos" : "m-neg"}">${o.tipo === "compra" ? "Compra" : "Venta"}</span>
        <strong>${esc(o.ticker)}</strong> ${num(o.acciones, 4)} × ${num(o.precio)} · ${esc(fecha(o.fecha))}
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
        <li><strong>Qué mira:</strong> de las empresas de tu lista (COMPRA y VIGILAR), las que están baratas frente a su propio rango de 5 años, sin estar hundidas y habiendo dejado de caer. Máx. 4 y 2 por sector.</li>
        <li><strong>Qué NO hace:</strong> no manda vender por precio. En la prueba histórica (S&amp;P 500, 2012-2026), comprar el bajón y <em>vender cuando está cara</em> rindió menos que el S&amp;P 500 (11,3 % frente a 12,7 % anual tras impuestos). Se vende, si acaso, por tesis rota.</li>
        <li><strong>Estado:</strong> experimental. La prueba histórica no confirma que la regla de compra bata al índice (está sesgada a favor y no distingue suerte de método). El juez real es el marcador de arriba: dale 12-24 meses con importes que puedas permitirte, y si no supera al S&amp;P 500, deja de usarla.</li>
        <li>La IA y los chequeos solo pueden quitar o avisar sobre una candidata, nunca añadirla. La IA solo lee titulares; si no explican la caída, dice «incierto».</li>
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
    else h = (aviso ? `<div class="m-aviso">${esc(aviso)}</div>` : "") + seccionEsteMes() + seccionCartera() + seccionMarcador();
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
    if (a === "detalle" || a === "detallepos") {
      const k = (a === "detalle" ? "c:" : "p:") + t;
      abiertos.has(k) ? abiertos.delete(k) : abiertos.add(k);
      const card = b.closest("article"); card.classList.toggle("abierta", abiertos.has(k));
      b.textContent = abiertos.has(k) ? (a === "detalle" ? "Ocultar detalle" : "Ocultar") : "Ver detalle";
    } else if (a === "registrar" || a === "vender") {
      prefill = { tipo: a === "vender" ? "venta" : "compra", ticker: t, precio: b.dataset.p, acciones: b.dataset.a };
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
