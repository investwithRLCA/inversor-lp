// Vigía · compra del mes en Trading 212 — script para la app Scriptable del iPhone.
//
// Cómo se instala: en Vigía → Este mes → «Comprar en Trading 212» → Preparar el iPhone → «Copiar el
// script»; en Scriptable, «+», pega y llama al script exactamente «Vigia Compra». No lleva ninguna
// clave: la pide la primera vez y la guarda en el llavero de este iPhone.
//
// Qué hace cuando pulsas «Comprar ahora» en Vigía:
//  1. Recibe de Vigía tu importe, tu reparto, la cuenta (real o Practice) y lo que ya tienes.
//  2. Lee la lista pública del mes (docs/compra/lista.json). Si aún no es la de este mes, no compra.
//  3. Reparte el importe entre secciones y empresas con la misma regla que Vigía — a partes iguales,
//     sin pasar del tope por empresa contando lo que ya tienes — y, tras UNA confirmación, lanza una
//     orden de compra a mercado por empresa. SOLO COMPRA: nunca vende.
//  4. Te enseña el resultado y copia la compra para apuntarla en Vigía («Pegar compra del iPhone»).
//
// Una compra por mes y cuenta (si quieres otra, te lo pregunta). Tu importe, tu reparto y lo que ya
// tienes se guardan solo en este iPhone. Lo único que sale de aquí son las órdenes a Trading 212.

const VERSION = 7;
const NOMBRE = "Vigia Compra";
const LISTA_POR_DEFECTO = "https://investwithrlca.github.io/inversor-lp/compra/lista.json";
const API = { live: "https://live.trading212.com", demo: "https://demo.trading212.com" };
const NOMBRE_ENTORNO = { live: "cuenta real", demo: "cuenta Practice (prueba)" };
const SECCION = { lynch_p: "Pequeñas", calidad_g: "Grandes", lynch_g: "Grandes (Lynch)", dividendos: "Dividendos" };
const TOPE_POSICION = 0.25;
const MIN_ORDEN_EUR = 1;
const MAX_IMPORTE_MES = 100000;
const PREFIJO_COMPRA = "VIGIA-COMPRA1:";
const URL_COPIAR = "scriptable:///run?scriptName=" + encodeURIComponent(NOMBRE) + "&accion=copiar";

// ───────────── almacenamiento (solo en este iPhone) ─────────────
const fm = FileManager.local();
const DIR = fm.joinPath(fm.documentsDirectory(), "vigia");
function ruta(nombre) {
  if (!fm.fileExists(DIR)) fm.createDirectory(DIR, true);
  return fm.joinPath(DIR, nombre);
}
function leerJSON(nombre, defecto) {
  const p = ruta(nombre);
  try { return fm.fileExists(p) ? JSON.parse(fm.readString(p)) : defecto; } catch (e) { return defecto; }
}
function guardarJSON(nombre, valor) { fm.writeString(ruta(nombre), JSON.stringify(valor)); }
const claveLlavero = (ent) => "vigia.t212." + ent;

// Nota para el atajo de Atajos (iCloud Drive → Scriptable → vigia-mes-hecho.txt): el mes cuya compra ya
// está resuelta. El atajo la lee antes de abrir Scriptable, así solo lo abre cuando hay algo que comprar.
const MARCA_ATAJO = "vigia-mes-hecho.txt";
function marcarMes(mes) {
  try {
    const nube = FileManager.iCloud();
    nube.writeString(nube.joinPath(nube.documentsDirectory(), MARCA_ATAJO), mes || "");
  } catch (e) { /* sin iCloud Drive: el atajo abrirá Scriptable cada vez, sin más */ }
}
const entornoDe = (conf) => (conf && conf.entorno === "demo" ? "demo" : "live");

// ───────────── utilidades ─────────────
const dos = (n) => String(n).padStart(2, "0");
const mesDe = (d) => `${d.getFullYear()}-${dos(d.getMonth() + 1)}`;
const hoy = () => { const d = new Date(); return `${d.getFullYear()}-${dos(d.getMonth() + 1)}-${dos(d.getDate())}`; };
const MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"];
const nombreMes = (mes) => { const [a, m] = String(mes).split("-").map(Number); return MESES[m - 1] ? `${MESES[m - 1]} ${a}` : String(mes); };
function euros(n) {
  const v = Math.round(Number(n || 0) * 100) / 100;
  const [e, d] = Math.abs(v).toFixed(2).split(".");
  return (v < 0 ? "-" : "") + e.replace(/\B(?=(\d{3})+(?!\d))/g, ".") + "," + d + " €";
}
const acciones = (q) => String(Number(q)).replace(".", ",");
function b64url(obj) {
  return Data.fromString(JSON.stringify(obj)).toBase64String().replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function deB64url(txt) {
  const s = String(txt || "").trim().replace(/-/g, "+").replace(/_/g, "/");
  return JSON.parse(Data.fromBase64String(s + "===".slice((s.length + 3) % 4)).toRawString());
}
const esperar = (ms) => new Promise((ok) => Timer.schedule(ms, false, ok));

// ───────────── reparto: COPIA EXACTA de docs/mensual.js (una prueba lo comprueba) ─────────────
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

/** Importe (€) que toca invertir en `mes`: el del mes concreto si lo hay; si no, el habitual. */
function importeDelMes(auto, mes) {
  const m = (auto && auto.meses) || {};
  const v = Object.prototype.hasOwnProperty.call(m, mes) ? Number(m[mes]) : Number(auto && auto.total);
  return isFinite(v) && v > 0 ? Math.min(v, MAX_IMPORTE_MES) : 0;
}

/** Reparto entre secciones normalizado a 1 (solo las claves dadas, sin negativos). */
function normalizarReparto(reparto, claves) {
  const out = {};
  let suma = 0;
  (claves || Object.keys(reparto || {})).forEach((k) => {
    const v = Number((reparto || {})[k]);
    if (isFinite(v) && v > 0) { out[k] = v; suma += v; }
  });
  if (!(suma > 0)) return {};
  Object.keys(out).forEach((k) => (out[k] = out[k] / suma));
  return out;
}

/** Plan de compra del mes: cuántas acciones de cada empresa, sección por sección.
 *  lista: la pública del mes (docs/compra/lista.json). conf: la del iPhone
 *  {total, meses, reparto, posiciones (valor en $ por empresa), fuera}. */
function planCompra(lista, conf, mes) {
  const importe = importeDelMes(conf, mes);
  const secs = (lista && lista.secciones) || {};
  const reparto = normalizarReparto(conf && conf.reparto);
  const fx = Number(lista && lista.eurusd), margen = Number((lista && lista.margen) || 0.98);
  const tope = Number((lista && lista.tope_posicion) || TOPE_POSICION);
  const fuera = new Set(((conf && conf.fuera) || []).map((t) => String(t).toUpperCase()));
  const valorPos = {};
  Object.entries((conf && conf.posiciones) || {}).forEach(([k, v]) => { if (Number(v) > 0) valorPos[k] = Number(v); });
  const ordenes = [], secciones = {};
  let sinInvertir = 0;
  if (!(importe > 0) || !(fx > 0)) return { importe, ordenes, secciones, sinInvertir: importe, gastoEur: 0 };
  Object.keys(reparto).forEach((k) => {
    const eur = importe * reparto[k];
    const disponibles = ((secs[k] && secs[k].ordenes) || []).filter((o) => !fuera.has(String(o.empresa).toUpperCase()) && Number(o.precio_usd) > 0);
    const info = { eur, pausada: !!(secs[k] && secs[k].pausada), sinLista: !secs[k], ordenes: [], sobranteEur: 0 };
    secciones[k] = info;
    if (!disponibles.length) { info.sobranteEur = eur; sinInvertir += eur; return; }
    const usd = eur * fx * margen;
    const r = repartir(usd, disponibles.map((o) => o.empresa), valorPos, tope);
    let gastado = 0;
    disponibles.forEach((o) => {
      const dolares = r.compras[o.empresa] || 0;
      const q = Math.floor((dolares / Number(o.precio_usd)) * 1000 + 1e-9) / 1000;
      const usdOrden = q * Number(o.precio_usd);
      if (!(q > 0) || usdOrden / fx < MIN_ORDEN_EUR) return;
      const ord = { seccion: k, empresa: o.empresa, ticker: o.ticker, cantidad: q, precio_usd: Number(o.precio_usd),
                    usd: Math.round(usdOrden * 100) / 100, eur: Math.round((usdOrden / fx) * 100) / 100 };
      info.ordenes.push(ord); ordenes.push(ord);
      valorPos[o.empresa] = (valorPos[o.empresa] || 0) + usdOrden;
      gastado += usdOrden / fx;
    });
    info.sobranteEur = Math.max(0, eur - gastado);
    sinInvertir += info.sobranteEur;
  });
  const gastoEur = ordenes.reduce((s, o) => s + o.eur, 0);
  return { importe, ordenes, secciones, sinInvertir, gastoEur };
}
// ───────────── fin de la copia ─────────────

// ───────────── avisos y diálogos ─────────────
async function avisar(titulo, cuerpo, abrir) {
  const n = new Notification();
  n.title = titulo;
  n.body = cuerpo;
  n.threadIdentifier = "vigia";
  if (abrir) n.openURL = abrir;
  await n.schedule();
}
async function avisarUnaVezAlDia(clave, titulo, cuerpo) {
  const est = leerJSON("estado.json", {});
  est.avisos = est.avisos || {};
  if (est.avisos[clave] === hoy()) return;
  est.avisos[clave] = hoy();
  guardarJSON("estado.json", est);
  await avisar(titulo, cuerpo);
}
/** Diálogo (solo con el script abierto en la app). Devuelve el índice del botón, o -1 si cancelas. */
async function dialogo(titulo, mensaje, botones, conCancelar = true) {
  if (!config.runsInApp) return -1;
  const a = new Alert();
  a.title = titulo;
  a.message = mensaje || "";
  (botones || ["OK"]).forEach((b) => a.addAction(b));
  if (conCancelar) a.addCancelAction("Cancelar");
  return await a.present();
}

// ───────────── datos ─────────────
function leerConfig() {
  const c = leerJSON("config.json", null);
  return c && typeof c === "object" && c.v === 1 ? c : null;
}
async function bajarJSON(url) {
  const r = new Request(url + (url.includes("?") ? "&" : "?") + "t=" + Date.now());
  r.timeoutInterval = 20;
  const j = await r.loadJSON();
  if (!j || typeof j !== "object" || !j.secciones || !/^\d{4}-\d{2}$/.test(j.mes || "")) throw new Error("lista no válida");
  return j;
}
/** La lista real del mes (lista.json). Con la cuenta Practice, si hay una lista de PRUEBA de este mes
 *  (lista-prueba.json), esa. La cuenta real nunca mira la de prueba. */
async function bajarLista(conf) {
  const url = (conf && typeof conf.lista === "string" && /^https:\/\//.test(conf.lista)) ? conf.lista : LISTA_POR_DEFECTO;
  if (entornoDe(conf) === "demo") {
    try {
      const p = await bajarJSON(url.replace(/lista\.json(\?.*)?$/, "lista-prueba.json"));
      if (p.mes === mesDe(new Date())) return p;
    } catch (e) { /* sin lista de prueba: la normal */ }
  }
  return bajarJSON(url);
}

/** Días desde los precios de la lista («2026-12-01 15:07 UTC (…)»); null si no se sabe. */
function diasDePrecios(lista) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String((lista && lista.precios_de) || ""));
  if (!m) return null;
  const d = Date.UTC(+m[1], +m[2] - 1, +m[3]);
  return Math.floor((Date.now() - d) / 86400000);
}
function resumenPlan(plan, lista, ent) {
  const cuenta = ent === "live" ? "CUENTA REAL" : NOMBRE_ENTORNO[ent];
  const lineas = [`${euros(plan.importe)} · ${cuenta} · lista de ${nombreMes(lista.mes)}${lista.prueba ? " (PRUEBA)" : ""}`];
  const dias = diasDePrecios(lista);
  if (dias != null && dias > 3) lineas.push(`Ojo: los precios de la lista son de hace ${dias} días. Si han subido mucho, puede que alguna orden no entre por saldo.`);
  Object.entries(plan.secciones).forEach(([k, s]) => {
    lineas.push("", `${SECCION[k] || k}: ${euros(s.eur)}`);
    if (!s.ordenes.length) lineas.push(s.pausada ? "  en pausa este mes: no se invierte" : "  sin empresas: no se invierte");
    s.ordenes.forEach((o) => lineas.push(`  ${o.empresa}: ${acciones(o.cantidad)} acc. ≈ ${euros(o.eur)}`));
  });
  lineas.push("", `Total ≈ ${euros(plan.gastoEur)}. Se queda sin invertir ≈ ${euros(plan.importe - plan.gastoEur)} (margen del 2 %, topes y redondeo).`);
  return lineas.join("\n");
}

// ───────────── Trading 212 ─────────────
async function enviarOrden(ent, auth, orden) {
  const r = new Request(API[ent] + "/api/v0/equity/orders/market");
  r.method = "POST";
  r.headers = { "Authorization": "Basic " + auth, "Content-Type": "application/json" };
  r.body = JSON.stringify({ ticker: orden.ticker, quantity: orden.cantidad });
  r.timeoutInterval = 45;          // con poca cobertura, mejor esperar: una orden sin respuesta no se puede repetir a ciegas
  let texto;
  try { texto = await r.loadString(); }
  catch (e) { return { ok: false, dudosa: true, motivo: "sin respuesta: mira en Trading 212 si se hizo" }; }
  const st = (r.response && r.response.statusCode) || 0;
  let j = null;
  try { j = JSON.parse(texto); } catch (e) { j = null; }
  if (st >= 200 && st < 300 && j && j.id != null) return { ok: true, id: j.id };
  if (st >= 200 && st < 300) return { ok: false, dudosa: true, motivo: "respuesta rara: mira en Trading 212 si se hizo" };
  const explicacion = j && (j.clarification || j.detail || j.message || j.title);
  const motivo = (j && [j.code, explicacion].filter(Boolean).join(": ")) || ("HTTP " + st);
  return { ok: false, status: st, motivo: String(motivo).slice(0, 140) };
}

/** Decimales que pide Trading 212 en su respuesta («invalid quantity precision 2» → 2), o null. */
function decimalesPedidos(motivo) {
  const m = /precision\D{0,20}(\d)/i.exec(String(motivo || ""));
  return m ? Number(m[1]) : null;
}
function decimalesDe(q) {
  const t = String(q);
  return t.includes(".") ? t.split(".")[1].length : 0;
}
/** La misma orden con menos decimales (redondeando hacia abajo: nunca se gasta de más). */
function conDecimales(o, dec) {
  const f = Math.pow(10, dec);
  const cantidad = Number((Math.floor(o.cantidad * f + 1e-9) / f).toFixed(dec));
  const k = o.cantidad > 0 ? cantidad / o.cantidad : 0;
  return { ...o, cantidad, eur: Math.round(o.eur * k * 100) / 100, usd: o.usd != null ? Math.round(o.usd * k * 100) / 100 : o.usd };
}

// ───────────── comprar ─────────────
// Candado: si dos ejecuciones coinciden (dos automatizaciones a la vez), solo una sigue.
function cogerCandado() {
  const c = leerJSON("candado.json", null);
  if (c && Date.now() - Number(c.t) < 5 * 60 * 1000) return false;
  guardarJSON("candado.json", { t: Date.now() });
  return true;
}
function soltarCandado() { guardarJSON("candado.json", { t: 0 }); }

async function comprar(manual) {
  if (!cogerCandado()) return "ocupado";
  try { return await comprarSinCandado(manual); } finally { soltarCandado(); }
}

async function comprarSinCandado(manual) {
  const conf = leerConfig();
  if (!conf) {
    await avisarUnaVezAlDia("sinconfig", "Vigía: falta configurar", "Compra desde Vigía: Este mes → «Comprar en Trading 212» → «Comprar ahora».");
    if (manual) await dialogo("Falta configurar", "Compra desde Vigía: Este mes → «Comprar en Trading 212» → «Comprar ahora».", ["OK"], false);
    return "sinconfig";
  }
  const ent = entornoDe(conf);
  const mes = mesDe(new Date());
  if (!manual && conf.modo === "manual") {
    // Ahora se compra a mano desde Vigía («Comprar ahora»): si queda alguna automatización
    // antigua, no compra nada y marca el mes para que el atajo deje de abrir Scriptable.
    marcarMes(mes);
    return "manual";
  }
  const est = leerJSON("estado.json", {});
  est.compras = est.compras || {};
  const id = ent + ":" + mes;
  const previa = est.compras[id];
  if (previa) {
    if (previa.estado === "enviando" && !previa.avisado) {
      previa.avisado = true;
      guardarJSON("estado.json", est);
      await avisar(`Vigía: revisa la compra de ${nombreMes(mes)}`, "Se cortó mientras enviaba las órdenes. Mira en Trading 212 qué se compró: Vigía no volverá a comprar este mes.");
    }
    let otra = false;
    if (manual) {
      const cortada = previa.estado === "enviando";
      const n = previa.resultados ? previa.resultados.filter((r) => r.ok).length : 0;
      const txt = cortada
        ? `La compra de ${nombreMes(mes)} (${NOMBRE_ENTORNO[ent]}) se cortó a mitad el ${previa.fecha}. Antes de repetir, mira en Trading 212 qué se compró.`
        : previa.estado === "saltado"
          ? `${nombreMes(mes)} (${NOMBRE_ENTORNO[ent]}) se saltó el ${previa.fecha} (importe a 0 € o nada que comprar).`
          : `Ya compraste ${nombreMes(mes)} (${NOMBRE_ENTORNO[ent]}) el ${previa.fecha}${n ? `: ${n} empresas` : ""}.`;
      otra = (await dialogo("Este mes ya está hecho", txt + "\n\n¿Comprar otra vez? Se reparte de nuevo el importe entre las empresas de la lista.", ["Comprar otra vez"])) === 0;
    }
    if (!otra) { marcarMes(mes); return "yahecho"; }
  }
  // La compra anterior de este mes (si pediste otra) se guarda aparte solo cuando de verdad se apunta la nueva.
  let repetir = !!previa, archivada = false;
  const registrar = (valor) => {
    if (repetir) { archivarCompra(est, id); repetir = false; archivada = true; }
    est.compras[id] = valor;
    guardarJSON("estado.json", est);
  };
  const dia = new Date().getDate();
  let lista;
  try { lista = await bajarLista(conf); }
  catch (e) {
    if (dia >= 5) await avisarUnaVezAlDia("sinlista", "Vigía: no puedo leer la lista", "No he podido descargar la lista de la compra. Lo vuelvo a intentar mañana.");
    if (manual) await dialogo("Sin lista", "No he podido descargar la lista de la compra. Mira tu conexión.", ["OK"], false);
    return "sinlista";
  }
  if (lista.mes !== mes) {
    if (dia >= 5) await avisarUnaVezAlDia("nolista:" + mes, `Vigía: aún no hay lista de ${nombreMes(mes)}`, "Vigía todavía no ha publicado la compra de este mes. Si sigue así, mira GitHub → Actions → «Compra del mes».");
    if (manual) await dialogo("Aún no hay lista de este mes", `La última lista es de ${nombreMes(lista.mes)}. La de ${nombreMes(mes)} se publica el primer día de bolsa del mes, a las 15:07 UTC (te llega un aviso).`, ["OK"], false);
    return "nolista";
  }
  if (lista.prueba && ent === "live") {
    if (manual) await dialogo("Lista de prueba", "La lista de este mes es de PRUEBA: solo se compra en la cuenta Practice, nunca con dinero real.", ["OK"], false);
    return "prueba";
  }
  if ((Number(lista.script_minimo) || 1) > VERSION) {
    await avisarUnaVezAlDia("version", "Vigía: actualiza el script", "Vigía → Este mes → «Comprar en Trading 212» → Preparar el iPhone → «Copiar el script» y pégalo en Scriptable sobre «Vigia Compra».");
    if (manual) await dialogo("Actualiza el script", "Esta lista necesita un script más nuevo. Vigía → «Copiar el script» y pégalo en Scriptable sobre «Vigia Compra».", ["OK"], false);
    return "version";
  }
  const importe = importeDelMes(conf, mes);
  if (!(importe > 0)) {
    registrar({ estado: "saltado", fecha: hoy() });
    await avisar(`Vigía: ${nombreMes(mes)} sin compra`, "Este mes lo tienes a 0 € en Vigía: no se compra nada.");
    marcarMes(mes);
    return "cero";
  }
  let auth = leerClave(ent);
  if (!auth && !manual && leerJSON("claves.json", {})[ent]) {
    // La clave está guardada pero el llavero no se puede leer ahora (iPhone bloqueado):
    // no se avisa ni se marca nada; la próxima ejecución con el iPhone en uso comprará.
    return "bloqueado";
  }
  if (!auth && manual) {
    // Con el script abierto a mano: la pide y, si la guardas, sigue con la compra.
    if (await pedirClave(ent)) auth = leerClave(ent);
    if (!auth) return "sinclave";
  }
  if (!auth) {
    await avisarUnaVezAlDia("sinclave:" + ent, "Vigía: falta la clave de Trading 212", `Abre Scriptable → «Vigia Compra» → «Poner o cambiar la clave» (${NOMBRE_ENTORNO[ent]}).`);
    return "sinclave";
  }
  const plan = planCompra(lista, conf, mes);
  if (!plan.ordenes.length) {
    registrar({ estado: "saltado", fecha: hoy() });
    await avisar(`Vigía: ${nombreMes(mes)} sin compra`, "No hay nada que comprar este mes (secciones en pausa o en el tope). El dinero se queda en la cuenta.");
    marcarMes(mes);
    return "nada";
  }
  if (manual) {
    const r = await dialogo("¿Comprar ahora?", resumenPlan(plan, lista, ent), ["Comprar"]);
    if (r !== 0) return "cancelado";
  }
  // La marca se guarda ANTES de enviar: si algo se corta a mitad, nunca se compra dos veces.
  registrar({ estado: "enviando", fecha: hoy(), inicio: new Date().toISOString() });
  marcarMes(mes);
  const resultados = [];
  let corte = null;
  est.decimales = est.decimales || {};          // decimales que admite cada valor (aprendidos de Trading 212)
  for (const o0 of plan.ordenes) {
    let o = o0;
    if (corte) { resultados.push({ ...o, ok: false, motivo: corte }); continue; }
    const sabidos = est.decimales[o.ticker];
    if (sabidos != null && sabidos < decimalesDe(o.cantidad)) o = conDecimales(o, sabidos);
    if (!(o.cantidad > 0) || o.eur < MIN_ORDEN_EUR) {
      resultados.push({ ...o, ok: false, motivo: `Trading 212 solo admite ${sabidos === 0 ? "acciones enteras" : `${sabidos} decimales`} en esta empresa y el importe no llega` });
      continue;
    }
    let res = await enviarOrden(ent, auth, o);
    if (!res.ok && res.status === 429) { await esperar(3000); res = await enviarOrden(ent, auth, o); }
    if (!res.ok && (res.status === 401 || res.status === 403)) corte = `Trading 212 rechaza la clave (${res.status})`;
    // Rechazada por los decimales (p. ej. «invalid quantity precision 2»): se recorta y se reintenta una vez.
    // Es seguro: una orden rechazada (400) no se ha creado.
    const dec = !res.ok && res.status === 400 ? decimalesPedidos(res.motivo) : null;
    if (dec != null && dec < decimalesDe(o.cantidad)) {
      est.decimales[o.ticker] = dec;
      guardarJSON("estado.json", est);
      const o2 = conDecimales(o, dec);
      if (o2.cantidad > 0 && o2.eur >= MIN_ORDEN_EUR) {
        o = o2;
        res = await enviarOrden(ent, auth, o);
        if (!res.ok && res.status === 429) { await esperar(3000); res = await enviarOrden(ent, auth, o); }
      } else {
        res = { ok: false, status: 400, motivo: `Trading 212 solo admite ${dec === 0 ? "acciones enteras" : `${dec} decimales`} en esta empresa y el importe no llega` };
      }
    }
    resultados.push({ ...o, ...res });
  }
  // Sin respuesta de Trading 212 (cobertura, lentitud): no se sabe si se compró y repetirla a ciegas podría
  // comprar dos veces. Con el script abierto, te pregunta: lo miras en Trading 212 y decides.
  if (manual) {
    for (const d of resultados.filter((r) => !r.ok && r.dudosa)) {
      const r = await dialogo(`¿Se ha comprado ${d.empresa}?`,
        `Trading 212 no contestó al comprar ${d.empresa} (${acciones(d.cantidad)} acc. ≈ ${euros(d.eur)}), así que no sé si se hizo.\n\n` +
        `Abre Trading 212 (${NOMBRE_ENTORNO[ent]}) y mira si hay una orden o una compra de ${d.empresa} de hoy. Luego vuelve aquí.`,
        ["Sí, está", "No está: comprarla ahora", "No lo sé"], false);
      if (r === 0) Object.assign(d, { ok: true, dudosa: false, confirmada: true, motivo: null });
      else if (r === 1) {
        let res = await enviarOrden(ent, auth, d);
        if (!res.ok && res.status === 429) { await esperar(3000); res = await enviarOrden(ent, auth, d); }
        Object.assign(d, res.ok ? { ok: true, dudosa: false, id: res.id, motivo: null } : { ...res });
      }
    }
  }
  const buenas = resultados.filter((r) => r.ok);
  const dudosas = resultados.filter((r) => !r.ok && r.dudosa);
  const malas = resultados.filter((r) => !r.ok && !r.dudosa);
  if (!buenas.length && !dudosas.length) {
    delete est.compras[id];               // no se compró nada: se reintenta en la próxima ejecución
    if (archivada) desarchivarCompra(est, id);   // era «otra vez»: la compra anterior sigue siendo la del mes
    guardarJSON("estado.json", est);
    marcarMes(est.compras[id] ? mes : "");
    const motivo = corte || (malas[0] && malas[0].motivo) || "error desconocido";
    if (manual) {
      await dialogo("No se pudo comprar", `${motivo}.\n\nNo se ha comprado nada: puedes volver a intentarlo desde Vigía.`, ["OK"], false);
      return "fallo";
    }
    await avisar("Vigía: no se pudo comprar", `${motivo}. Se volverá a intentar en la próxima ejecución.`);
    return "fallo";
  }
  const veces = (est.repeticiones || {})[id] || 1;
  const compra = { id: veces > 1 ? `${id}#${veces}` : id, mes, entorno: ent, fecha: hoy(),
                   compras: buenas.map((r) => ({ t: r.empresa, q: r.cantidad, p: r.precio_usd, s: r.seccion })) };
  est.compras[id] = { estado: "hecho", fecha: hoy(), inicio: est.compras[id].inicio, compra,
                      resultados: resultados.map((r) => ({ empresa: r.empresa, cantidad: r.cantidad, ok: !!r.ok, motivo: r.motivo || null })) };
  guardarJSON("estado.json", est);
  const gastado = buenas.reduce((s, r) => s + r.eur, 0);
  let cuerpo = `Compradas ${buenas.length} de ${resultados.length} · ≈ ${euros(gastado)} (${NOMBRE_ENTORNO[ent]}).`;
  if (malas.length) cuerpo += ` Fallaron: ${malas.map((r) => `${r.empresa} (${r.motivo})`).join("; ")}.`;
  if (dudosas.length) cuerpo += ` Sin respuesta de Trading 212 (mira si se compraron; si sí, apúntalas a mano en Vigía): ${dudosas.map((r) => r.empresa).join(", ")}.`;
  if (manual) {
    if (ent === "live") Pasteboard.copy(PREFIJO_COMPRA + b64url(compra));
    await avisar(`Vigía: compra de ${nombreMes(mes)}`, cuerpo, URL_COPIAR);
    const fin = ent === "live" ? "\n\nYa está copiada: vuelve a Vigía y pulsa «Pegar compra del iPhone» para apuntarla en tu cartera."
                               : "\n\n(Cuenta Practice: es una prueba, no hace falta apuntarla en Vigía.)";
    await dialogo(malas.length || dudosas.length ? "Compra enviada (con fallos)" : "Compra enviada ✓", cuerpo + fin, ["OK"], false);
    return "hecho";
  }
  cuerpo += ent === "live" ? " Toca para apuntarlas en Vigía." : " (Prueba: no hace falta apuntarla.)";
  await avisar(`Vigía: compra de ${nombreMes(mes)}`, cuerpo, URL_COPIAR);
  return "hecho";
}

// ───────────── clave de Trading 212 ─────────────
function leerClave(ent) {
  try { return Keychain.contains(claveLlavero(ent)) ? Keychain.get(claveLlavero(ent)) || null : null; } catch (e) { return null; }
}
/** Quita los espacios y saltos de línea que se cuelan al copiar. */
const limpiarDato = (v) => String(v == null ? "" : v).replace(/\s+/g, "");
const taparDato = (v) => (v.length > 10 ? `${v.slice(0, 4)}…${v.slice(-4)}` : "…") + ` (${v.length} caracteres)`;
/** ¿Puede ser una API key o un secret? Solo descarta lo que claramente no lo es (un enlace, el script…). */
const pareceDato = (v) => v.length >= 8 && v.length <= 400 && !/:\/\/|[{}<>"]|^VIGIA/i.test(v);

/** Un dato por pantalla. Si dejas el campo vacío, usa lo que tengas copiado (así no hay que pegar en
 *  el campo). Devuelve el texto limpio, "" si no había nada, o null si cancelas. */
async function pedirDato(titulo, mensaje, campo) {
  const a = new Alert();
  a.title = titulo;
  a.message = mensaje;
  a.addTextField(campo, "");
  a.addAction("Pegar");
  a.addCancelAction("Cancelar");
  if ((await a.present()) !== 0) return null;
  const escrito = limpiarDato(a.textFieldValue(0));
  if (escrito) return escrito;
  let copiado = "";
  try { copiado = Pasteboard.pasteString() || ""; } catch (e) { copiado = ""; }
  return limpiarDato(copiado);
}

/** Comprueba la clave SIN comprar nada: manda a Trading 212 una orden vacía (sin empresa ni cantidad),
 *  que siempre rechaza. Si la rechaza por la orden (400), la clave es buena y puede comprar; si la
 *  rechaza por la clave, 401 (no la reconoce) o 403 (le falta el permiso). */
async function probarClave(ent, auth) {
  const r = new Request(API[ent] + "/api/v0/equity/orders/market");
  r.method = "POST";
  r.headers = { "Authorization": "Basic " + auth, "Content-Type": "application/json" };
  r.body = "{}";
  r.timeoutInterval = 15;
  try { await r.loadString(); } catch (e) { return "sinred"; }
  const st = (r.response && r.response.statusCode) || 0;
  if (st === 401) return "mala";
  if (st === 403) return "sinpermiso";
  if (st === 400 || st === 422) return "ok";
  return "sincomprobar";
}

/** Pide la API key y el API secret de una cuenta, en dos pasos, y los guarda en el llavero del iPhone.
 *  Devuelve true si se guardó. */
async function pedirClave(entPreferido) {
  if (!config.runsInApp) return false;
  let ent = entPreferido;
  if (!ent) {
    const r = await dialogo("¿Qué clave?", "Una clave para cada cuenta de Trading 212.", ["Cuenta real", "Cuenta Practice (prueba)"]);
    if (r < 0) return false;
    ent = r === 0 ? "live" : "demo";
  }
  const cuenta = ent === "demo" ? "cuenta Practice" : "cuenta real";
  const donde = `Trading 212 (${cuenta}) → Settings → API (Beta): clave con el permiso «Orders: execute».`;
  for (let intento = 0; intento < 5; intento++) {
    let clave = null, secreto = null;
    for (let n = 0; !clave && n < 10; n++) {
      const v = await pedirDato("Clave de Trading 212 · paso 1 de 2",
        `${donde}\n\nCopia la API KEY, vuelve aquí y pulsa «Pegar».\n(También puedes pegarla o escribirla en el campo.)`, "API key");
      if (v === null) return false;
      const sep = v.indexOf(":");
      if (sep > 0 && !v.includes("://") && pareceDato(v.slice(0, sep)) && pareceDato(v.slice(sep + 1))) {
        clave = v.slice(0, sep); secreto = v.slice(sep + 1);           // copiaste «clave:secreto» de una vez
        break;
      }
      if (pareceDato(v)) { clave = v; break; }
      const r = await dialogo("Eso no es la API key", v ? `Lo que hay copiado no parece una clave: ${taparDato(v)}. Cópiala otra vez en Trading 212.` : "No hay nada copiado ni escrito. Copia la API key en Trading 212.", ["Repetir"]);
      if (r !== 0) return false;
    }
    for (let n = 0; clave && !secreto && n < 10; n++) {
      const v = await pedirDato("Clave de Trading 212 · paso 2 de 2",
        `API key ✓ ${taparDato(clave)}\n\nAhora copia el API SECRET, vuelve aquí y pulsa «Pegar».`, "API secret");
      if (v === null) return false;
      let problema = null;
      if (v === clave) problema = "Has pegado la API key otra vez. Copia el API secret (el otro dato) en Trading 212.";
      else if (!pareceDato(v)) problema = v ? `Lo que hay copiado no parece el secret: ${taparDato(v)}. Cópialo otra vez.` : "No hay nada copiado ni escrito. Copia el API secret en Trading 212.";
      if (!problema) { secreto = v; break; }
      const r = await dialogo("Eso no es el API secret", problema, ["Repetir"]);
      if (r !== 0) return false;
    }
    if (!clave || !secreto) return false;
    const auth = Data.fromString(clave + ":" + secreto).toBase64String();
    const prueba = await probarClave(ent, auth);
    if (prueba === "mala" || prueba === "sinpermiso") {
      const txt = prueba === "mala"
        ? `Trading 212 no reconoce esta clave en la ${cuenta}. Mira que sea de esa cuenta (la real y la Practice tienen claves distintas) y que la API key y el secret sean de la misma clave.`
        : "Trading 212 reconoce la clave, pero no le deja comprar: le falta el permiso «Orders: execute» (o tiene limitadas las IP).";
      const r = await dialogo("La clave no funciona", txt, ["Repetir", "Guardarla igualmente"]);
      if (r === 0) continue;
      if (r !== 1) return false;
    }
    Keychain.set(claveLlavero(ent), auth);
    guardarJSON("claves.json", { ...leerJSON("claves.json", {}), [ent]: true });
    const nota = prueba === "ok" ? "Trading 212 la acepta ✓ (comprobado sin comprar nada)."
      : prueba === "sinred" || prueba === "sincomprobar" ? "No he podido comprobarla ahora con Trading 212; se verá en la primera compra." : "";
    await dialogo("Clave guardada ✓", `Guardada en el llavero de este iPhone para la ${cuenta}. ${nota}`.trim(), ["OK"], false);
    return true;
  }
  return false;
}

// ───────────── otras acciones ─────────────

function validarConfig(c) {
  if (!c || typeof c !== "object" || c.v !== 1) return "formato desconocido";
  const total = Number(c.total);
  if (!(total >= 0 && total <= MAX_IMPORTE_MES)) return "importe mensual fuera de rango";
  if (!c.reparto || typeof c.reparto !== "object") return "falta el reparto";
  const suma = Object.values(c.reparto).reduce((s, v) => s + (Number(v) > 0 ? Number(v) : 0), 0);
  if (!(suma > 0)) return "el reparto está vacío";
  for (const [m, v] of Object.entries(c.meses || {})) {
    if (!/^\d{4}-\d{2}$/.test(m) || !(Number(v) >= 0 && Number(v) <= MAX_IMPORTE_MES)) return "un importe de mes concreto no es válido";
  }
  if (c.entorno !== "live" && c.entorno !== "demo") return "cuenta desconocida";
  if (c.lista && !/^https:\/\//.test(c.lista)) return "dirección de la lista no válida";
  return null;
}

function describirConfig(c) {
  const rep = Object.entries(c.reparto).filter(([, v]) => Number(v) > 0).map(([k, v]) => `${SECCION[k] || k} ${Math.round(Number(v) * 100)} %`).join(" · ");
  const meses = Object.entries(c.meses || {}).sort().map(([m, v]) => `${nombreMes(m)}: ${euros(v)}`).join(" · ");
  return [`${euros(c.total)} al mes · ${NOMBRE_ENTORNO[entornoDe(c)]}`, rep, meses ? `Meses concretos: ${meses}` : "",
          `Posiciones: ${Object.keys(c.posiciones || {}).length} empresas · sin Trading 212: ${(c.fuera || []).join(", ") || "ninguna"}`]
    .filter(Boolean).join("\n");
}

async function guardarConfig(datos) {
  let c;
  try { c = deB64url(datos); } catch (e) { c = null; }
  const error = validarConfig(c);
  if (error) { await dialogo("No guardado", `Los datos de Vigía no son válidos (${error}).`, ["OK"], false); return "invalida"; }
  const r = await dialogo("¿Guardar en el iPhone?", describirConfig(c), ["Guardar"]);
  if (r !== 0) return "cancelado";
  guardarJSON("config.json", { ...c, recibido: new Date().toISOString() });
  const ent = entornoDe(c);
  const mesHoy = mesDe(new Date());
  marcarMes((leerJSON("estado.json", {}).compras || {})[ent + ":" + mesHoy] ? mesHoy : "");   // la nota sigue a la cuenta elegida
  if (!Keychain.contains(claveLlavero(ent))) await pedirClave(ent);
  await dialogo("Guardado ✓", "Ya puedes volver a Vigía (arriba a la izquierda).", ["OK"], false);
  return "guardada";
}

async function copiarUltima() {
  const est = leerJSON("estado.json", {});
  const hechas = Object.values(est.compras || {}).filter((x) => x && x.estado === "hecho" && x.compra && x.compra.entorno === "live");
  hechas.sort((a, b) => String(a.compra.mes + (a.inicio || a.fecha)).localeCompare(String(b.compra.mes + (b.inicio || b.fecha))));
  const ult = hechas[hechas.length - 1];
  if (!ult) { await dialogo("Nada que copiar", "Todavía no hay ninguna compra con la cuenta real.", ["OK"], false); return "nada"; }
  Pasteboard.copy(PREFIJO_COMPRA + b64url(ult.compra));
  await dialogo("Copiado ✓", `${ult.compra.compras.length} compras de ${nombreMes(ult.compra.mes)}. Abre Vigía → Este mes → «Pegar compra del iPhone».`, ["OK"], false);
  return "copiada";
}

async function simular(datos) {
  let conf = leerConfig();
  if (datos) {
    let c; try { c = deB64url(datos); } catch (e) { c = null; }
    const error = validarConfig(c);
    if (error) { await dialogo("No se puede simular", `Los datos de Vigía no son válidos (${error}).`, ["OK"], false); return "invalida"; }
    conf = { ...c, meses: {} };
  }
  if (!conf) { await dialogo("Falta configurar", "Abre Vigía → Este mes → «Comprar en Trading 212» → «Probar sin comprar».", ["OK"], false); return "sinconfig"; }
  let lista;
  try { lista = await bajarLista(conf); } catch (e) { await dialogo("Sin lista", "No he podido descargar la lista de la compra.", ["OK"], false); return "sinlista"; }
  const mes = mesDe(new Date());
  const ent = entornoDe(conf);
  const plan = planCompra(lista, conf, mes);
  const est = leerJSON("estado.json", {});
  const previa = (est.compras || {})[ent + ":" + mes];
  let aviso = "SIMULACIÓN: no se envía ninguna orden.";
  if (lista.mes !== mes) aviso += ` La lista es de ${nombreMes(lista.mes)}, no de este mes: el día de la compra puede cambiar.`;
  if (lista.prueba && ent === "live") aviso += " La lista es de PRUEBA: con la cuenta real no se compraría.";
  if (previa) aviso += " Este mes ya hay una compra: al comprar te preguntará si quieres otra.";
  if (!Keychain.contains(claveLlavero(ent))) aviso += ` Falta la clave de la ${NOMBRE_ENTORNO[ent]}.`;
  await dialogo("Simulación", aviso + "\n\n" + resumenPlan(plan, lista, ent), ["OK"], false);
  return "simulada";
}

async function verEstado() {
  const conf = leerConfig();
  const est = leerJSON("estado.json", {});
  const filas = Object.entries(est.compras || {}).sort().slice(-6).map(([k, x]) => {
    const [ent, resto] = k.split(":");
    const mes = String(resto || "").split("#")[0];
    const n = x.resultados ? `${x.resultados.filter((r) => r.ok).length}/${x.resultados.length}` : "";
    return `${nombreMes(mes)} · ${ent === "demo" ? "prueba" : "real"} · ${x.estado} ${n}`;
  });
  const claves = ["live", "demo"].map((e) => `${NOMBRE_ENTORNO[e]}: ${Keychain.contains(claveLlavero(e)) ? "clave guardada" : "sin clave"}`);
  await dialogo(`Vigía · script v${VERSION}`, [conf ? describirConfig(conf) : "Sin configurar.", "", ...claves, "", ...(filas.length ? filas : ["Sin compras todavía."])].join("\n"), ["OK"], false);
  return "estado";
}

/** Guarda aparte la compra de este mes (live:2026-12 → live:2026-12#1) para poder hacer otra.
 *  La siguiente lleva otro id (live:2026-12#2), así Vigía no la toma por la misma. */
function archivarCompra(est, id) {
  est.repeticiones = est.repeticiones || {};
  const veces = est.repeticiones[id] || 1;
  est.compras[`${id}#${veces}`] = est.compras[id];
  delete est.compras[id];
  est.repeticiones[id] = veces + 1;
}

function desarchivarCompra(est, id) {
  const veces = (est.repeticiones || {})[id] || 1;
  if (veces < 2) return;
  est.compras[id] = est.compras[`${id}#${veces - 1}`];
  delete est.compras[`${id}#${veces - 1}`];
  if (veces - 1 <= 1) delete est.repeticiones[id]; else est.repeticiones[id] = veces - 1;
}

async function permitirOtra() {
  const conf = leerConfig();
  const ent = entornoDe(conf);
  const id = ent + ":" + mesDe(new Date());
  const est = leerJSON("estado.json", {});
  if (!est.compras || !est.compras[id]) { await dialogo("Nada que borrar", "Este mes aún no hay compra registrada.", ["OK"], false); return "nada"; }
  const r = await dialogo("¿Permitir otra compra este mes?", `Se olvida la compra de este mes (${NOMBRE_ENTORNO[ent]}) y la próxima ejecución volverá a comprar. Úsalo solo si de verdad no se compró nada.`, ["Sí, olvidarla"]);
  if (r !== 0) return "cancelado";
  archivarCompra(est, id);
  guardarJSON("estado.json", est);
  marcarMes("");
  return "borrada";
}

/** «Comprar ahora» en Vigía: trae el importe, el reparto, la cuenta y tus posiciones. Se guardan como la
 *  configuración (modo manual: la automatización ya no compra) y se compra tras UNA confirmación. */
async function comprarDesdeVigia(datos) {
  let c; try { c = deB64url(datos); } catch (e) { c = null; }
  const error = validarConfig(c);
  if (error) { await dialogo("No se puede comprar", `Los datos de Vigía no son válidos (${error}).`, ["OK"], false); return "invalida"; }
  guardarJSON("config.json", { ...c, meses: {}, modo: "manual", recibido: new Date().toISOString() });
  return comprar(true);
}

async function menu() {
  const opciones = ["Comprar ahora", "Simular (sin comprar)", "Copiar la última compra para Vigía", "Poner o cambiar la clave", "Ver estado", "Permitir otra compra este mes"];
  const r = await dialogo(`Vigía · compra (v${VERSION})`, "Lo normal es comprar desde Vigía: Este mes → «Comprar en Trading 212» → «Comprar ahora».", opciones);
  if (r === 0) return comprar(true);
  if (r === 1) return simular();
  if (r === 2) return copiarUltima();
  if (r === 3) return pedirClave(null);
  if (r === 4) return verEstado();
  if (r === 5) return permitirOtra();
  return "nada";
}

async function main() {
  const q = (typeof args !== "undefined" && args && args.queryParameters) || {};
  const accion = q.accion || (config.runsInApp ? "menu" : "auto");
  if (accion === "auto") return comprar(false);
  if (accion === "config") return guardarConfig(q.datos);
  if (accion === "simular") return simular(q.datos);
  if (accion === "copiar") return copiarUltima();
  if (accion === "clave") return pedirClave(q.entorno === "demo" ? "demo" : q.entorno === "live" ? "live" : null);
  if (accion === "comprar") return comprar(true);
  if (accion === "compra") return comprarDesdeVigia(q.datos);
  return menu();
}

const VIGIA = { VERSION, planCompra, repartir, importeDelMes, normalizarReparto, validarConfig, b64url, deB64url, main };
if (typeof module !== "undefined") module.exports = VIGIA;
if (!(typeof VIGIA_SIN_EJECUTAR !== "undefined" && VIGIA_SIN_EJECUTAR)) {
  await main();
  Script.complete();
}
