-- =========================================================================
-- MIGRACIÓN 009 — Reloj de Vigía en Supabase (lanza los workflows de GitHub a su hora)
--
-- GitHub retrasa muchas horas o se salta las ejecuciones programadas (medido
-- en este repositorio: el 27-nov no lanzó ni una de ~18 de «Compra del mes»).
-- El programador de tareas de Postgres (pg_cron) sí es puntual: aquí se le
-- encarga lanzar, con la API de GitHub, el informe del día 1 y la lista de la
-- compra. Las programaciones de GitHub siguen ahí, de reserva.
--
--   · Informe mensual: día 1 a las 00:17 UTC, con reservas a las 06:17 y 12:17
--     y el día 2 a las 00:17. Va con «solo_si_falta»: si el informe del mes ya
--     está, el workflow no hace nada.
--   · Compra del mes: cada hora a y 7, de 13:07 a 21:07 UTC, de lunes a viernes
--     (publica la lista si falta y le pone precios al día si la bolsa está
--     abierta; si no hay nada que hacer, acaba en medio minuto).
--
-- ANTES de ejecutar esto, guarda en Vault el token de GitHub con el nombre
-- exacto  vigia_github_token  (Integrations → Vault → Add new secret). El token
-- es «fine-grained», SOLO del repositorio inversor-lp y SOLO con el permiso
-- «Actions: Read and write»: no puede leer ni tocar nada más. Nunca está en
-- este archivo ni en el repositorio (que es público).
--
-- Nada de tu cartera: solo se lanzan los workflows.
--
-- Cómo aplicarla: Supabase → SQL Editor → pega todo esto → Run.
-- Es idempotente: puedes ejecutarla más de una vez sin problema.
-- =========================================================================

-- 1) Extensiones: el programador (pg_cron) y las llamadas a internet (pg_net)
create extension if not exists pg_cron with schema pg_catalog;
create extension if not exists pg_net with schema extensions;

-- 2) Esquema privado (no se publica en la API de Supabase: nadie puede llamarlo desde fuera)
create schema if not exists vigia_privado;
revoke all on schema vigia_privado from public, anon, authenticated;

-- 3) Lanzar un workflow de GitHub. Devuelve el id de la llamada (su respuesta queda en net._http_response).
create or replace function vigia_privado.lanzar(flujo text, entradas jsonb default '{}'::jsonb)
returns bigint
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_tok text;
  v_id  bigint;
begin
  select decrypted_secret into v_tok
    from vault.decrypted_secrets
   where name = 'vigia_github_token'
   limit 1;
  if v_tok is null then
    raise exception 'Falta el secreto «vigia_github_token» en Vault (Integrations → Vault)';
  end if;
  select net.http_post(
    url     := 'https://api.github.com/repos/investwithRLCA/inversor-lp/actions/workflows/' || flujo || '/dispatches',
    body    := jsonb_build_object('ref', 'main', 'inputs', entradas),
    headers := jsonb_build_object(
                 'Authorization', 'Bearer ' || v_tok,
                 'Accept', 'application/vnd.github+json',
                 'X-GitHub-Api-Version', '2022-11-28',
                 'User-Agent', 'vigia-reloj-supabase',
                 'Content-Type', 'application/json'),
    timeout_milliseconds := 20000
  ) into v_id;
  return v_id;
end;
$$;
revoke all on function vigia_privado.lanzar(text, jsonb) from public, anon, authenticated;

-- 4) Las tareas (hora UTC). Con el mismo nombre se sustituyen: volver a ejecutar esto no las duplica.
select cron.schedule('vigia-informe-dia1',     '17 0 1 * *',
  $$ select vigia_privado.lanzar('mensual.yml', '{"solo_si_falta": "true"}'::jsonb) $$);
select cron.schedule('vigia-informe-reserva1', '17 6 1 * *',
  $$ select vigia_privado.lanzar('mensual.yml', '{"solo_si_falta": "true"}'::jsonb) $$);
select cron.schedule('vigia-informe-reserva2', '17 12 1 * *',
  $$ select vigia_privado.lanzar('mensual.yml', '{"solo_si_falta": "true"}'::jsonb) $$);
select cron.schedule('vigia-informe-dia2',     '17 0 2 * *',
  $$ select vigia_privado.lanzar('mensual.yml', '{"solo_si_falta": "true"}'::jsonb) $$);
select cron.schedule('vigia-compra-cada-hora', '7 13-21 * * 1-5',
  $$ select vigia_privado.lanzar('compra.yml') $$);

-- 5) Comprobación: las tareas que quedan programadas
select jobname as tarea, schedule as cuando_utc, active as activa
  from cron.job
 where jobname like 'vigia-%'
 order by jobname;

-- -------------------------------------------------------------------------
-- PRUEBA (aparte, después): lanza «Probar avisos al móvil» → te llega
-- «Vigía · prueba de avisos» al iPhone en un minuto o dos:
--
--   select vigia_privado.lanzar('avisos.yml');
--
-- Y, unos segundos después, la respuesta de GitHub (204 = lanzado bien;
-- 401/403/404 = el token no vale, no es de este repositorio o le falta el permiso):
--
--   select id, status_code, left(content::text, 200) as respuesta, created
--     from net._http_response order by id desc limit 5;
--
-- Para quitarlo todo:
--   select cron.unschedule(jobname) from cron.job where jobname like 'vigia-%';
-- -------------------------------------------------------------------------
