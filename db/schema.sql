-- =========================================================================
-- INVERSOR LP — Esquema Supabase / PostgreSQL
-- Objetivo: no volver a pedir datos ni pagar tokens por algo ya calculado.
--
-- Idea central: `data_hash` (SHA-256 de las métricas normalizadas) +
-- `prompt_version` + `model` forman una clave única en `ai_analyses`.
-- Si los fundamentales no han cambiado y el prompt tampoco, el análisis
-- guardado sigue siendo válido → cero llamadas a la IA.
-- =========================================================================

create extension if not exists "pgcrypto";

-- ------------------------------------------------------------------ --
-- 1. Universo de empresas
-- ------------------------------------------------------------------ --
create table if not exists companies (
    id            uuid primary key default gen_random_uuid(),
    ticker        text        not null unique,
    cik           text,
    name          text        not null,
    sector        text,
    industry      text,
    exchange      text,
    country       text        default 'US',
    currency      text        default 'USD',
    is_financial  boolean     not null default false,  -- ratios de deuda no comparables
    active        boolean     not null default true,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);
create index if not exists idx_companies_sector on companies (sector) where active;

-- ------------------------------------------------------------------ --
-- 2. Fotografías de fundamentales (capa de datos + caché de API)
-- ------------------------------------------------------------------ --
create table if not exists fundamentals_snapshots (
    id            uuid primary key default gen_random_uuid(),
    company_id    uuid not null references companies (id) on delete cascade,
    source        text not null check (source in ('edgar', 'finnhub', 'mixto', 'manual')),
    as_of         date not null,                -- fecha de corte de los datos
    last_fy       int,                          -- último año fiscal incluido
    raw           jsonb not null,               -- respuesta cruda del proveedor
    metrics       jsonb not null,               -- métricas derivadas (Metrics)
    data_hash     text  not null,               -- sha256 de metrics normalizado
    price         numeric(18, 4),               -- se refresca aparte, no entra en el hash
    price_at      timestamptz,
    fetched_at    timestamptz not null default now(),
    expires_at    timestamptz not null default (now() + interval '90 days'),
    unique (company_id, source, as_of)
);
create index if not exists idx_snap_company_recent
    on fundamentals_snapshots (company_id, as_of desc);
create index if not exists idx_snap_hash on fundamentals_snapshots (data_hash);

comment on column fundamentals_snapshots.data_hash is
'Huella de las métricas SIN el precio. Cambia solo cuando la empresa publica
cuentas nuevas, que es lo único que invalida una tesis a 3-10 años.';

-- ------------------------------------------------------------------ --
-- 3. Resultados del filtro cuantitativo (capa 2)
-- ------------------------------------------------------------------ --
create table if not exists quant_screens (
    id              uuid primary key default gen_random_uuid(),
    company_id      uuid not null references companies (id) on delete cascade,
    snapshot_id     uuid not null references fundamentals_snapshots (id) on delete cascade,
    ruleset_version text not null,
    score           numeric(5, 2) not null,
    passed          boolean not null,
    vetoes          text[] not null default '{}',
    warnings        text[] not null default '{}',
    breakdown       jsonb  not null default '{}',
    created_at      timestamptz not null default now(),
    unique (snapshot_id, ruleset_version)
);
create index if not exists idx_screens_passed
    on quant_screens (passed, score desc) where passed;

-- ------------------------------------------------------------------ --
-- 4. Análisis de la IA (capa 3) — lo caro, lo que hay que cachear
-- ------------------------------------------------------------------ --
create type verdict_t as enum
    ('COMPRA_FUERTE', 'COMPRA', 'VIGILAR', 'NO_INVERTIBLE', 'DESCARTE');
create type confianza_t as enum ('Alta', 'Media', 'Baja');

create table if not exists ai_analyses (
    id                uuid primary key default gen_random_uuid(),
    company_id        uuid not null references companies (id) on delete cascade,
    snapshot_id       uuid not null references fundamentals_snapshots (id) on delete cascade,
    screen_id         uuid references quant_screens (id) on delete set null,

    -- claves de invalidación
    data_hash         text not null,
    prompt_version    text not null,
    model             text not null,

    -- resultado estructurado
    verdict           verdict_t not null,
    score_total       numeric(4, 2),
    score_moat        numeric(4, 2),
    score_salud       numeric(4, 2),
    score_revaloriz   numeric(4, 2),
    score_margen      numeric(4, 2),
    confianza         confianza_t,

    price_at_analysis numeric(18, 4),
    valor_min         numeric(18, 4),
    valor_max         numeric(18, 4),
    precio_entrada    numeric(18, 4),   -- precio máximo con margen suficiente

    report_md         text not null,    -- informe completo en Markdown
    report_json       jsonb,            -- mismo informe parseado

    input_tokens      int,
    output_tokens     int,
    cached_tokens     int,
    cost_usd          numeric(10, 6),

    created_at        timestamptz not null default now(),
    expires_at        timestamptz not null default (now() + interval '180 days'),

    -- ⇩ la restricción que ahorra el dinero
    unique (company_id, data_hash, prompt_version, model)
);
create index if not exists idx_analyses_company on ai_analyses (company_id, created_at desc);
create index if not exists idx_analyses_verdict on ai_analyses (verdict, score_total desc);

-- ------------------------------------------------------------------ --
-- 5. Caché genérica de llamadas HTTP (respeta rate limits gratuitos)
-- ------------------------------------------------------------------ --
create table if not exists api_cache (
    cache_key   text primary key,           -- p.ej. 'edgar:companyfacts:0000789019'
    provider    text not null,
    payload     jsonb not null,
    fetched_at  timestamptz not null default now(),
    expires_at  timestamptz not null
);
create index if not exists idx_api_cache_exp on api_cache (expires_at);

-- ------------------------------------------------------------------ --
-- 6. Cartera y seguimiento (datos privados del usuario)
-- ------------------------------------------------------------------ --
create table if not exists watchlist (
    id            uuid primary key default gen_random_uuid(),
    user_id       uuid not null default auth.uid(),
    company_id    uuid not null references companies (id) on delete cascade,
    estado        text not null default 'vigilando'
                  check (estado in ('vigilando', 'en_cartera', 'descartada')),
    precio_objetivo numeric(18, 4),
    tesis_propia  text,
    revisar_el    date,                     -- revisión anual, no diaria
    notas         text,
    created_at    timestamptz not null default now(),
    unique (user_id, company_id)
);

create table if not exists positions (
    id           uuid primary key default gen_random_uuid(),
    user_id      uuid not null default auth.uid(),
    company_id   uuid not null references companies (id) on delete cascade,
    fecha        date not null,
    tipo         text not null check (tipo in ('compra', 'venta')),
    acciones     numeric(18, 6) not null,
    precio       numeric(18, 4) not null,
    comision     numeric(18, 4) default 0,
    created_at   timestamptz not null default now()
);
create index if not exists idx_positions_user on positions (user_id, company_id, fecha);

-- ------------------------------------------------------------------ --
-- 7. Vistas de consulta
-- ------------------------------------------------------------------ --
create or replace view v_ultimo_analisis as
select distinct on (a.company_id)
       c.ticker, c.name, c.sector,
       a.verdict, a.score_total, a.confianza,
       a.price_at_analysis, a.valor_min, a.valor_max, a.precio_entrada,
       s.as_of      as datos_a,
       a.created_at as analizado_el,
       a.expires_at,
       a.report_md
from ai_analyses a
join companies c              on c.id = a.company_id
join fundamentals_snapshots s on s.id = a.snapshot_id
order by a.company_id, a.created_at desc;

-- Candidatas: pasaron el filtro y aún no tienen análisis vigente
create or replace view v_pendientes_ia as
select c.ticker, q.score, q.snapshot_id, q.id as screen_id
from quant_screens q
join companies c              on c.id = q.company_id
join fundamentals_snapshots s on s.id = q.snapshot_id
where q.passed
  and not exists (
        select 1 from ai_analyses a
        where a.company_id = q.company_id
          and a.data_hash  = s.data_hash
          and a.expires_at > now()
  )
order by q.score desc;

-- ------------------------------------------------------------------ --
-- 8. Función de decisión: ¿hay que llamar a la IA?
-- ------------------------------------------------------------------ --
create or replace function necesita_analisis(
    p_ticker         text,
    p_data_hash      text,
    p_prompt_version text,
    p_model          text
) returns boolean
language sql stable as $$
    select not exists (
        select 1
        from ai_analyses a
        join companies c on c.id = a.company_id
        where c.ticker        = upper(p_ticker)
          and a.data_hash     = p_data_hash
          and a.prompt_version= p_prompt_version
          and a.model         = p_model
          and a.expires_at    > now()
    );
$$;

-- ------------------------------------------------------------------ --
-- 9. Seguridad (RLS)
--    Datos de referencia: lectura para cualquier usuario autenticado,
--    escritura solo desde el backend (service_role, que salta RLS).
--    Datos de cartera: estrictamente del propietario.
-- ------------------------------------------------------------------ --
alter table companies              enable row level security;
alter table fundamentals_snapshots enable row level security;
alter table quant_screens          enable row level security;
alter table ai_analyses            enable row level security;
alter table api_cache              enable row level security;
alter table watchlist              enable row level security;
alter table positions              enable row level security;

create policy "lectura companies"  on companies              for select to authenticated using (true);
create policy "lectura snapshots"  on fundamentals_snapshots for select to authenticated using (true);
create policy "lectura screens"    on quant_screens          for select to authenticated using (true);
create policy "lectura analyses"   on ai_analyses            for select to authenticated using (true);
-- api_cache: sin políticas → solo accesible con service_role.

create policy "watchlist propia" on watchlist for all to authenticated
    using (user_id = auth.uid()) with check (user_id = auth.uid());
create policy "posiciones propias" on positions for all to authenticated
    using (user_id = auth.uid()) with check (user_id = auth.uid());

-- ------------------------------------------------------------------ --
-- 10. Mantenimiento (programar con pg_cron)
-- ------------------------------------------------------------------ --
create or replace function limpiar_caches() returns void
language sql as $$
    delete from api_cache where expires_at < now();
    delete from fundamentals_snapshots s
      where s.expires_at < now() - interval '2 years'
        and not exists (select 1 from ai_analyses a where a.snapshot_id = s.id);
$$;
-- select cron.schedule('limpieza', '0 4 * * 0', 'select limpiar_caches()');
