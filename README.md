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

## Nota

Esto es una herramienta de análisis, no asesoramiento financiero. El informe que produce
la IA es una hipótesis estructurada, no una recomendación: los supuestos de valoración
son suyos y pueden estar equivocados. Las decisiones son tuyas.
