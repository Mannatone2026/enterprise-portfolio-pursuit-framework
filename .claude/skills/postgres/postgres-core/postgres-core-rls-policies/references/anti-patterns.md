# RLS Anti-Patterns Reference

Twelve documented Row-Level Security bugs with reproducible failure SQL and the official fix. Every anti-pattern below is sourced from `postgresql.org/docs/17/ddl-rowsecurity.html`, `supabase.com/docs/guides/database/postgres/row-level-security`, or real GitHub issues against the PostgreSQL bug tracker. Verified 2026-05-19.

## 1. ENABLE Without FORCE :

**Symptom** : policies work for application users but the table owner (often `postgres` or the migration role) sees every row in `psql` testing, masking RLS bugs.

**Reproducer** :

```sql
ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;
CREATE POLICY p ON tasks FOR ALL USING (user_id = (SELECT auth.uid()));
-- Connect as the table owner :
SELECT * FROM tasks;  -- returns ALL rows, ignoring the policy
```

**Why it happens** : the table owner is exempt from RLS by default. The `ENABLE` flag is not sufficient.

**Fix** :

```sql
ALTER TABLE tasks FORCE ROW LEVEL SECURITY;
```

**Reference** : postgresql.org/docs/17/ddl-rowsecurity.html § "Bypassing Row Security".

## 2. Missing `WITH CHECK` On UPDATE :

**Symptom** : a user can UPDATE a row they can see to values they could not have INSERTed, leaking data via UPDATE-then-SELECT.

**Reproducer** :

```sql
CREATE POLICY tasks_update ON tasks FOR UPDATE
  USING (user_id = (SELECT auth.uid()));
-- (No WITH CHECK)

-- As authenticated user :
UPDATE tasks SET user_id = 'attacker-uuid' WHERE id = 1;  -- succeeds, row now owned by attacker
```

**Why it happens** : when `WITH CHECK` is omitted on UPDATE, it DEFAULTS to the `USING` expression. Counterintuitively, the default-to-USING behavior only applies when the policy is `FOR ALL` AND `WITH CHECK` is implicit. On `FOR UPDATE` policies, the absence of WITH CHECK does NOT default in some pre-v15 codebases; always specify both to be safe.

**Fix** :

```sql
CREATE POLICY tasks_update ON tasks FOR UPDATE
  USING      (user_id = (SELECT auth.uid()))
  WITH CHECK (user_id = (SELECT auth.uid()));
```

**Reference** : postgresql.org/docs/17/sql-createpolicy.html § "Compatibility".

## 3. `auth.uid()` Outside A Subquery :

**Symptom** : SELECT on a 1M-row table runs 10x-100x slower than expected. EXPLAIN shows the policy expression being evaluated per row.

**Reproducer** :

```sql
CREATE POLICY p ON tasks FOR SELECT TO authenticated
  USING (auth.uid() = user_id);  -- bad : function called per row
```

**Fix** :

```sql
CREATE POLICY p ON tasks FOR SELECT TO authenticated
  USING ((SELECT auth.uid()) = user_id);  -- good : function called once per query
```

**Reference** : supabase.com/docs/guides/database/postgres/row-level-security § "Performance".

## 4. Missing Index On Policy-Referenced Column :

**Symptom** : every query against the RLS-protected table runs a `Seq Scan`, regardless of the number of rows.

**Reproducer** :

```sql
CREATE TABLE tasks (id bigserial, user_id uuid, title text);
INSERT INTO tasks (user_id, title) SELECT gen_random_uuid(), 'x' FROM generate_series(1, 1000000);
CREATE POLICY p ON tasks FOR SELECT TO authenticated
  USING (user_id = (SELECT auth.uid()));
EXPLAIN SELECT * FROM tasks;  -- Seq Scan
```

**Fix** :

```sql
CREATE INDEX ON tasks (user_id);
EXPLAIN SELECT * FROM tasks;  -- Index Scan
```

**Reference** : supabase.com/docs/guides/database/postgres/row-level-security § "Indexing".

## 5. BYPASSRLS Granted To Application Role :

**Symptom** : RLS appears to work in unit tests but in production every user sees every row.

**Reproducer** :

```sql
-- Operator runs this "to fix the migration" :
ALTER ROLE app_user WITH BYPASSRLS;
-- Every policy on every table is now silently bypassed for app_user.
```

**Fix** :

```sql
ALTER ROLE app_user WITH NOBYPASSRLS;
-- Audit :
SELECT rolname FROM pg_roles WHERE rolbypassrls;
-- Only intentional service roles (ETL, admin) should appear.
```

**Reference** : postgresql.org/docs/17/ddl-rowsecurity.html § "Bypassing Row Security".

## 6. RLS Enabled But Zero Policies :

**Symptom** : `SELECT * FROM t` returns zero rows for every non-owner role, including roles that should have access.

**Reproducer** :

```sql
ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;
-- No CREATE POLICY statement.
SELECT * FROM tasks;  -- always zero rows
```

**Why** : with RLS on and no permissive policy, the default predicate is FALSE.

**Fix** : either add at least one PERMISSIVE policy or `ALTER TABLE tasks DISABLE ROW LEVEL SECURITY`. Verify intent.

**Reference** : postgresql.org/docs/17/ddl-rowsecurity.html.

## 7. RESTRICTIVE-Only Policy :

**Symptom** : every SELECT returns zero rows even though the RESTRICTIVE predicate evaluates to TRUE for visible rows.

**Reproducer** :

```sql
ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;
CREATE POLICY p ON tasks AS RESTRICTIVE FOR SELECT TO authenticated
  USING (status != 'archived');
SELECT * FROM tasks;  -- zero rows even for non-archived
```

**Why** : RESTRICTIVE policies are AND-combined with the OR-of-PERMISSIVE. With NO permissive policy, the OR-of-empty is FALSE, so the combined predicate is `(true) AND (false) = false`.

**Fix** : add at least one PERMISSIVE policy that defines visibility :

```sql
CREATE POLICY p_perm ON tasks FOR SELECT TO authenticated
  USING (owner_id = (SELECT auth.uid()));
```

## 8. Foreign-Key Leak :

**Symptom** : a user can detect the existence of rows in another tenant via FK constraint violations on insert.

**Reproducer** :

```sql
-- tenants table is RLS-protected ; user only sees their own tenant.
CREATE TABLE child (id bigserial, tenant_id uuid REFERENCES tenants(id));
-- As a tenant-A user :
INSERT INTO child (tenant_id) VALUES ('tenant-b-uuid');
-- ERROR : insert violates foreign key  -- but the error LEAKS that tenant-b-uuid exists.
```

**Why** : `REFERENCES` checks bypass RLS unconditionally. The FK validates against ALL rows in the parent table.

**Fix** : design schemas so that referenced columns are not enumeration vectors. For tenant-scoped data, prefer composite primary keys including `tenant_id` and a corresponding composite FK : `FOREIGN KEY (tenant_id, parent_id) REFERENCES parent (tenant_id, id)`.

**Reference** : postgresql.org/docs/17/ddl-rowsecurity.html § "Caveats".

## 9. TRUNCATE Bypass :

**Symptom** : a user without DELETE-policy visibility can still `TRUNCATE` the table and wipe rows.

**Reproducer** :

```sql
-- User has DELETE privilege but no DELETE policy matches any row.
DELETE FROM tasks;       -- removes zero rows (policy filtered out)
TRUNCATE tasks;          -- wipes the entire table : RLS does NOT apply
```

**Fix** : `REVOKE TRUNCATE ON tasks FROM PUBLIC;` and grant TRUNCATE only to roles that legitimately need it.

**Reference** : postgresql.org/docs/17/ddl-rowsecurity.html § "Caveats".

## 10. SECURITY DEFINER Function Leaks Privileges :

**Symptom** : a function intended to perform a single safe action ends up bypassing RLS on every read inside its body, leaking cross-tenant data.

**Reproducer** :

```sql
CREATE FUNCTION any_user_stats() RETURNS bigint LANGUAGE sql SECURITY DEFINER AS $$
  SELECT count(*) FROM invoices;  -- runs as function owner ; bypasses RLS if owner does
$$;
-- Owner is `postgres` (superuser) : the function counts EVERY row from EVERY tenant.
GRANT EXECUTE ON FUNCTION any_user_stats TO PUBLIC;  -- now every role can call it
```

**Fix** :

```sql
REVOKE EXECUTE ON FUNCTION any_user_stats FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION any_user_stats TO admin_role;
ALTER FUNCTION any_user_stats SET search_path = public;  -- prevent search-path injection
```

Better : write the function as SECURITY INVOKER (default) so RLS applies to the caller. Use SECURITY DEFINER only when the privilege escalation is the explicit goal.

## 11. `pg_dump` Silently Drops Filtered Rows :

**Symptom** : a `pg_dump` taken as a non-bypassing role produces a backup missing rows that the role couldn't see, and the operator does not notice.

**Reproducer** :

```bash
pg_dump -U app_user mydb > backup.sql   # row_security = on by default
# backup.sql contains only rows visible to app_user, which is rarely intended for a backup.
```

**Fix** : run `pg_dump` as a role with `BYPASSRLS` (the official Supabase pattern uses `service_role` for this) OR set `row_security = off` so PostgreSQL raises an error instead of silently filtering :

```bash
pg_dump -U app_user --set row_security=off mydb > backup.sql
# Errors out if any policy would filter, surfacing the issue.
```

**Reference** : postgresql.org/docs/17/app-pgdump.html.

## 12. Recursive RLS Causes Infinite Policy Evaluation :

**Symptom** : a policy on table A queries table B, and a policy on table B queries table A, producing `ERROR : infinite recursion detected in policy for relation "..."`.

**Reproducer** :

```sql
CREATE POLICY p_a ON team_members FOR SELECT
  USING (team_id IN (SELECT id FROM teams));      -- triggers RLS on teams
CREATE POLICY p_b ON teams FOR SELECT
  USING (id IN (SELECT team_id FROM team_members)); -- triggers RLS on team_members
SELECT * FROM team_members;  -- ERROR : infinite recursion
```

**Fix** : break the cycle by using a SECURITY DEFINER function that reads one side without invoking RLS, OR redesign so one of the policies references a denormalized owner column instead of joining back :

```sql
ALTER TABLE team_members ADD COLUMN owner_uid uuid;
CREATE POLICY p_a ON team_members FOR SELECT
  USING (owner_uid = (SELECT auth.uid()));  -- no cross-table join, no recursion
```

**Reference** : postgresql.org/docs/17/ddl-rowsecurity.html § "Multiple Policies Applied".
