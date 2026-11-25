// Vigía · compra mensual automática en Trading 212 — script para la app Scriptable del iPhone.
//
// Cómo se instala: en Vigía → Este mes → «Inversión automática» → «Copiar el script»; en Scriptable,
// «+», pega y llama al script exactamente «Vigia Compra». No lleva ninguna clave: la pide la primera
// vez y la guarda en el llavero de este iPhone.
//
// Qué hace cada vez que lo lanza la automatización diaria de Atajos:
//  1. Si este mes ya compró (o lo dejaste a 0 €), no hace nada.
//  2. Lee la lista pública del mes (docs/compra/lista.json). Si aún no es la de este mes, no hace nada.
//  3. Reparte tu importe del mes (el que pusiste en Vigía) entre secciones y empresas con la misma
//     regla que Vigía — a partes iguales, sin pasar del tope por empresa contando lo que ya tienes —
//     y lanza una orden de compra a mercado por empresa. SOLO COMPRA: nunca vende.
//  4. Te avisa con una notificación; al tocarla copia la compra para apuntarla en Vigía.
//
// Tu importe, tu reparto y lo que ya tienes llegan desde Vigía («Guardar en el iPhone») y se guardan
// solo en este iPhone. Lo único que sale de aquí son las órdenes a Trading 212.

const VERSION = 1;
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
async function bajarLista(conf) {
  const url = (conf && typeof conf.lista === "string" && /^https:\/\//.test(conf.lista)) ? conf.lista : LISTA_POR_DEFECTO;
  const r = new Request(url + (url.includes("?") ? "&" : "?") + "t=" + Date.now());
  r.timeoutInterval = 20;
  const j = await r.loadJSON();
  if (!j || typeof j !== "object" || !j.secciones || !/^\d{4}-\d{2}$/.test(j.mes || "")) throw new Error("lista no válida");
  return j;
}

function resumenPlan(plan, lista, ent) {
  const lineas = [`${euros(plan.importe)} · ${NOMBRE_ENTORNO[ent]} · lista de ${nombreMes(lista.mes)}${lista.prueba ? " (PRUEBA)" : ""}`];
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
  r.timeoutInterval = 20;
  let texto;
  try { texto = await r.loadString(); }
  catch (e) { return { ok: false, dudosa: true, motivo: "sin respuesta: mira en Trading 212 si se hizo" }; }
  const st = (r.response && r.response.statusCode) || 0;
  let j = null;
  try { j = JSON.parse(texto); } catch (e) { j = null; }
  if (st >= 200 && st < 300 && j && j.id != null) return { ok: true, id: j.id };
  if (st >= 200 && st < 300) return { ok: false, dudosa: true, motivo: "respuesta rara: mira en Trading 212 si se hizo" };
  const motivo = (j && (j.detail || j.message || j.title || j.code)) || ("HTTP " + st);
  return { ok: false, status: st, motivo: String(motivo).slice(0, 140) };
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
    await avisarUnaVezAlDia("sinconfig", "Vigía: falta configurar", "Abre Vigía → Este mes → Inversión automática → «Guardar en el iPhone».");
    if (manual) await dialogo("Falta configurar", "Abre Vigía → Este mes → Inversión automática → «Guardar en el iPhone».", ["OK"], false);
    return "sinconfig";
  }
  const ent = entornoDe(conf);
  const mes = mesDe(new Date());
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
    if (manual) await dialogo("Este mes ya está hecho", `La compra de ${nombreMes(mes)} (${NOMBRE_ENTORNO[ent]}) ya se hizo o se saltó. Para repetirla usa «Permitir otra compra este mes».`, ["OK"], false);
    return "yahecho";
  }
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
    if (manual) await dialogo("Aún no hay lista de este mes", `La última lista es de ${nombreMes(lista.mes)}. Se publica el primer día de bolsa del mes, a las 15:07 UTC.`, ["OK"], false);
    return "nolista";
  }
  if (lista.prueba && ent === "live") {
    if (manual) await dialogo("Lista de prueba", "La lista de este mes es de PRUEBA: solo se compra en la cuenta Practice, nunca con dinero real.", ["OK"], false);
    return "prueba";
  }
  if ((Number(lista.script_minimo) || 1) > VERSION) {
    await avisarUnaVezAlDia("version", "Vigía: actualiza el script", "Vigía → Inversión automática → «Copiar el script» y pégalo en Scriptable sobre «Vigia Compra».");
    return "version";
  }
  const importe = importeDelMes(conf, mes);
  if (!(importe > 0)) {
    est.compras[id] = { estado: "saltado", fecha: hoy() };
    guardarJSON("estado.json", est);
    await avisar(`Vigía: ${nombreMes(mes)} sin compra`, "Este mes lo tienes a 0 € en Vigía: no se compra nada.");
    return "cero";
  }
  const auth = Keychain.contains(claveLlavero(ent)) ? Keychain.get(claveLlavero(ent)) : null;
  if (!auth) {
    await avisarUnaVezAlDia("sinclave:" + ent, "Vigía: falta la clave de Trading 212", `Abre Scriptable → «Vigia Compra» → «Poner o cambiar la clave» (${NOMBRE_ENTORNO[ent]}).`);
    if (manual) await pedirClave(ent);
    return "sinclave";
  }
  const plan = planCompra(lista, conf, mes);
  if (!plan.ordenes.length) {
    est.compras[id] = { estado: "saltado", fecha: hoy() };
    guardarJSON("estado.json", est);
    await avisar(`Vigía: ${nombreMes(mes)} sin compra`, "No hay nada que comprar este mes (secciones en pausa o en el tope). El dinero se queda en la cuenta.");
    return "nada";
  }
  if (manual) {
    const r = await dialogo("¿Comprar ahora?", resumenPlan(plan, lista, ent), ["Comprar"]);
    if (r !== 0) return "cancelado";
  }
  // La marca se guarda ANTES de enviar: si algo se corta a mitad, nunca se compra dos veces.
  est.compras[id] = { estado: "enviando", fecha: hoy(), inicio: new Date().toISOString() };
  guardarJSON("estado.json", est);
  const resultados = [];
  let corte = null;
  for (const o of plan.ordenes) {
    if (corte) { resultados.push({ ...o, ok: false, motivo: corte }); continue; }
    let res = await enviarOrden(ent, auth, o);
    if (!res.ok && res.status === 429) { await esperar(3000); res = await enviarOrden(ent, auth, o); }
    if (!res.ok && (res.status === 401 || res.status === 403)) corte = `Trading 212 rechaza la clave (${res.status})`;
    resultados.push({ ...o, ...res });
  }
  const buenas = resultados.filter((r) => r.ok);
  const dudosas = resultados.filter((r) => !r.ok && r.dudosa);
  const malas = resultados.filter((r) => !r.ok && !r.dudosa);
  if (!buenas.length && !dudosas.length) {
    delete est.compras[id];               // no se compró nada: se reintenta en la próxima ejecución
    guardarJSON("estado.json", est);
    const motivo = corte || (malas[0] && malas[0].motivo) || "error desconocido";
    await avisar("Vigía: no se pudo comprar", `${motivo}. Se volverá a intentar en la próxima ejecución.`);
    return "fallo";
  }
  const compra = { id, mes, entorno: ent, fecha: hoy(),
                   compras: buenas.map((r) => ({ t: r.empresa, q: r.cantidad, p: r.precio_usd, s: r.seccion })) };
  est.compras[id] = { estado: "hecho", fecha: hoy(), compra,
                      resultados: resultados.map((r) => ({ empresa: r.empresa, cantidad: r.cantidad, ok: !!r.ok, motivo: r.motivo || null })) };
  guardarJSON("estado.json", est);
  const gastado = buenas.reduce((s, r) => s + r.eur, 0);
  let cuerpo = `Compradas ${buenas.length} de ${resultados.length} · ≈ ${euros(gastado)} (${NOMBRE_ENTORNO[ent]}).`;
  if (malas.length) cuerpo += ` Fallaron: ${malas.map((r) => `${r.empresa} (${r.motivo})`).join("; ")}.`;
  if (dudosas.length) cuerpo += ` Revisa en Trading 212: ${dudosas.map((r) => r.empresa).join(", ")}.`;
  cuerpo += ent === "live" ? " Toca para apuntarlas en Vigía." : " (Prueba: no hace falta apuntarla.)";
  await avisar(`Vigía: compra de ${nombreMes(mes)}`, cuerpo, URL_COPIAR);
  return "hecho";
}

// ───────────── otras acciones ─────────────
async function pedirClave(entPreferido) {
  let ent = entPreferido;
  if (!ent) {
    const r = await dialogo("¿Qué clave?", "Una clave para cada cuenta de Trading 212.", ["Cuenta real", "Cuenta Practice (prueba)"]);
    if (r < 0) return;
    ent = r === 0 ? "live" : "demo";
  }
  const a = new Alert();
  a.title = `Clave de Trading 212 · ${NOMBRE_ENTORNO[ent]}`;
  a.message = "Trading 212 → Settings → API (Beta) → generar clave con el permiso «Orders: execute». Pega aquí la clave y el secreto. Se guardan solo en el llavero de este iPhone.";
  a.addSecureTextField("Clave (API key)", "");
  a.addSecureTextField("Secreto (API secret)", "");
  a.addAction("Guardar");
  a.addCancelAction("Cancelar");
  if ((await a.present()) !== 0) return;
  const k = String(a.textFieldValue(0) || "").trim(), s = String(a.textFieldValue(1) || "").trim();
  if (!k || !s) { await dialogo("Falta algo", "Hacen falta la clave y el secreto.", ["OK"], false); return; }
  Keychain.set(claveLlavero(ent), Data.fromString(k + ":" + s).toBase64String());
  await dialogo("Clave guardada ✓", `Guardada para la ${NOMBRE_ENTORNO[ent]}.`, ["OK"], false);
}

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
  if (!Keychain.contains(claveLlavero(ent))) await pedirClave(ent);
  await dialogo("Guardado ✓", "Ya puedes volver a Vigía (arriba a la izquierda).", ["OK"], false);
  return "guardada";
}

async function copiarUltima() {
  const est = leerJSON("estado.json", {});
  const hechas = Object.values(est.compras || {}).filter((x) => x && x.estado === "hecho" && x.compra && x.compra.entorno === "live");
  hechas.sort((a, b) => String(a.compra.mes).localeCompare(String(b.compra.mes)));
  const ult = hechas[hechas.length - 1];
  if (!ult) { await dialogo("Nada que copiar", "Todavía no hay ninguna compra con la cuenta real.", ["OK"], false); return "nada"; }
  Pasteboard.copy(PREFIJO_COMPRA + b64url(ult.compra));
  await dialogo("Copiado ✓", `${ult.compra.compras.length} compras de ${nombreMes(ult.compra.mes)}. Abre Vigía → Este mes → «Pegar compra del iPhone».`, ["OK"], false);
  return "copiada";
}

async function simular() {
  const conf = leerConfig();
  if (!conf) { await dialogo("Falta configurar", "Abre Vigía → Inversión automática → «Guardar en el iPhone».", ["OK"], false); return "sinconfig"; }
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
  if (previa) aviso += " Este mes ya está hecho: no se volvería a comprar.";
  if (!Keychain.contains(claveLlavero(ent))) aviso += ` Falta la clave de la ${NOMBRE_ENTORNO[ent]}.`;
  await dialogo("Simulación", aviso + "\n\n" + resumenPlan(plan, lista, ent), ["OK"], false);
  return "simulada";
}

async function verEstado() {
  const conf = leerConfig();
  const est = leerJSON("estado.json", {});
  const filas = Object.entries(est.compras || {}).sort().slice(-6).map(([k, x]) => {
    const [ent, mes] = k.split(":");
    const n = x.resultados ? `${x.resultados.filter((r) => r.ok).length}/${x.resultados.length}` : "";
    return `${nombreMes(mes)} · ${ent === "demo" ? "prueba" : "real"} · ${x.estado} ${n}`;
  });
  const claves = ["live", "demo"].map((e) => `${NOMBRE_ENTORNO[e]}: ${Keychain.contains(claveLlavero(e)) ? "clave guardada" : "sin clave"}`);
  await dialogo(`Vigía · script v${VERSION}`, [conf ? describirConfig(conf) : "Sin configurar.", "", ...claves, "", ...(filas.length ? filas : ["Sin compras todavía."])].join("\n"), ["OK"], false);
  return "estado";
}

async function permitirOtra() {
  const conf = leerConfig();
  const ent = entornoDe(conf);
  const id = ent + ":" + mesDe(new Date());
  const est = leerJSON("estado.json", {});
  if (!est.compras || !est.compras[id]) { await dialogo("Nada que borrar", "Este mes aún no hay compra registrada.", ["OK"], false); return "nada"; }
  const r = await dialogo("¿Permitir otra compra este mes?", `Se olvida la compra de este mes (${NOMBRE_ENTORNO[ent]}) y la próxima ejecución volverá a comprar. Úsalo solo si de verdad no se compró nada.`, ["Sí, olvidarla"]);
  if (r !== 0) return "cancelado";
  delete est.compras[id];
  guardarJSON("estado.json", est);
  return "borrada";
}

async function menu() {
  const opciones = ["Comprar ahora", "Simular (sin comprar)", "Copiar la última compra para Vigía", "Poner o cambiar la clave", "Ver estado", "Permitir otra compra este mes"];
  const r = await dialogo(`Vigía · compra automática (v${VERSION})`, "Normalmente no hace falta tocar nada: la automatización de Atajos lo lanza cada día.", opciones);
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
  if (accion === "simular") return simular();
  if (accion === "copiar") return copiarUltima();
  if (accion === "clave") return pedirClave(q.entorno === "demo" ? "demo" : q.entorno === "live" ? "live" : null);
  if (accion === "comprar") return comprar(true);
  return menu();
}

const VIGIA = { VERSION, planCompra, repartir, importeDelMes, normalizarReparto, validarConfig, b64url, deB64url, main };
if (typeof module !== "undefined") module.exports = VIGIA;
if (!(typeof VIGIA_SIN_EJECUTAR !== "undefined" && VIGIA_SIN_EJECUTAR)) {
  await main();
  Script.complete();
}
