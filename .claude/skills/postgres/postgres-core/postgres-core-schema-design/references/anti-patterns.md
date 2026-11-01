# Anti-Patterns : Schema Design

Schema-design anti-patterns with symptom, cause, SQLSTATE (where applicable), detection query, and fix. Sources are official PostgreSQL 17 docs.

---

## AP-1 : `serial` in new code (v10+)

**Symptom** : `pg_dump --schema-only` output is full of `CREATE SEQUENCE`, `ALTER SEQUENCE OWNED BY`, `ALTER COLUMN SET DEFAULT nextval(...)` triples. `ALTER COLUMN TYPE bigint` fails or leaves the sequence orphaned. INSERT-only roles silently get USAGE on the sequence by table inheritance.

**Cause** : `serial` is documented as a shorthand for "CREATE SEQUENCE + integer column + DEFAULT nextval()". The sequence is owned by the column dependency but is a separate object with its own ACL. Privilege flows are surprising : a role with INSERT on the table also implicitly needs USAGE on the sequence ; PostgreSQL grants this transparently, which makes the grant logic invisible to auditors. Column type stays `integer` (max 2^31 − 1 ≈ 2.1B) unless `bigserial` was used explicitly.

**SQLSTATE** : not an error path : structural debt.

**Fix pattern** : migrate to `GENERATED ALWAYS AS IDENTITY` (see `examples.md` Example 3).

```sql
-- One-shot migration template
BEGIN;
  ALTER TABLE legacy ALTER COLUMN id DROP DEFAULT;
  -- compute start value FIRST, then add identity with literal START WITH
  ALTER TABLE legacy ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (START WITH 9001);
  -- existing sequence is now orphaned ; drop or repoint
  ALTER SEQUENCE legacy_id_seq OWNED BY NONE;
  DROP SEQUENCE legacy_id_seq;
COMMIT;
```

Source : postgresql.org/docs/17/datatype-numeric.html (Serial Types), postgresql.org/docs/17/sql-createtable.html (Identity Columns).

---

## AP-2 : Quoted `"CamelCase"` identifiers

**Symptom** : every query against the table has `"Quotes"` around identifiers. `pg_dump` output across environments diffs noisily. ORM model classes that emit lowercase SQL (Hibernate, Django) raise `relation "customers" does not exist` against a table named `"Customers"`. Junior developers paste a query and get a different error class than the table author intended.

**Cause** : PostgreSQL folds unquoted identifiers to lowercase. `CREATE TABLE Customers` actually creates a relation named `customers`. The MOMENT you write `CREATE TABLE "Customers"` (with quotes), the relation name is literally `Customers` (capital C). Subsequent unquoted references fold to `customers` and fail to resolve.

**SQLSTATE** : `42P01` (`undefined_table`) on any unquoted reference.

**Fix pattern** : rename to snake_case (this is a breaking change for every consumer ; coordinate).

```sql
-- Detect (full inventory) :
SELECT n.nspname, c.relname
FROM pg_class c
  JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE c.relkind IN ('r','v','m','i','S')
  AND quote_ident(c.relname) <> c.relname;

-- Rename in a transaction (every dependent view, function, FK must update):
BEGIN;
  ALTER TABLE "Customers" RENAME TO customers;
  ALTER TABLE customers RENAME COLUMN "CustomerID" TO id;
  -- Update every dependent view, function, materialized view manually.
COMMIT;
```

Source : postgresql.org/docs/17/sql-syntax-lexical.html (Identifiers and Key Words).

---

## AP-3 : Writable `public` schema on pre-v15 databases

**Symptom** : `SELECT has_schema_privilege('public', 'public', 'CREATE')` returns `t`. Low-privilege roles can `CREATE TABLE public.anything`. Tools that depend on schema-search-path resolution can be tricked into using attacker-supplied objects (CVE-2018-1058 against `pg_dump`).

**Cause** : pre-v15, `public` had `CREATE` granted to role `PUBLIC` by default. v15 changed the initdb default but did NOT alter existing databases. Any database created before v15 (or upgraded in place) keeps the open grant.

**SQLSTATE** : not an error path : trust-boundary leak.

**Fix pattern** :

```sql
-- For EVERY database in the cluster :
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- Verify :
SELECT datname,
       has_schema_privilege('public', 'public', 'CREATE') AS public_can_create
FROM pg_database
WHERE NOT datistemplate;
-- Expect: public_can_create = f everywhere.

-- Re-grant CREATE only to specific roles that need it :
GRANT CREATE ON SCHEMA public TO app_owner;
```

Source : postgresql.org/docs/17/ddl-schemas.html (Public Schema), postgresql.org/docs/15/release-15.html, CVE-2018-1058 advisory.

---

## AP-4 : `SECURITY DEFINER` function without pinned `search_path`

**Symptom** : a function that owns elevated privileges resolves bare object names against the caller's `search_path`. A low-privilege caller can `SET search_path = malicious, ...` and substitute objects (CVE-2007-2138 vector).

**Cause** : `SECURITY DEFINER` switches the executing role to the function owner but does NOT switch `search_path`. Unless the function explicitly pins `SET search_path = ...`, name resolution walks the caller-controlled path.

**SQLSTATE** : not an error path : silent privilege escalation.

**Fix pattern** :

```sql
-- Detect (one-shot per database) :
SELECT n.nspname || '.' || p.proname AS function,
       p.proconfig
FROM pg_proc p
  JOIN pg_namespace n ON p.pronamespace = n.oid
WHERE p.prosecdef
  AND (p.proconfig IS NULL
       OR NOT EXISTS (
         SELECT 1 FROM unnest(p.proconfig) AS cfg
         WHERE cfg LIKE 'search_path=%'
       ));

-- Remediate per function :
ALTER FUNCTION admin.check_password(text, text)
  SET search_path = pg_catalog, admin, pg_temp;

-- Verify :
SELECT proconfig FROM pg_proc
WHERE oid = 'admin.check_password(text,text)'::regprocedure;
```

Source : postgresql.org/docs/17/sql-createfunction.html (Writing SECURITY DEFINER Functions Safely), CVE-2007-2138.

---

## AP-5 : Reserved-word identifiers (`user`, `order`, `table`)

**Symptom** : `SELECT * FROM order` fails with `syntax error at or near "order"`. Every SQL statement against the table requires `"order"` (quoted). ORMs that emit unquoted SQL break entirely.

**Cause** : SQL-standard reserved words cannot be used as unquoted identifiers. PostgreSQL accepts them only when quoted, which inherits all the pain of AP-2.

**SQLSTATE** : `42601` (`syntax_error`) on every bare reference.

**Fix pattern** : rename. `user` -> `app_user` or `account` ; `order` -> `sales_order` or `order_record` ; `table` -> `data_table` or rename to its actual domain (`schedule`, `roster`).

```sql
ALTER TABLE "order" RENAME TO sales_order;
-- Update every dependent FK, view, function, trigger, RLS policy.
```

Source : postgresql.org/docs/17/sql-keywords-appendix.html (reserved word list).

---

## AP-6 : Mixing `id` and `<entity>_id` PK styles in one schema

**Symptom** : `JOIN ... USING(customer_id)` works between some tables but not others ; the planner cannot use a natural-join shorthand. ORMs that infer PK by convention misalign. Schema documentation reads inconsistently.

**Cause** : a convention started with one style, a later developer used the other, no project-wide rule enforced consistency.

**SQLSTATE** : not an error path : design debt.

**Fix pattern** : pick one convention project-wide and document in CLAUDE.md or schema-style.md. Rename existing tables in a coordinated migration. Two valid choices :

| Style | Primary key column | Foreign key column | Self-describing in joins? |
|---|---|---|---|
| `id` | `customer.id` | `order.customer_id` | No (needs `ON o.customer_id = c.id`) |
| `<entity>_id` | `customer.customer_id` | `order.customer_id` | Yes (`JOIN customer USING(customer_id)`) |

Source : postgresql.org/docs/17/queries-table-expressions.html (Joined Tables, USING clause).

---

## AP-7 : Schema-per-tenant scaling past ~50 tenants

**Symptom** : a DDL change requires an N-pass loop ; tooling times out ; planner stats are noisy because each tenant's table is statistically distinct ; `pg_dump` time scales linearly with tenant count.

**Cause** : schema-per-tenant duplicates every DDL artifact (tables, sequences, indexes, types, functions). Cost scales linearly. Beyond ~50 tenants the maintenance overhead overwhelms the isolation benefit.

**SQLSTATE** : not an error path : operational regression.

**Fix pattern** : migrate to RLS in a shared schema with a `tenant_id` column on every table. The migration is non-trivial (data copy, RLS policy authoring, application changes to set `app.current_tenant`) but pays back fast above ~50 tenants. See `postgres-core-rls-policies` for policy details.

Decision matrix : see `methods.md` "Multi-Tenant Decision Reference".

Source : postgresql.org/docs/17/ddl-rowsecurity.html.

---

## AP-8 : Cross-database joins via `dblink` for everyday queries

**Symptom** : queries that touch two databases run via `dblink('host=... dbname=other', 'SELECT ...')`. Latency is poor (one connection per call, no statement cache). Transactional integrity is absent (commits across databases are not atomic without 2PC).

**Cause** : the system was designed with multiple databases when it should have had multiple schemas in one database. PostgreSQL forbids cross-database queries natively : `SELECT * FROM other_db.public.t` does not work.

**SQLSTATE** : various, often `08006` (`connection_failure`) or timeout.

**Fix pattern** : consolidate into one database with multiple schemas (the schema separation gives you most of the isolation benefits while keeping query, transaction, and lock primitives intact). If true database isolation is required, use `postgres_fdw` (better caching than `dblink`) and accept the limitations.

Source : postgresql.org/docs/17/dblink.html, postgresql.org/docs/17/postgres-fdw.html.

---

## AP-9 : `pg_temp` missing or not last in pinned `SECURITY DEFINER` `search_path`

**Symptom** : a `SECURITY DEFINER` function HAS `SET search_path = admin` but a malicious caller can still substitute objects via the implicit temporary schema (which is searched first when present, unless explicitly placed elsewhere).

**Cause** : PostgreSQL's `pg_temp` schema is searched FIRST by default, even when not listed in `search_path`. To force it to be searched LAST (the safe position), it must be listed explicitly as the last element of `search_path`.

**SQLSTATE** : not an error path : silent injection vector.

**Fix pattern** :

```sql
-- Wrong (still vulnerable, pg_temp implicit-first wins) :
ALTER FUNCTION f() SET search_path = admin;

-- Correct :
ALTER FUNCTION f() SET search_path = pg_catalog, admin, pg_temp;

-- Verify :
SELECT proname, proconfig FROM pg_proc
WHERE pronamespace = 'admin'::regnamespace AND prosecdef
  AND proconfig::text NOT LIKE '%pg_temp%';
-- This query lists SECDEF functions that still have an unsafe search_path.
```

Source : postgresql.org/docs/17/sql-createfunction.html (Writing SECURITY DEFINER Functions Safely : the documented safe form ends with `pg_temp`).

---

## AP-10 : Forgetting `ALTER DEFAULT PRIVILEGES` after CREATE SCHEMA

**Symptom** : every new table created in a module schema needs a manual `GRANT SELECT, INSERT, UPDATE, DELETE ON <table> TO app_service` ; developers forget and the application gets `42501` (`insufficient_privilege`) after deploy.

**Cause** : `GRANT ... ON TABLES` applies to EXISTING tables only. New tables get default privileges defined by `ALTER DEFAULT PRIVILEGES`. Without the latter, every table is a one-off grant.

**SQLSTATE** : `42501` (`insufficient_privilege`) on application queries after a migration.

**Fix pattern** :

```sql
-- For each schema-owner role, declare what newly-created objects should grant :
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA app
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES    TO app_service;
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA app
  GRANT USAGE                          ON SEQUENCES TO app_service;
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA app
  GRANT EXECUTE                        ON FUNCTIONS TO app_service;

-- Backfill existing objects (one time) :
GRANT SELECT, INSERT, UPDATE, DELETE
  ON ALL TABLES    IN SCHEMA app TO app_service;
GRANT USAGE
  ON ALL SEQUENCES IN SCHEMA app TO app_service;
GRANT EXECUTE
  ON ALL FUNCTIONS IN SCHEMA app TO app_service;
```

Source : postgresql.org/docs/17/sql-alterdefaultprivileges.html, postgresql.org/docs/17/sql-grant.html.
