# RLS Examples Reference

Complete, runnable patterns for Row-Level Security on PostgreSQL 15, 16, and 17. Every example has been verified against `postgresql.org/docs/17/ddl-rowsecurity.html` and `supabase.com/docs/guides/database/postgres/row-level-security` on 2026-05-19.

## Example 1 : Per-User Ownership (Supabase) :

```sql
CREATE TABLE notes (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  title      text NOT NULL,
  body       text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX notes_user_id_idx ON notes (user_id);

ALTER TABLE notes ENABLE ROW LEVEL SECURITY;

CREATE POLICY notes_select ON notes FOR SELECT TO authenticated
  USING (user_id = (SELECT auth.uid()));

CREATE POLICY notes_insert ON notes FOR INSERT TO authenticated
  WITH CHECK (user_id = (SELECT auth.uid()));

CREATE POLICY notes_update ON notes FOR UPDATE TO authenticated
  USING      (user_id = (SELECT auth.uid()))
  WITH CHECK (user_id = (SELECT auth.uid()));

CREATE POLICY notes_delete ON notes FOR DELETE TO authenticated
  USING (user_id = (SELECT auth.uid()));
```

Why `TO authenticated` and not `TO PUBLIC` : unauthenticated requests (role `anon`) match no policy and immediately see zero rows. PostgreSQL skips policy evaluation for them, which is faster than evaluating a policy that would return false.

## Example 2 : Multi-Tenant SaaS By `tenant_id` :

```sql
CREATE TABLE invoices (
  id         bigserial PRIMARY KEY,
  tenant_id  uuid NOT NULL,
  amount     numeric(12,2) NOT NULL,
  paid       boolean NOT NULL DEFAULT false
);

CREATE INDEX invoices_tenant_id_idx ON invoices (tenant_id);

ALTER TABLE invoices ENABLE  ROW LEVEL SECURITY;
ALTER TABLE invoices FORCE   ROW LEVEL SECURITY;

CREATE POLICY invoice_tenant ON invoices FOR ALL TO app_user
  USING      (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);
```

The application code sets the tenant per transaction :

```sql
BEGIN;
SET LOCAL app.current_tenant_id = '11111111-1111-1111-1111-111111111111';
SELECT * FROM invoices;          -- only sees this tenant
INSERT INTO invoices (tenant_id, amount) VALUES ('11111111-...', 99.00);  -- ok
INSERT INTO invoices (tenant_id, amount) VALUES ('22222222-...', 99.00);  -- ERROR: new row violates policy
COMMIT;
```

`SET LOCAL` resets at transaction end, so a hijacked session cannot retain a tenant context across transactions.

## Example 3 : Public-Read + Owner-Write :

```sql
CREATE TABLE posts (
  id         bigserial PRIMARY KEY,
  author_id  uuid NOT NULL,
  title      text NOT NULL,
  body       text NOT NULL,
  is_public  boolean NOT NULL DEFAULT false
);

CREATE INDEX posts_author_id_idx ON posts (author_id);

ALTER TABLE posts ENABLE ROW LEVEL SECURITY;

CREATE POLICY posts_public_read ON posts FOR SELECT TO anon, authenticated
  USING (is_public = true);

CREATE POLICY posts_owner_read ON posts FOR SELECT TO authenticated
  USING (author_id = (SELECT auth.uid()));

CREATE POLICY posts_owner_write ON posts FOR INSERT TO authenticated
  WITH CHECK (author_id = (SELECT auth.uid()));

CREATE POLICY posts_owner_update ON posts FOR UPDATE TO authenticated
  USING      (author_id = (SELECT auth.uid()))
  WITH CHECK (author_id = (SELECT auth.uid()));

CREATE POLICY posts_owner_delete ON posts FOR DELETE TO authenticated
  USING (author_id = (SELECT auth.uid()));
```

For `anon`, only the public-read policy applies. For `authenticated`, both SELECT policies are OR-combined : a user sees public posts AND their own private posts.

## Example 4 : Soft-Delete With RLS :

```sql
CREATE TABLE accounts (
  id          bigserial PRIMARY KEY,
  user_id     uuid NOT NULL,
  name        text NOT NULL,
  deleted_at  timestamptz
);

CREATE INDEX accounts_user_id_idx ON accounts (user_id);

ALTER TABLE accounts ENABLE ROW LEVEL SECURITY;

CREATE POLICY accounts_owner ON accounts AS PERMISSIVE FOR ALL TO authenticated
  USING      (user_id = (SELECT auth.uid()))
  WITH CHECK (user_id = (SELECT auth.uid()));

CREATE POLICY accounts_hide_deleted ON accounts AS RESTRICTIVE FOR SELECT TO authenticated
  USING (deleted_at IS NULL);
```

The RESTRICTIVE policy globally hides soft-deleted rows from SELECT (but still allows owner UPDATE to set `deleted_at`). To bring soft-deleted rows back into a view, query as a role without the RESTRICTIVE policy applied (e.g. an admin role with its own permissive override).

## Example 5 : Time-Bounded Access :

```sql
ALTER TABLE financials ENABLE ROW LEVEL SECURITY;

CREATE POLICY financials_business_hours ON financials AS RESTRICTIVE FOR SELECT
  USING (EXTRACT(hour FROM now() AT TIME ZONE 'UTC') BETWEEN 6 AND 22);

CREATE POLICY financials_owner ON financials FOR ALL TO authenticated
  USING      (owner_id = (SELECT auth.uid()))
  WITH CHECK (owner_id = (SELECT auth.uid()));
```

The RESTRICTIVE policy applies to every SELECT regardless of the user. Outside business hours, the table appears empty.

## Example 6 : Read-Only Shared Reference Data :

```sql
CREATE TABLE countries (code char(2) PRIMARY KEY, name text NOT NULL);

ALTER TABLE countries ENABLE ROW LEVEL SECURITY;

CREATE POLICY countries_read_all ON countries FOR SELECT TO PUBLIC
  USING (true);
```

`USING (true)` is the explicit "everyone sees everything" predicate. Combined with NO INSERT/UPDATE/DELETE policies, the table is globally readable but no role can modify it (default-deny on writes).

## Example 7 : Role-Per-Policy For Different Read Surfaces :

```sql
CREATE ROLE reporter NOLOGIN;
CREATE ROLE auditor  NOLOGIN;
GRANT reporter TO bob;
GRANT auditor  TO alice;

ALTER TABLE financials ENABLE ROW LEVEL SECURITY;

CREATE POLICY financials_reporter ON financials FOR SELECT TO reporter
  USING (amount < 10000);

CREATE POLICY financials_auditor ON financials FOR SELECT TO auditor
  USING (true);
```

Bob (member of `reporter`) sees only small amounts. Alice (member of `auditor`) sees everything. A user who is a member of BOTH roles sees the OR of both predicates (because PERMISSIVE policies are OR-combined).

## Example 8 : Cross-Tenant Lookup Via Security-Definer :

```sql
CREATE FUNCTION cross_tenant_invoice_total() RETURNS numeric
LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
  SELECT COALESCE(SUM(amount), 0) FROM invoices;
$$;

REVOKE EXECUTE ON FUNCTION cross_tenant_invoice_total FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION cross_tenant_invoice_total TO admin_role;
```

The function runs with the privileges (and BYPASSRLS attribute, if any) of the function owner. A SECURITY DEFINER function bypasses RLS only if its owner does. NEVER pass user-controlled SQL into a SECURITY DEFINER function. ALWAYS set `search_path` to a fixed value.

## Example 9 : ETL Worker With BYPASSRLS :

```sql
CREATE ROLE etl_worker LOGIN BYPASSRLS PASSWORD 'redacted';
GRANT USAGE ON SCHEMA public TO etl_worker;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA public TO etl_worker;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT ON TABLES TO etl_worker;
```

The ETL connects as `etl_worker` and bypasses every RLS policy on every table. Audit :

```sql
SELECT rolname FROM pg_roles WHERE rolbypassrls;
```

If any unexpected role appears in that result, that is a finding. Document each row.

## Example 10 : Storage Bucket Pattern (Supabase) :

```sql
-- Buckets table : owner-only access except for public buckets.
CREATE POLICY bucket_select ON storage.buckets FOR SELECT TO authenticated, anon
  USING (public = true OR owner = (SELECT auth.uid()));

CREATE POLICY bucket_insert ON storage.buckets FOR INSERT TO authenticated
  WITH CHECK (owner = (SELECT auth.uid()));

-- Objects table : a user can only access objects in buckets they own
-- OR objects they personally uploaded.
CREATE POLICY object_select ON storage.objects FOR SELECT TO authenticated
  USING (
    bucket_id IN (SELECT id FROM storage.buckets WHERE owner = (SELECT auth.uid()))
    OR owner = (SELECT auth.uid())
  );
```

The inner `SELECT id FROM storage.buckets ...` is itself subject to RLS, so the predicate composes safely : a user cannot escalate through a bucket they don't own. ALWAYS verify nested-RLS reasoning by querying as the target role with `SET ROLE`.
