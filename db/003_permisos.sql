-- =========================================================================
-- MIGRACIÓN 003 — Permisos para service_role
--
-- Normalmente Supabase concede esto automáticamente en cada proyecto nuevo.
-- Si al usar la clave service_role (o su equivalente "Secret key") te sale
-- "permission denied for table ..." (código 42501), es que a ese rol le
-- faltan privilegios básicos sobre las tablas — un paso previo e
-- independiente de las políticas de RLS que ya definimos en schema.sql.
-- Esta migración se lo concede explícitamente, y hace lo mismo para
-- cualquier tabla que crees en el futuro, para no repetir este problema.
--
-- Segura de ejecutar más de una vez.
-- =========================================================================

grant usage on schema public to service_role;

grant select, insert, update, delete on all tables in schema public to service_role;
grant usage, select on all sequences in schema public to service_role;
grant execute on all functions in schema public to service_role;

-- Para que las tablas y funciones que crees en migraciones futuras
-- concedan esto automáticamente, sin repetir este paso cada vez:
alter default privileges in schema public
    grant select, insert, update, delete on tables to service_role;
alter default privileges in schema public
    grant usage, select on sequences to service_role;
alter default privileges in schema public
    grant execute on functions to service_role;
