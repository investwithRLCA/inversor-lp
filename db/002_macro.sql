-- =========================================================================
-- MIGRACIÓN 002 — Capa macro
-- Aditiva: no toca nada de schema.sql. Ejecutar después.
--
-- Diferencia clave con ai_analyses: la unicidad es por SECTOR, no por empresa.
-- Un análisis macro de "aerolineas|red-europea" sirve para todas las
-- aerolíneas de red europeas del universo.
-- =========================================================================

create table if not exists macro_analyses (
    id                uuid primary key default gen_random_uuid(),

    cache_key         text not null,              -- 'aerolineas|red-europea'
    sector            text not null,
    subsector         text,

    prompt_version    text not null,
    model             text not null,

    score             numeric(4, 2) check (score between 1 and 10),
    confianza         confianza_t,
    tesis             text,
    reloj_disrupcion  text,                       -- tecnología · fase · año estimado
    refutacion        text,                       -- qué observación tumbaría la tesis

    report_md         text not null,
    report_json       jsonb,

    input_tokens      int,
    output_tokens     int,
    cached_tokens     int,
    cost_usd          numeric(10, 6),

    created_at        timestamptz not null default now(),
    valid_until       timestamptz not null default (now() + interval '270 days'),
    invalidated_at    timestamptz,                -- a mano, si ocurre un hecho con fecha
    invalidated_por   text,

    unique (cache_key, prompt_version, model)
);
create index if not exists idx_macro_vigente
    on macro_analyses (cache_key)
    where invalidated_at is null;
create index if not exists idx_macro_score on macro_analyses (score desc);

comment on table macro_analyses is
'Análisis estructural de sector. TTL largo (270 días) porque las dinámicas macro no
cambian en trimestres. Se invalida a mano cuando un hecho fija una fecha: una
prohibición legislada, el cruce de coste de una tecnología sustitutiva, una fusión
que reestructura la competencia del sector.';

-- ------------------------------------------------------------------ --
-- Enlace empresa → sector macro
-- ------------------------------------------------------------------ --
alter table companies
    add column if not exists macro_cache_key text;
create index if not exists idx_companies_macro on companies (macro_cache_key);

-- ------------------------------------------------------------------ --
-- El veredicto final vive en ai_analyses, acotado por el macro
-- ------------------------------------------------------------------ --
alter table ai_analyses
    add column if not exists macro_analysis_id uuid references macro_analyses (id) on delete set null,
    add column if not exists macro_score       numeric(4, 2),
    add column if not exists verdict_final     verdict_t,
    add column if not exists ajuste_macro      text,
    add column if not exists excepcion_sector  boolean not null default false;

comment on column ai_analyses.verdict_final is
'Resultado de macro_layer.combine(): el veredicto fundamental ACOTADO por la
puntuación macro. `verdict` conserva el juicio fundamental puro para poder
auditar cuánto está corrigiendo la capa macro.';

-- ------------------------------------------------------------------ --
-- Vista consolidada
-- ------------------------------------------------------------------ --
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
       a.created_at      as analizado_el
from ai_analyses a
join companies c              on c.id = a.company_id
join fundamentals_snapshots s on s.id = a.snapshot_id
left join macro_analyses m    on m.id = a.macro_analysis_id
order by a.company_id, a.created_at desc;

-- Sectores ya evaluados y aún vigentes (para no volver a llamar a la IA)
create or replace view v_macro_vigente as
select cache_key, sector, subsector, score, confianza, tesis, valid_until
from macro_analyses
where invalidated_at is null
  and valid_until > now()
order by score desc;

-- ------------------------------------------------------------------ --
-- Función: ¿hay macro vigente para este sector?
-- ------------------------------------------------------------------ --
create or replace function macro_vigente(
    p_cache_key      text,
    p_prompt_version text,
    p_model          text
) returns uuid
language sql stable as $$
    select id
    from macro_analyses
    where cache_key      = p_cache_key
      and prompt_version = p_prompt_version
      and model          = p_model
      and invalidated_at is null
      and valid_until    > now()
    limit 1;
$$;

-- Invalidación manual cuando ocurre un hecho estructural con fecha
create or replace function invalidar_macro(p_cache_key text, p_motivo text)
returns void language sql as $$
    update macro_analyses
       set invalidated_at = now(), invalidated_por = p_motivo
     where cache_key = p_cache_key and invalidated_at is null;
$$;

-- ------------------------------------------------------------------ --
-- RLS
-- ------------------------------------------------------------------ --
alter table macro_analyses enable row level security;
create policy "lectura macro" on macro_analyses
    for select to authenticated using (true);
