-- =========================================================================
-- MIGRACIÓN 004 — Progreso del barrido del universo
--
-- Analizar los ~10.000 tickers de la SEC no cabe en una sola ejecución de
-- GitHub Actions. Esta tabla lleva la cuenta de a quién le toca ya, para
-- que cada ejecución (manual o programada) coja el siguiente lote sin
-- repetir a los que se procesaron hace poco.
--
-- No es el resultado del análisis (eso vive en companies / quant_screens /
-- ai_analyses) — es solo la cola de "a quién le toca a continuación".
-- =========================================================================

create table if not exists universe_progreso (
    ticker              text primary key,
    cik                 text,
    nombre              text,
    company_id          uuid references companies(id) on delete set null,
    ultimo_intento      timestamptz,
    ultimo_screening_ok boolean,
    ultimo_error        text,
    veces_intentado     int not null default 0,
    creado_en           timestamptz not null default now()
);

-- Los nunca-intentados (ultimo_intento nulo) salen primero; luego, los
-- intentados hace más tiempo. Así el barrido recorre todo el universo
-- por rondas en vez de quedarse siempre en los mismos primeros tickers.
create index if not exists idx_universe_progreso_pendientes
    on universe_progreso (ultimo_intento nulls first);

comment on table universe_progreso is
'Cola de barrido del universo completo de tickers EDGAR. Sembrada una vez
desde universe.py y actualizada en cada ejecución de run_universo.py.';

alter table universe_progreso enable row level security;
create policy "lectura universe_progreso" on universe_progreso
    for select to authenticated using (true);

-- No hace falta GRANT explícito para service_role: la migración 003 ya dejó
-- configurado que las tablas nuevas lo reciban automáticamente.
