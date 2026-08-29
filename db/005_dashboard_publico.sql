-- =========================================================================
-- MIGRACIÓN 005 — Vista pública para el dashboard móvil
--
-- El dashboard (docs/index.html, servido por GitHub Pages) lee Supabase
-- directamente desde el navegador del móvil, con la clave `anon` — no hay
-- backend intermedio. Por eso:
--
--   1. Guardamos en ai_analyses el último precio que vigilar.py comprobó
--      (y si ya cruzó el precio de entrada), para que el dashboard pueda
--      mostrarlo SIN llamar a Finnhub por su cuenta — evita exponer esa
--      clave también en el navegador.
--   2. Ampliamos v_tesis_completa con esos campos y con los que el
--      dashboard necesita mostrar (informe completo, confianza, rango de
--      valor intrínseco).
--   3. Concedemos a `anon` acceso de solo lectura a esa vista — y SOLO a
--      esa vista, no a las tablas de debajo. Quien tenga el enlace puede
--      LEER tus análisis; no puede escribir nada (no hay grant de INSERT/
--      UPDATE/DELETE para anon en ningún sitio).
-- =========================================================================

alter table ai_analyses
    add column if not exists ultimo_precio    numeric(18, 4),
    add column if not exists ultimo_precio_en timestamptz,
    add column if not exists en_zona_compra   boolean not null default false;

create or replace view v_tesis_completa as
select distinct on (a.company_id)
       c.ticker,
       c.name,
       c.sector,
       m.sector          as sector_macro,
       m.score           as macro_score,
       a.score_total     as nota_fundamental,
       a.verdict         as veredicto_fundamental,
       coalesce(a.verdict_final, a.verdict) as veredicto_final,
       a.ajuste_macro,
       a.precio_entrada,
       a.price_at_analysis,
       m.tesis           as tesis_macro,
       m.reloj_disrupcion,
       s.as_of           as datos_a,
       a.created_at      as analizado_el,
       -- columnas nuevas, SIEMPRE al final: Postgres no permite reordenar
       -- ni insertar en medio de columnas ya existentes con CREATE OR REPLACE VIEW.
       a.id              as analysis_id,
       a.confianza,
       a.ultimo_precio,
       a.ultimo_precio_en,
       a.en_zona_compra,
       a.valor_min,
       a.valor_max,
       a.report_md
from ai_analyses a
join companies c              on c.id = a.company_id
join fundamentals_snapshots s on s.id = a.snapshot_id
left join macro_analyses m    on m.id = a.macro_analysis_id
order by a.company_id, a.created_at desc;

-- Solo lectura, y solo de esta vista concreta — las tablas base
-- (ai_analyses, companies...) siguen sin ser accesibles para `anon`.
grant usage on schema public to anon;
grant select on v_tesis_completa to anon;