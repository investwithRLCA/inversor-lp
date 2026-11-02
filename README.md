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
| `barrido.yml` | **Descubrimiento.** Recorre el universo EDGAR (~10.000 tickers) buscando candidatas nuevas, una vez cada una antes de repetir ninguna. | cada 4 horas |
| `refresco.yml` | **Mantenimiento.** Vuelve a pasar por el embudo a las empresas que YA tienen un análisis guardado, empezando por COMPRA_FUERTE/COMPRA/VIGILAR y por las más antiguas. Casi siempre gratis (caché). | a diario |
| `vigilancia.yml` | Comprueba el precio de las VIGILAR y abre un Issue si alguna cruza su precio de entrada. | cada 2 horas |

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

## Módulo mensual de oportunidades (experimental)

Complemento del bot de largo plazo, pensado para ir **mes a mes**: tú decides cuánto dinero
metes (a mano, en tu bróker) y la herramienta te dice **en qué acciones**. Parte de tu lista
(COMPRA_FUERTE, COMPRA, VIGILAR: ya pasaron el cribado fundamental) y busca las que están
baratas **frente a sí mismas** (su propio rango de los últimos ~5 años: «siempre ha estado
entre 80 y 120 y ahora está en 60»), sin estar hundidas y habiendo dejado de caer. Es un modelo
**solo de acciones**: sin ETF de relleno; si un mes nada está barato, el dinero espera.

### Qué dijo la prueba histórica (y por qué el módulo es como es)

Con 504 empresas del S&P 500 (2012-2026, impuestos y comisiones incluidos), las reglas tal como
se diseñaron **NO pasaron** los criterios fijados antes de ver los resultados:

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
- Conclusión honesta: **no hay evidencia de que la regla de compra bata al índice.** El backtest solo
  puede decir «no» (le faltan las empresas que quebraron y el filtro de calidad no se puede probar
  sin datos de la época). Por eso el módulo trae un **marcador** que compara tu resultado real con
  poner exactamente los mismos flujos de dinero en el S&P 500. Es el juez: dale 12-24 meses con
  importes que puedas permitirte, y si no gana, abandónalo.

### Cómo funciona cada mes

1. `mensual.yml` (día 1 de cada mes, o a mano) ejecuta `python -m mensual.recomendador`.
2. **Núcleo = lo que se probó:** señales de precio vs. su propio rango, máx. 4 candidatas y 2 por sector.
3. **Frenos que solo pueden quitar o avisar, nunca añadir:** precio por encima del rango de valor de la
   tesis; chequeo del último trimestre publicado en la SEC (ingresos y margen interanuales); y la IA
   (Gemini) lee los titulares de Finnhub y dice si la caída parece temporal o estructural. Solo veta
   «estructural con confianza alta»; si los titulares no explican la caída, dice «incierto».
4. Guarda el informe en Supabase (`mensual_informes`) y abre un Issue con las candidatas.
5. La pestaña **Mensual** del Vigía lee ese informe: escribes cuánto vas a invertir y reparte a partes
   iguales con tope por empresa (lo que no cabe NO se fuerza: se guarda para el mes siguiente).

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
| `backend/mensual/recomendador.py` | El informe mensual (núcleo + frenos) y el Markdown del Issue. |
| `backend/mensual/salud.py` | Chequeo del último trimestre (EDGAR, gratis). |
| `backend/mensual/explicacion.py` | «¿Por qué ha caído?»: titulares + Gemini, con veto solo si «estructural + alta». |
| `db/006_mensual.sql` | Tabla del informe y vista pública de solo lectura (aplicar en el SQL Editor de Supabase). |
| `docs/mensual.js`, `docs/mensual-ui.js` | Lógica pura y pantalla de la pestaña Mensual. |
| `.github/workflows/mensual.yml` | Informe mensual (día 1) o a mano. |
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
