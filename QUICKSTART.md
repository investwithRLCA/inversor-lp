# Cómo probarlo en tu ordenador

Cuatro niveles, de menos a más. Cada uno funciona solo, así que si algo falla sabes
exactamente dónde. No montes Supabase hasta el nivel 4.

---

## Paso 0 · Colocar los archivos

**La estructura de carpetas importa.** El código busca los prompts en `../prompts/`,
así que si lo dejas todo suelto en una carpeta, no arranca.

```
inversor-lp/
├── backend/
│   ├── providers.py
│   ├── quant_filter.py
│   ├── macro_layer.py
│   ├── pipeline.py
│   └── probar_ia.py
├── prompts/
│   ├── system_prompt_es.md
│   ├── macro_prompt_es.md
│   └── user_payload_format.md
├── db/
│   ├── schema.sql
│   └── 002_macro.sql
└── README.md
```

Descarga los archivos del chat y colócalos así. En Mac/Linux:

```bash
mkdir -p ~/inversor-lp/backend ~/inversor-lp/prompts ~/inversor-lp/db
cd ~/inversor-lp
```

En Windows es lo mismo con el explorador de archivos: crea `inversor-lp` y dentro las
tres carpetas.

### Python y dependencias

Necesitas **Python 3.10 o superior** (el código usa sintaxis moderna de tipos).

```bash
python3 --version          # Windows: python --version

cd ~/inversor-lp
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install requests
```

Sabrás que el entorno virtual está activo porque el prompt del terminal empieza por
`(.venv)`. Cada vez que abras un terminal nuevo tienes que volver a activarlo.

---

## Nivel 1 · Comprobar que el código arranca (0 €, sin internet)

```bash
cd ~/inversor-lp/backend
python3 macro_layer.py
```

Debe imprimir la tabla de la regla de combinación:

```
macro=3 fund=COMPRA         moat=9 → DESCARTE       (sector estructuralmente en contracción)
macro=3 fund=COMPRA         moat=9 → VIGILAR        (excepción documentada: techo VIGILAR)
macro=5 fund=COMPRA_FUERTE  moat=8 → VIGILAR        (buen negocio en sector sin viento a favor)
...
```

Si ves eso, Python y los archivos están bien. Si sale `ModuleNotFoundError`, no has
activado el entorno virtual o no instalaste `requests`.

---

## Nivel 2 · Datos reales de la SEC (gratis, sin API key)

La SEC exige identificarse en cada petición. **No es opcional**: sin esto devuelve 403.

```bash
export SEC_USER_AGENT="Rafa/1.0 (tu@email.com)"
```

En Windows PowerShell: `$env:SEC_USER_AGENT="Rafa/1.0 (tu@email.com)"`

Y ahora, el cribado cuantitativo sobre datos auditados de verdad:

```bash
python3 probar_ia.py MSFT KO F
```

Verás, por cada empresa: nombre, años de datos disponibles, si pasa el filtro, la
puntuación, los vetos que se han activado y el payload compacto que se enviaría a la IA.
**Cero tokens gastados.**

Pruébalo con empresas que deberían salir por motivos distintos: `MSFT` (debería pasar),
`F` (Ford: mucha deuda), `T` (AT&T: crecimiento plano), `BA` (Boeing: FCF negativo).
Si los vetos no se disparan donde esperas, ahí es donde tienes que tocar `Thresholds`
en `quant_filter.py`.

### Añadir el precio (opcional pero recomendable)

Sin precio no hay PER ni valoración. Regístrate gratis en finnhub.io y:

```bash
export FINNHUB_API_KEY="tu_clave"
```

---

## Nivel 3 · Probar la IA sin base de datos (gratis, con Gemini)

Clave gratuita, sin tarjeta, en https://aistudio.google.com/apikey.

```bash
export GEMINI_API_KEY="..."               # Windows: $env:GEMINI_API_KEY="..."

python3 probar_ia.py MSFT --ia            # solo análisis fundamental
python3 probar_ia.py MSFT --ia --macro    # + capa macro + veredicto final combinado
```

Te imprime el informe completo en Markdown, los campos que el parser ha conseguido
extraer y los tokens consumidos. El nivel gratuito de Gemini tiene un límite de
peticiones por minuto y por día — si te sale un error 429, espera un minuto o
analiza menos empresas de golpe. Empieza con **una sola empresa**.

Dos cosas que mirar con lupa en esta primera ejecución:

1. **¿El parser extrae todos los campos?** Si `parse_report()` devuelve un diccionario
   incompleto, es que el modelo se ha desviado del formato. Se arregla apretando la
   sección 7 del system prompt, no tocando el regex.
2. **¿Las notas están infladas?** Si las primeras cinco empresas salen todas con 8/10,
   el prompt es demasiado complaciente. Es el fallo más común y el más caro.

---

## Nivel 4 · Supabase (persistencia)

Solo cuando los niveles 2 y 3 funcionen.

1. Crea un proyecto gratuito en supabase.com.
2. En el panel: **SQL Editor** → pega `db/schema.sql` → *Run*. Después `db/002_macro.sql`
   → *Run*. El orden importa: el segundo depende de tipos creados en el primero.
3. En **Project Settings → API** copia la URL y la clave `service_role`.

```bash
pip install supabase
export SUPABASE_URL="https://xxxx.supabase.co"
export SUPABASE_SERVICE_KEY="eyJ..."

python3 pipeline.py MSFT
```

La segunda vez que lo ejecutes con la misma empresa debe salir `stage=cache` y coste 0.
Si no, la caché no está funcionando y estás pagando dos veces por lo mismo: revisa que
`data_hash` sea idéntico entre ejecuciones.

Ojo: la clave `service_role` salta todas las políticas de seguridad. Va en el backend y
**nunca** en una app móvil o en código que llegue al navegador.

---

## Que no se te olviden las variables cada vez

Crea un archivo `entorno.sh` en la raíz del proyecto:

```bash
export SEC_USER_AGENT="Rafa/1.0 (tu@email.com)"
export FINNHUB_API_KEY="..."
export GEMINI_API_KEY="..."
export SUPABASE_URL="..."
export SUPABASE_SERVICE_KEY="..."
```

Y al abrir el terminal: `source entorno.sh`. Si algún día subes esto a GitHub, añade
`entorno.sh` y `.venv/` al `.gitignore`.

---

## Problemas frecuentes

| Síntoma | Causa |
|---|---|
| `403 Forbidden` en sec.gov | Falta `SEC_USER_AGENT`, o no lleva un email dentro |
| `ModuleNotFoundError: providers` | No estás ejecutando desde dentro de `backend/` |
| `FileNotFoundError: system_prompt_es.md` | La carpeta `prompts/` no está al lado de `backend/` |
| `Ticker no encontrado en EDGAR` | Solo cubre emisores de EE. UU. IAG o Iberdrola no están |
| Todo sale `n/d` | La empresa usa etiquetas XBRL distintas: añádelas a `CONCEPTS` |
| `429 Too Many Requests` (Finnhub) | Finnhub gratuito son 60 peticiones/minuto |
| `429` al llamar a Gemini | Límite de peticiones del nivel gratuito superado: espera un minuto |
| Gemini rechaza con 400/403 | `GEMINI_API_KEY` mal copiada o caducada |

Cuando algo falle, `probar_ia.py` sin `--ia` es el diagnóstico más rápido: te dice si el
problema está en los datos o más arriba, y no cuesta nada.
