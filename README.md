# Inversor LP — análisis de acciones con horizonte 3-10 años

App personal de análisis fundamental. Embudo de 3 capas para que el 85-90 % del trabajo
lo haga código gratuito y la IA solo toque lo que merece la pena.

```
       ~3.000 tickers
            │
 CAPA 1 · DATOS ─────────── SEC EDGAR (XBRL, gratis) + Finnhub (precio)
            │               caché 90 días en Supabase
            ▼
 CAPA 2 · FILTRO ────────── vetos de solvencia/calidad + score 0-100
            │               coste: 0 tokens
            ▼  ~300 tickers
 CAPA 2b · MACRO ────────── ¿el SECTOR sobrevive 10 años y quién captura el valor?
            │               cacheado por sector: ~35 llamadas, no 300
            ▼
 CAPA 3 · IA ───────────── análisis fundamental de la empresa
            │               payload ~210 tok · Gemini (nivel gratuito)
            ▼
      combine(macro, fundamental) → veredicto final → Supabase → app
```

**LLM: Gemini, nivel gratuito.** `backend/llm_gemini.py` es el único punto que habla
con la IA; `pipeline.py` y `macro_layer.py` lo usan por debajo. Clave gratuita, sin
tarjeta, en https://aistudio.google.com/apikey. El nivel gratuito tiene límites de
peticiones por minuto y por día que Google ajusta de vez en cuando — si te sale un
error 429, espera un minuto o analiza menos empresas de golpe.

## Ficheros

| Fichero | Qué es |
|---|---|
| `prompts/system_prompt_es.md` | **El system prompt definitivo.** Rol, mandato temporal, 4 pilares, vetos, escala de veredicto, formato de salida. |
| `prompts/user_payload_format.md` | Formato compacto del bloque `<DATOS>` + presupuesto de tokens. |
| `prompts/macro_prompt_es.md` | **Prompt del estratega macro.** Cinco fuerzas estructurales, captura de valor, reloj de disrupción, escala 1-10 calibrada. |
| `backend/macro_layer.py` | Capa 2b. Caché por sector, parseo y regla `combine()` macro × fundamental. |
| `db/002_macro.sql` | Migración aditiva: `macro_analyses`, veredicto final y vistas. |
| `backend/providers.py` | Capa 1. EDGAR XBRL y Finnhub normalizados a `Fundamentals`. |
| `backend/quant_filter.py` | Capa 2. Métricas derivadas, vetos, score y generador del payload. |
| `backend/llm_gemini.py` | Único punto que llama a la IA (Gemini, nivel gratuito). |
| `backend/pipeline.py` | Orquestador con caché, llamada al LLM y parseo del informe. |
| `backend/probar_ia.py` | Prueba el embudo completo **sin base de datos**. Empieza por aquí. |
| `backend/universe.py` | Capa 0. Listado completo de tickers EDGAR (~10.000, no solo S&P 500). |
| `backend/run_universo.py` | **Barrido de descubrimiento.** Recorre el universo por lotes, cribado en paralelo. |
| `backend/refrescar.py` | **Refresco de cartera.** Re-verifica lo ya analizado, por prioridad. Ver sección de abajo. |
| `backend/vigilar.py` | Vigilancia diaria de precio (sin IA) de las empresas marcadas VIGILAR. |
| `.github/workflows/` | Los tres workflows que lo hacen correr solo: `barrido.yml`, `refresco.yml`, `vigilancia.yml`. |
| `docs/index.html` | Dashboard móvil ("Vigía"), servido por GitHub Pages, lee Supabase con la clave `anon`. |
| `QUICKSTART.md` | Guía paso a paso para ponerlo en marcha en tu ordenador. |
| `db/schema.sql` | Esquema Supabase con RLS, vistas y dedupe por `data_hash`. |

## Puesta en marcha

```bash
pip install requests supabase
export SEC_USER_AGENT="TuNombre/1.0 (tu@email.com)"   # obligatorio para la SEC
export FINNHUB_API_KEY="..."
export GEMINI_API_KEY="..."                           # gratis en aistudio.google.com/apikey
export SUPABASE_URL="..." SUPABASE_SERVICE_KEY="..."

psql "$SUPABASE_DB_URL" -f db/schema.sql

python backend/quant_filter.py MSFT KO F     # solo cribado, sin IA ni base de datos
python backend/pipeline.py MSFT              # embudo completo
python backend/macro_layer.py                # demo de la regla de combinación
```

## Automatización 24/7 (GitHub Actions)

Una vez desplegado, tres workflows independientes lo mantienen vivo sin que tengas que
tocar nada:

| Workflow | Qué hace | Cuándo |
|---|---|---|
| `barrido.yml` | **Descubrimiento.** Primero las ~2.900 empresas de índices (S&P 500/400/600, Russell 2000, Nasdaq-100; repasadas cada 45 días), luego el resto del universo EDGAR (~10.000 tickers). Lotes de 400; lo que pasa el cribado y no cabe en el cupo de IA queda en cola para la siguiente ejecución. | cada 2 horas (minuto 17) |
| `refresco.yml` | **Mantenimiento.** Vuelve a pasar por el embudo a las empresas que YA tienen un análisis guardado, empezando por COMPRA_FUERTE/COMPRA/VIGILAR y por las más antiguas. Casi siempre gratis (caché). | a diario (07:23 UTC) |
| `vigilancia.yml` | Comprueba el precio de las VIGILAR y abre un Issue si alguna cruza su precio de entrada. | cada 2 horas |

**Qué limita cuántas empresas se analizan (nov-2026).** No es la velocidad ni la IA: el barrido
ya había visto el 93 % de las empresas de índices, pero solo ~1 de cada 10 pasaba el cribado de
calidad. Para ampliar: el mínimo de capitalización bajó de 1.000 a **300 M$** (`quant_filter.py`,
ruleset `quant-1.3`; no invalida análisis guardados), y las **pequeñas (300–5.000 M$) que pasan el
filtro Peter Lynch** llegan a la IA aunque no pasen el cribado de calidad (`pipeline.ENTRADA_LYNCH`;
ver más abajo por qué solo las pequeñas). `python run_universo.py --estado` (o el input
`solo_estado` del workflow) muestra cómo va: universo visto, empresas de índices pendientes y cola
de IA. Las ejecuciones programadas «en punto» se las saltaba GitHub (salía ~1 de 6): por eso van en
el minuto 17.

**Por qué dos barridos distintos (`barrido.yml` y `refresco.yml`) en vez de uno solo.**
El descubrimiento recorre ~10.000 tickers por turnos: a una empresa ya analizada podría
no tocarle de nuevo hasta dentro de semanas, según cuántas haya por delante en la cola.
Eso vale para encontrar candidatas nuevas, pero no para mantener al día lo que ya te
interesa. `refresco.yml` es una cola aparte, mucho más corta (solo lo ya analizado) y
mucho más frecuente — así una tesis en cartera no se queda nunca "congelada" durante
semanas, y si una empresa deja de pasar el cribado cuantitativo (se ha endeudado, el
margen se ha deteriorado...) te enteras en menos de un día, no cuando le vuelva a tocar
turno en el barrido grande.

**Cómo comprobar que el aviso de `vigilancia.yml` te llega de verdad.** Pestaña
*Actions* → *Vigilancia de precio* → *Run workflow* → marca `prueba` → *Run workflow*.
Se abre un Issue de prueba al momento (sin tocar Supabase ni Finnhub), asignado a ti y
con la etiqueta `alerta-compra`. Si te llega el aviso (correo, notificación en el móvil
con la app de GitHub...), el mecanismo funciona. Si no te llega nada, revisa tu
configuración de notificaciones en https://github.com/settings/notifications — en
particular que "Issues" tenga marcado algún canal (correo y/o web/móvil) para
"Participating, @mentions" como mínimo (el Issue te menciona y te asigna, así que con
eso ya basta).

**Las claves de Gemini, de proyectos de Google Cloud DISTINTOS, no solo claves
distintas.** El límite del nivel gratuito es por *proyecto*, no por clave: si tus 3
claves salieron del mismo proyecto en Google AI Studio, en la práctica sigues teniendo
el cupo de una sola, y `GEMINI_API_KEYS` no te da ninguna velocidad extra — solo un
respaldo si una clave se queda sin cupo momentáneamente. Para que de verdad se reparta
el trabajo entre las tres, cada una tiene que venir de un proyecto distinto (en
https://aistudio.google.com/apikey, cambia de proyecto arriba antes de crear cada
clave nueva).

## Filtro por mercado en el Vigía

En la vista **Cartera** del Vigía, debajo de *Todas / Comprar / Vigilar / Descartadas*, hay una
fila para filtrar por mercado: **S&P 500, Nasdaq-100, Russell 2000, S&P 400, S&P 600, bolsa
Nasdaq, bolsa NYSE** y **Fuera de índices**. Se combina con el veredicto y con la búsqueda, cada
botón muestra cuántas empresas hay, el elegido se recuerda, y cada tarjeta lleva sus etiquetas
de índice y bolsa.

- Las listas salen de `docs/mercados.json`, que el workflow `mercados.yml` actualiza **cada lunes**
  (o a mano: *Actions → Listas de mercados (filtro del Vigía) → Run workflow*). Fuentes: S&P 500
  (datasets/s-and-p-500-companies), Nasdaq-100 (Wikipedia o nasdaq.com), **Russell 2000 = posiciones
  del ETF IWM de iShares** (respaldo: VTWO de Vanguard), S&P 400 y 600 (Wikipedia), bolsa (SEC).
  Si una fuente falla, se conserva la lista anterior con su fecha.
- El log de ese workflow incluye cuántas COMPRA FUERTE / COMPRA / VIGILAR hay en cada mercado.
- **Ojo con el Russell 2000:** el cribado del bot exige más de 1.000 M$ de capitalización
  (`quant_filter.py`, `min_market_cap`), así que solo llegan a analizarse las small caps más
  grandes del índice (que va de ~150 M$ a ~5.700 M$).

## Filtro Peter Lynch

`backend/lynch.py` lleva a números las reglas de *One Up on Wall Street* (1989), con los umbrales
de dos codificaciones públicas de su método (Validea, modelo «P/E/Growth Investor»; AAII, «Lynch's
One Up on Wall Street-Inspired Screening Strategy»):

- **Categoría** por crecimiento del beneficio por acción (BPA), sin ningún año de pérdidas:
  *rápida* 20–50 %/año (más de 50 % no dura); *sólida* 10–20 % con ventas ≥ 1.000 M$; *lenta*
  < 10 % solo con ventas ≥ 1.000 M$ y dividendo ≥ 3 %. Cíclicas, *turnarounds* y *asset plays*
  necesitan juicio: no se eligen aquí.
- **PEG ≤ 1** (PER ÷ crecimiento; 0,5 = ganga). En sólidas y lentas, PER ÷ (crecimiento + dividendo).
  Una rápida ya grande, además PER ≤ 40.
- **Deuda/patrimonio ≤ 80 %**; en bancos y aseguradoras, patrimonio/activos ≥ 5 % y ROA ≥ 1 %.
- **Inventario** que no crece más que las ventas (+5 puntos como mucho).
- Puntos 0–100 para ordenar: sobre todo el PEG; luego deuda, caja neta, caja libre, recompras.
- Lo que Lynch también miraba y **no hay en datos gratuitos**: compras de directivos, % en manos
  de fondos, nº de analistas y, sobre todo, la «historia» del negocio. Eso queda para la IA y para ti.

El bot guarda la evaluación con cada cribado (`quant_screens.breakdown → lynch`). Con la
migración `db/007_lynch.sql` aplicada, el Vigía la enseña en cada empresa («✓ Pasa el filtro Lynch»
o por qué no) y añade el botón **✓ Lynch** para filtrar.

### La prueba histórica del filtro (`prueba-lynch.yml`)

Cada fin de mes desde 2015, con las cuentas **tal como se conocían** (SEC) y ~2.900 empresas (las
de los índices de hoy), se eligen las 15 con más puntos (máx. 3 por sector) en dos grupos según su
tamaño ese día: **grandes** (> 5.000 M$, frente al S&P 500) y **pequeñas** (300–5.000 M$, frente al
Russell 2000). Misma regla fijada de antemano que los paquetes: vale solo si a 12 y 24 meses bate al
índice y a elegir sin criterio en el mismo grupo.

| Variante | Grupo | Exceso vs índice (12 / 24 m) | Exceso vs grupo (12 / 24 m) | Aciertan* (12 / 24 / 36 m) | Veredicto |
|---|---|---|---|---|---|
| Lynch | Pequeñas | +6,0 / +13,6 pp | +1,8 / +3,9 pp | 53 / 56 / 55 % (sin criterio 48 / 47 / 47 %) | ✅ |
| **Lynch + cribado de calidad** | **Pequeñas** | **+5,9 / +14,9 pp** | **+1,7 / +5,3 pp** | **54 / 58 / 60 %** | ✅ **activo** |
| Lynch | Grandes | −0,7 / −2,3 pp | +0,7 / +1,2 pp | 43 / 42 / 36 % (sin criterio 44 / 41 / 38 %) | ❌ |
| Lynch + cribado de calidad | Grandes | −3,7 / −8,9 pp | −2,2 / −5,4 pp | 39 / 38 / 35 % | ❌ |

\* % de compras que batieron a su índice en ese plazo («si eres bueno, aciertas seis de cada diez», Lynch).

- En pequeñas, «Lynch + cribado» a 3 años: el 22 % de las compras duplicó (sin criterio, 18 %) y solo
  el 2 % perdió más de la mitad (sin criterio, 10 %). Por épocas a 12 m: 2015-19 9,5 % (Russell 7,1 %,
  grupo 9,7 %), 2020-22 24,7 % (12,2 / 23,1 %), 2023+ 22,2 % (16,9 / 17,2 %).
- Entre las dos variantes que aprobaron en pequeñas se activa «Lynch + cribado»: mismos excesos,
  algo más de acierto y la mitad de desastres. Es una elección hecha viendo los resultados: el
  seguimiento en vivo dirá si se sostiene.
- Los intervalos de confianza del exceso **frente al grupo** a 12 m incluyen el cero: el filtro bate
  con claridad al Russell 2000, pero su ventaja sobre elegir sin criterio entre pequeñas de calidad
  es modesta. Faltan además las que quebraron (sesgo a favor).
- **Fallo corregido (nov-2026):** el «Close» de Yahoo viene ajustado por splits posteriores; las
  cuentas de la SEC usan las acciones de entonces. Sin corregir, una empresa que luego hizo un split
  de 10:1 parecía 10 veces más pequeña y barata (sesgo hacia las futuras ganadoras). La primera
  versión de esta prueba daba aprobado también en grandes; corregida (`pit.factor_splits`), no.

## Módulo mensual de oportunidades (experimental)

Complemento del bot de largo plazo, pensado para ir **mes a mes**: tú decides cuánto dinero
metes (a mano, en tu bróker) y la herramienta te dice **en qué acciones**. Es un modelo
**solo de acciones**: sin ETF de relleno; si un mes nada cumple, el dinero espera.

| Paquete | Qué busca | Estado |
|---|---|---|
| **Lynch pequeñas** | Empresas de 300–5.000 M$ que pasan el filtro Peter Lynch **y** el cribado de calidad del bot, con el PEG recalculado al precio del día. Hasta 15, máx. 3 por sector. Frenos: la IA no la ha descartado, precio dentro del rango de valor de su tesis, último trimestre sin hundirse. Se compara con el Russell 2000. | **Activo, en prueba** (ver arriba). |
| **Dividendos** · Small caps | Small caps del Russell 2000 (posiciones de IWM; si iShares falla, S&P 600 + 400) que pagan dividendo regular **sin recortarlo** (≥ 5 años pagando), con caja que lo cubre, poca deuda y sin diluir. Hasta 8, 2 por sector; frente al Russell 2000. | **Activo.** No se ha podido probar en el pasado. |
| ~~1–3 años~~ · Descuento con tesis | Baratas frente a su propio rango de 5 años. | Retirado |
| ~~3–5 años~~ · Calidad a precio razonable | ROIC ≥ 12 %, crecimiento ≥ 3 %, caja, precio razonable. | Retirado (con splits corregidos) |
| ~~5+ años~~ · Compounders | ROIC medio ≥ 15 %, crecimiento ≥ 6 %… | Retirado |
| ~~Lynch grandes~~ | El filtro Lynch en > 5.000 M$. | Retirado (no bate al S&P 500) |

Los paquetes retirados siguen codificados (`backend/mensual/paquetes.py`): se reactivan añadiéndolos
a `ACTIVOS`. Las operaciones que ya tengas anotadas con ellos se conservan en el Vigía, marcadas
«retirado». En «Mi cartera», una compra de Lynch pequeñas avisa si deja de cumplir las reglas por
algo que no es el precio (beneficios, deuda, inventario) o si ya está cara (PEG > 1).

### La prueba histórica de los paquetes (point-in-time) y su veredicto

`prueba-paquetes.yml` reconstruye cada fin de mes desde 2015 lo que habría elegido cada paquete con
las cuentas **tal como se conocían en esa fecha** y el mismo código del bot (S&P 500 de hoy). Regla
fijada antes de ver los resultados: un paquete se mantiene solo si a 12 **y** 24 meses supera al
S&P 500 **y** al listado a partes iguales. Resultado **con el precio corregido por splits**:

| Horizonte | S&P 500 | Listado a partes iguales | Solo las que pasan el cribado |
|---|---:|---:|---:|
| 12 m | 14,9 % | 18,6 % | 16,7 % |
| 24 m | 32,0 % | 40,1 % | 36,5 % |
| 36 m | 50,1 % | 63,1 % | 59,1 % |

| Paquete | Exceso vs S&P 500 (12 / 24 / 36 m) | Exceso vs listado (12 / 24 / 36 m) | Veredicto |
|---|---|---|---|
| 1–3 años | +6,5 / +0,6 / +11,6 pp | +2,4 / **−8,4** / −3,1 pp | ❌ |
| 3–5 años | +2,0 / +5,2 / +2,2 pp | **−1,7** / **−2,9** / −10,8 pp | ❌ (antes del arreglo, aprobaba por poco) |
| 5+ años | +3,4 / +11,0 / +14,1 pp | **−0,3** / +2,9 / +1,1 pp | ❌ |

El propio cribado de calidad rindió **menos** que el listado entero (−1,8 / −3,6 / −4,0 pp), aunque
batió al S&P 500. En el S&P 500 ninguna regla basada en las cuentas ha demostrado elegir mejor que
no elegir; donde sí aparece ventaja es en las pequeñas con el filtro Lynch.

### Qué dijo la prueba anterior (precio, 2012-2026)

Con 504 empresas del S&P 500 (impuestos y comisiones incluidos), la regla de precio del antiguo
paquete de 1–3 años **no pasó** los criterios fijados antes de ver los resultados:

| | Rentab. anual tras impuestos |
|---|---:|
| Comprar el bajón y **vender cuando está cara** | 11,3 % |
| Aportar solo al S&P 500 | 12,7 % |
| Aportar a partes iguales a todas las empresas del universo (control de supervivencia) | 15,4 % |
| Misma estrategia eligiendo al azar (mediana) | 12,1 % |

- **Las tres reglas de venta por precio pierden** frente al S&P 500. Por eso el módulo **no manda
  vender por precio**: solo marca «muy cara» a título informativo. Se vende, si acaso, por tesis rota.
- La única variante que ganaba de largo («no vender nunca», 27,5 %) depende de **una sola empresa
  (AMD: 72 % de todas las ganancias)**. Sin ella baja a 16,0 %, prácticamente lo mismo que el control
  de supervivencia (15,4 %). Es sesgo de supervivencia, no una ventaja demostrada.
- Conclusión honesta: **no hay evidencia de que las reglas de compra batan al índice.** El módulo trae un
  **marcador** que compara tu resultado real con poner exactamente los mismos flujos de dinero en el
  S&P 500. Es el juez: dale 12-24 meses con importes que puedas permitirte, y si no gana, abandónalo.

### Cómo funciona cada mes

1. `mensual.yml` (día 1 de cada mes, o a mano) ejecuta `python -m mensual.recomendador --sin-ia`.
   Ya **no usa Gemini ni Finnhub** (la IA solo servía al paquete de 1–3 años).
2. Carga el universo desde Supabase (último cribado de cada empresa, con sus métricas y su evaluación
   Lynch) y descarga sus precios (Yahoo, gratis).
3. **Lynch pequeñas:** rehace el PEG con el precio del día (`lynch.con_precio`), se queda con las de
   300–5.000 M$ que además pasan el cribado de calidad y ordena por puntos; máx. 15, 3 por sector.
   **Dividendos** (`con_dividendos`, activado por defecto): módulo aparte sobre small caps (ver arriba).
4. **Frenos que solo pueden quitar o avisar, nunca añadir:** precio por encima del rango de valor de la
   tesis; chequeo del último trimestre publicado en la SEC (ingresos y margen interanuales). Las
   empresas que el bot de largo plazo ya descartó (NO_INVERTIBLE, DESCARTE) no entran.
5. **Seguimiento automático** (solo paquetes activos): calcula cuánto habrían rendido las elecciones de
   los informes anteriores (el primero de cada mes, para que no se pueda repetir hasta que salga uno
   bonito) frente al S&P 500 y frente al «resto del universo» (todas las elegibles de ese mes a partes
   iguales); Lynch pequeñas y Dividendos, frente al Russell 2000.
6. Guarda el informe en Supabase (`mensual_informes`) y abre un Issue con las elegidas.
7. La pestaña **Mensual** del Vigía lee ese informe: eliges paquete, escribes cuánto vas a invertir y
   reparte a partes iguales con tope por empresa (lo que no cabe NO se fuerza: se guarda para el mes
   siguiente). Las compras se etiquetan con su paquete y ves el resultado por horizonte.

> **Honestidad sobre «encontrar ganadoras».** Lo único que ha aprobado la prueba histórica es el
> filtro Lynch en pequeñas, y la prueba tiene sesgo a favor (faltan las que quebraron). Se mantiene
> **en prueba**: el juez es el seguimiento en vivo (12–24 meses de cohortes mensuales) y tu marcador
> frente al S&P 500. Si no gana, el índice es la respuesta.

### Privacidad: tu cartera nunca sale de tu móvil

Este repositorio es público, así que el informe, el Issue y los logs contienen **solo datos de
empresas**. Tus compras, importes y posiciones se guardan únicamente en el `localStorage` de tu
navegador (pestaña Mensual → «Registrar operación»), con botones de exportar/importar copia. Allí
mismo se calculan, sin enviar nada: el reparto de tu importe, el estado de cada posición (tesis
rota, peso, revisión a los 24 meses), el impuesto estimado de vender (FIFO, aproximado), el aviso de
la regla de los 2 meses y el marcador frente al S&P 500.

### Ficheros

| Fichero | Qué es |
|---|---|
| `backend/mensual/estrategia.py` | Las reglas (señales, candidatas, reparto, venta) como funciones puras; las usan el backtest y el recomendador. |
| `backend/mensual/backtest.py` | Motor del backtest, controles (S&P 500, supervivencia, azar), veredicto, anexo de robustez. |
| `backend/mensual/precios.py` | Descarga de precios (Yahoo Finance, gratis) y universo S&P 500 con su sector. |
| `backend/mensual/recomendador.py` | El informe mensual (universo, paquetes activos, frenos, seguimiento) y el Markdown del Issue. |
| `backend/mensual/paquetes.py` | Reglas, puntuación y textos de los paquetes; `ACTIVOS` dice cuáles se ofrecen (hoy solo Lynch pequeñas). Funciones puras, sin red. |
| `backend/mensual/pit.py` | Cuentas de la SEC «tal como se conocían» en cada fecha (point-in-time) para probar los paquetes en el pasado. |
| `backend/mensual/prueba_calidad.py` | La prueba histórica de los paquetes y su veredicto (regla fijada de antemano). |
| `backend/mensual/dividendos.py` | Sección de small caps con dividendo constante (S&P 600 + S&P 400) y su seguimiento frente al Russell 2000. |
| `backend/mensual/seguimiento.py` | Seguimiento: cuánto habrían rendido las elecciones pasadas frente al S&P 500 y al universo. |
| `backend/mensual/salud.py` | Chequeo del último trimestre (EDGAR, gratis). |
| `backend/mensual/explicacion.py` | «¿Por qué ha caído?»: titulares + Gemini (solo lo usaba el paquete 1–3 años, hoy retirado). |
| `db/006_mensual.sql` | Tabla del informe y vista pública de solo lectura (aplicar en el SQL Editor de Supabase). |
| `docs/mensual.js`, `docs/mensual-ui.js` | Lógica pura y pantalla de la pestaña Mensual. |
| `.github/workflows/mensual.yml` | Informe mensual (día 1) o a mano. |
| `.github/workflows/prueba-paquetes.yml` | Prueba histórica de los paquetes con cuentas de cada fecha (a mano; informe en *Summary*). |
| `backend/lynch.py` | Filtro Peter Lynch (puro, sin red): categoría, PEG, deuda, inventario, puntos; `con_precio` lo rehace con el precio de hoy. |
| `backend/mensual/prueba_lynch.py` | Prueba histórica del filtro Lynch (grandes vs S&P 500, pequeñas vs Russell 2000, % de aciertos). |
| `.github/workflows/prueba-lynch.yml` | Lanza esa prueba a mano (informe en *Summary*). |
| `db/007_lynch.sql` | Añade la evaluación Lynch a la vista pública del Vigía (aplicar en el SQL Editor de Supabase). |
| `backend/mensual/mercados.py` | Listas de índices y bolsas para el filtro por mercado del Vigía (`docs/mercados.json`). |
| `.github/workflows/mercados.yml` | Actualiza esas listas cada lunes. |
| `.github/workflows/dividendos.yml` | Sección de dividendos suelta (a mano), con opción de depurar empresas concretas. |
| `.github/workflows/backtest-mensual.yml` | Repite la prueba histórica a mano (informe en *Summary*; zip en *Artifacts*). |

**Para activarlo:** (1) pega `db/006_mensual.sql` en Supabase → SQL Editor → Run; (2) pestaña
*Actions* → *Recomendador mensual* → *Run workflow*; (3) abre el Vigía → *Mensual*.

**Fuente de precios.** Yahoo Finance vía `yfinance`: gratis y con ~20 años de histórico, pero no es
oficial y a veces bloquea las IPs de GitHub Actions. Si pasa, no se genera informe y el log lo dice
(no sigue con datos a medias). Funcionó bien en la prueba del 2-nov-2026.

**Límites que conviene recordar.** Precios y cifras en dólares (sin cambio a euros); impuesto con tipo
único del 21 % (editable en la pestaña); la regla de los 2 meses solo avisa. Información automática,
no asesoramiento financiero ni fiscal.

## Seis decisiones de diseño que conviene entender

**1. El `data_hash` excluye el precio.** Una tesis a 10 años no cambia porque la acción
suba un 5 %: solo cambia cuando la empresa publica cuentas nuevas. Por eso el hash se
calcula sobre las métricas sin múltiplos, y un análisis vale hasta el siguiente 10-K.
Es lo que evita repetir llamadas caras.

**2. El system prompt va cacheado, el payload no.** Los ~2.100 tokens del rol son
idénticos en todas las llamadas → prompt caching. Los ~210 del bloque `<DATOS>` cambian
y se pagan enteros. De ahí el formato con `|` en lugar de tablas o JSON.

**3. El precio pesa poco en el score cuantitativo (15 %).** El filtro decide *si merece
la pena mirar la empresa*, no *si está barata*. La valoración fina la hace la IA, que
puede razonar sobre normalización de beneficios y calidad del crecimiento.

**4. Las empresas financieras se marcan, no se vetan.** Deuda/Patrimonio y ROIC no
significan lo mismo en un banco. `is_financial` desactiva esos vetos y añade un aviso
para que la IA sepa que necesita otro marco.

**5. El macro se cachea por sector, no por empresa.** IAG, Lufthansa y Air France
comparten veredicto macro. Y el macro **acota** al fundamental en vez de sumarse:
a 10 años el sector explica más dispersión de retorno que la calidad relativa de una
empresa dentro de él. Una empresa excelente en un sector en contracción acaba siendo
mediocre.

**6. Los umbrales están versionados.** `RULESET_VERSION` y `PROMPT_VERSION` se guardan
con cada resultado. Si cambias un criterio, sabes exactamente qué análisis quedaron
obsoletos sin tener que borrar la base de datos.

## Limitaciones conocidas

- La serie de ROIC usa el capital invertido actual para todos los años. Sirve para ver
  la tendencia, no como cifra histórica exacta. Si quieres precisión, extrae balance por
  año fiscal en `SecEdgarProvider._annual_series(kind="stock")` y calcúlalo año a año.
- EBITDA se aproxima como EBIT × 1,25 (no hay D&A explícita en todos los `companyfacts`).
  Para deuda neta/EBITDA fina, añade `DepreciationDepletionAndAmortization`.
- Solo cubre emisores estadounidenses (EDGAR). Para Europa harían falta datos de otra
  fuente; el resto de la arquitectura no cambia.
- Las etiquetas XBRL varían entre empresas. `CONCEPTS` prueba varias por concepto, pero
  para casos raros tendrás que añadir la etiqueta a la lista.
- (Corregido) `SecEdgarProvider` creaba una instancia nueva por ticker y volvía a
  descargar el listado completo de EDGAR (varios MB) en cada una — un barrido de 100
  tickers lo descargaba 100 veces. Ahora hay una caché a nivel de proceso compartida por
  todas las instancias (ver `providers.py`), protegida con un lock porque el cribado
  ahora corre en paralelo.

## Nota

Esto es una herramienta de análisis, no asesoramiento financiero. El informe que produce
la IA es una hipótesis estructurada, no una recomendación: los supuestos de valoración
son suyos y pueden estar equivocados. Las decisiones son tuyas.
