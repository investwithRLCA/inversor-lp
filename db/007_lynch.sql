-- =========================================================================
-- MIGRACIÓN 007 — Filtro Peter Lynch visible en el Vigía
--
-- El bot guarda con cada cribado la evaluación Peter Lynch de la empresa
-- (quant_screens.breakdown -> 'lynch': categoría, PEG, crecimiento del
-- beneficio por acción, deuda, si pasa o no y por qué). Esta migración la
-- añade al final de la vista pública v_tesis_completa, tomando el cribado
-- MÁS RECIENTE de cada empresa (es el que tiene el precio más actual).
--
-- Solo datos de empresas (públicos). Nada de tu cartera.
--
-- Cómo aplicarla: Supabase → SQL Editor → pega todo esto → Run.
-- Es idempotente: puedes ejecutarla más de una vez sin problema.
-- =========================================================================

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
       a.id              as analysis_id,
       a.confianza,
       a.ultimo_precio,
       a.ultimo_precio_en,
       a.en_zona_compra,
       a.valor_min,
       a.valor_max,
       a.report_md,
       -- nueva (007), SIEMPRE al final: Postgres no deja reordenar columnas de una vista
       ql.lynch,
       ql.lynch_el
from ai_analyses a
join companies c              on c.id = a.company_id
join fundamentals_snapshots s on s.id = a.snapshot_id
left join macro_analyses m    on m.id = a.macro_analysis_id
left join lateral (
       select q.breakdown -> 'lynch' as lynch, q.created_at as lynch_el
       from quant_screens q
       where q.company_id = a.company_id and q.breakdown ? 'lynch'
       order by q.created_at desc
       limit 1
) ql on true
order by a.company_id, a.created_at desc;

grant select on v_tesis_completa to anon;

-- La consulta de arriba busca el último cribado de cada empresa: este índice la hace rápida.
create index if not exists idx_quant_screens_empresa_fecha on quant_screens (company_id, created_at desc);
