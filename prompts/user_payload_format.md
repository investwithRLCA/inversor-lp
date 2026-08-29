# FORMATO DE DATOS OPTIMIZADO (mensaje `user`)

## Principio de diseño

El truco de ahorro no es acortar las cifras, es **mover la leyenda al system prompt**.

- El `system` (rol + 4 pilares + formato + **leyenda de campos**) es idéntico en las
  ~2.000 llamadas que harás. Con prompt caching cuesta ~10 % de su precio a partir de la
  segunda llamada.
- El `user` cambia en cada empresa → ahí es donde cada token se paga a precio completo.

Por tanto: **cero prosa, cero cabeceras de tabla, cero unidades repetidas** en el `user`.
Formato de filas con `|` porque tokeniza mejor que JSON (sin llaves, comillas ni nombres
de campo repetidos).

Ahorro medido en un caso típico: tabla Markdown "bonita" ≈ 780 tokens vs. formato compacto
≈ 210 tokens. Sobre 500 análisis, son ~285.000 tokens de entrada ahorrados.

---

## Bloque a añadir al SYSTEM PROMPT (sección 3-bis)

```markdown
## LEYENDA DEL BLOQUE <DATOS>
Filas separadas por salto de línea, campos por `|`. Series temporales: del año fiscal más
antiguo al más reciente. Unidades: millones de la moneda indicada, salvo ratios y
por-acción. `n/d` = dato no disponible.

ID      | nombre | sector | país | año fiscal último cierre
PX      | precio | market cap | enterprise value
MULT    | PER | PER medio 5a | EV/EBIT | P/FCF | P/VC | rent. dividendo %
REV     | serie de ingresos
EBIT    | serie de resultado de explotación
NI      | serie de beneficio neto
FCF     | serie de flujo de caja libre (CFO − capex)
MGN     | margen bruto % (serie)
ROIC    | serie de ROIC % (aproximada: NOPAT de cada año sobre el capital
          invertido actual; sirve para ver la tendencia, no como cifra exacta histórica)
BAL     | deuda total | caja | patrimonio neto | deuda neta/EBITDA | cobertura intereses
SHR     | serie de acciones diluidas (millones)
CAGR    | ingresos 3a % | ingresos 5a % | BPA 5a % | FCF 5a %
CAP     | % FCF a dividendo | % FCF a recompra | % FCF a capex crecimiento
FLAG    | banderas del filtro cuantitativo (texto libre corto)
```

---

## Ejemplo real de mensaje `user` (formato compacto)

```
<DATOS ticker="MSFT" fecha_corte="2026-06-30" moneda="USD" fy_serie="2021..2025">
ID|Microsoft Corp|Software Infraestructura|US|FY2025
PX|498.2|3702000|3665000
MULT|34.1|31.8|27.8|38.0|11.2|0.7
REV|168088|198270|211915|245122|281700
EBIT|69916|83383|88523|109433|128900
NI|61271|72738|72361|88136|101800
FCF|56118|65149|59475|74071|85200
MGN|68.9|68.4|68.9|69.8|70.1
ROIC|30.1|31.7|26.9|29.4|31.2
BAL|97852|75543|268477|0.2|38.4
SHR|7547|7496|7472|7446|7433
CAGR|12.4|13.8|10.9|11.0
CAP|32|24|31
FLAG|paso_quant score=87; sin vetos
</DATOS>

Analiza según tu mandato.
```

Eso es todo. **No añadas instrucciones en el `user`** ("por favor sé riguroso", "usa los
4 pilares"...): ya están en el system y las repetirías en cada llamada.

---

## Variante con contexto cualitativo (solo cuando aporta)

Si quieres inyectar noticias o el MD&A del 10-K, hazlo **filtrado y comprimido**, nunca
en bruto, y siempre etiquetado para que la IA aplique la regla de descarte temporal:

```
<CONTEXTO tipo="estructural">
- 2025-Q3: contrato de capacidad de nube a 6 años con {cliente}, ~2% ingresos anuales.
- 2026-02: regulador UE abre expediente sobre bundling de Teams. Riesgo: remedios estructurales.
</CONTEXTO>
```

Regla: máximo 5 viñetas, cada una con fecha, y solo hechos con efecto >3 años. Si dudas
si una noticia es estructural, no la incluyas: la IA la descartará igualmente y habrás
pagado los tokens.

---

## Presupuesto de tokens por análisis

| Componente | Tokens aprox. | Coste |
|---|---|---|
| System prompt (v1.3) | ~2.100 | cacheado tras la 1ª llamada |
| `<DATOS>` compacto | ~210 | precio completo |
| `<CONTEXTO>` opcional | ~120 | precio completo |
| Salida del informe | ~850 | precio de salida |

Con el filtro cuantitativo descartando ~85 % del universo antes de la IA, analizar 500
tickers cuesta aproximadamente lo mismo que analizar 75 sin filtro.
