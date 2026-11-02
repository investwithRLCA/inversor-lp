-- =========================================================================
-- MIGRACIÓN 006 — Módulo mensual de oportunidades
--
-- Una tabla con el informe de cada mes (un JSON por ejecución) y una vista
-- pública con el ÚLTIMO informe, para que la pestaña «Mensual» del Vigía
-- (docs/index.html, en GitHub Pages) lo lea igual que lee v_tesis_completa.
--
-- PRIVACIDAD: el informe solo contiene datos de EMPRESAS (precios, señales,
-- veredictos de la tesis) — información que ya es pública en el Vigía. NO
-- guarda tu cartera, tus compras ni tus importes: eso vive únicamente en tu
-- móvil (localStorage), porque este repositorio es público.
--
-- Cómo aplicarla: Supabase → SQL Editor → pega todo esto → Run.
-- Es idempotente: puedes ejecutarla más de una vez sin problema.
-- =========================================================================

create table if not exists mensual_informes (
    id         uuid primary key default gen_random_uuid(),
    creado_el  timestamptz not null default now(),
    mes        text        not null,          -- 'AAAA-MM'
    informe    jsonb       not null
);

create index if not exists idx_mensual_informes_creado on mensual_informes (creado_el desc);

-- Sin políticas para anon/authenticated: nadie lee la tabla directamente.
-- El script de GitHub Actions usa la clave service_role, que se salta RLS.
alter table mensual_informes enable row level security;

create or replace view v_mensual_ultimo as
select id, creado_el, mes, informe
from mensual_informes
order by creado_el desc
limit 1;

-- Solo lectura, y solo de esta vista (igual que en la migración 005).
grant usage on schema public to anon;
grant select on v_mensual_ultimo to anon;
