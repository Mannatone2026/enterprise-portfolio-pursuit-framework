# Examples : Schema Reviewer

Worked schema reviews and before/after DDL per check. Version annotations :
`v15+` works on 15, 16, 17 ; `v10+` marks the feature's introduction.

## Worked review 1 : a multi-tenant tasks schema

Schema under review :

```sql
CREATE TABLE "Tenants" (
    "tenantId" serial,
    name text
);

CREATE TABLE "Tasks" (
    "taskId"    serial,
    "tenantId"  integer REFERENCES "Tenants" ("tenantId"),
    title       text,
    tags        text,                       -- 'urgent,backend,q3'
    created     timestamp,
    done        boolean
);
```

Review output :

```
PostgreSQL Schema Review
========================
Scope   : 2 tables, 8 findings

[CRITICAL] public."Tenants"
  Problem : no primary key; "tenantId" is only a serial column
  Fix     : tenant_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY
  Cite    : postgres-core-schema-design

[CRITICAL] public."Tasks"
  Problem : no primary key
  Fix     : task_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY
  Cite    : postgres-core-schema-design

[CRITICAL] public."Tasks"
  Problem : multi-tenant table ("tenantId" column) with no RLS and no
            documented tenant-scoping strategy
  Fix     : ALTER TABLE tasks ENABLE ROW LEVEL SECURITY + a tenant policy,
            or document the enforced scoping layer
  Cite    : postgres-core-rls-policies

[WARNING] public."Tasks"."tenantId"
  Problem : foreign-key column with no index
  Fix     : CREATE INDEX ON tasks (tenant_id)
  Cite    : postgres-impl-indexing-strategy

[WARNING] public."Tenants"."tenantId", public."Tasks"."taskId"
  Problem : serial columns instead of identity columns
  Fix     : bigint GENERATED ALWAYS AS IDENTITY
  Cite    : postgres-syntax-dml-ddl

[WARNING] public."Tenants", public."Tasks" (and all camelCase columns)
  Problem : PascalCase / camelCase identifiers force double-quoting everywhere
  Fix     : rename to snake_case (tenants, tasks, tenant_id, task_id)
  Cite    : postgres-core-schema-design

[WARNING] public."Tasks".tags
  Problem : structured data (comma-separated tags) stored in a text column
  Fix     : a text[] array, a jsonb column, or a task_tags child table
  Cite    : postgres-syntax-jsonb

[WARNING] public."Tasks".created
  Problem : timestamp without time zone for an event time
  Fix     : created_at timestamptz NOT NULL DEFAULT now()
  Cite    : postgres-core-schema-design

Verdict : POOR   (3 CRITICAL, 5 WARNING, 0 INFO)
```

## Worked review 2 : a clean schema, INFO only

```sql
CREATE TABLE app.invoices (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id bigint NOT NULL REFERENCES app.customers (id),
    amount      money NOT NULL,            -- INFO: prefer numeric
    issued_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON app.invoices (customer_id);
```

```
PostgreSQL Schema Review
========================
Scope   : 1 table, 1 finding

[INFO] app.invoices.amount
  Problem : money type is locale-dependent (lc_monetary) with fixed scale
  Fix     : numeric(12,2) plus an explicit currency column
  Cite    : postgres-core-schema-design

Verdict : NEEDS-WORK -> GOOD?   no: INFO does not block GOOD
Verdict : GOOD   (0 CRITICAL, 0 WARNING, 1 INFO)
```

INFO findings never block a `GOOD` verdict.

## Before / after : S-1 missing primary key

```sql
-- BEFORE : no row identity
CREATE TABLE events (occurred_at timestamptz, payload jsonb);

-- AFTER (v10+)
CREATE TABLE events (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at timestamptz NOT NULL,
    payload     jsonb NOT NULL
);
```

## Before / after : S-4 SERIAL to IDENTITY

```sql
-- BEFORE : detached sequence, ownership and GRANT quirks
CREATE TABLE products (id serial PRIMARY KEY, name text);

-- AFTER (v10+) : SQL-standard identity column
CREATE TABLE products (
    id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL
);
```

## Before / after : S-2 + S-7 tenant isolation with RLS

```sql
-- BEFORE : multi-tenant, scoping left to every query by hand
CREATE TABLE documents (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id bigint NOT NULL,
    body      text
);

-- AFTER : RLS enforces the tenant filter; FORCE so the owner is covered too
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE  ROW LEVEL SECURITY;   -- S-7: owner not exempt

CREATE POLICY tenant_isolation ON documents
  USING      (tenant_id = current_setting('app.tenant_id')::bigint)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::bigint);

CREATE INDEX ON documents (tenant_id);             -- index the policy column
```

Without `FORCE`, an application connecting as the table owner bypasses the
policy entirely and S-2 is not actually solved.

## Before / after : S-6 redundant index

```sql
-- BEFORE : idx_a is fully covered by idx_ab
CREATE INDEX idx_ab ON orders (customer_id, status);
CREATE INDEX idx_a  ON orders (customer_id);        -- redundant

-- AFTER : drop the redundant one; (customer_id, status) still serves
-- queries that filter on customer_id alone
DROP INDEX idx_a;
```

`(customer_id)` is a leading prefix of `(customer_id, status)`. A standalone
index on `(status)` would NOT be redundant: it is not a leading prefix.

## Before / after : S-8 structured data in text

```sql
-- BEFORE : the database cannot index or constrain the inner values
CREATE TABLE users (id bigint PRIMARY KEY, roles text);  -- 'admin,billing'

-- AFTER : a typed child table, queryable and constrainable
CREATE TABLE user_roles (
    user_id bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    role    text   NOT NULL CHECK (role IN ('admin','billing','viewer')),
    PRIMARY KEY (user_id, role)
);
CREATE INDEX ON user_roles (user_id);
```

## Before / after : S-10 normalization (repeating group)

```sql
-- BEFORE : 1NF violation, a repeating group
CREATE TABLE contacts (
    id     bigint PRIMARY KEY,
    name   text NOT NULL,
    phone1 text, phone2 text, phone3 text
);

-- AFTER : the repeating group extracted to a child table
CREATE TABLE contacts (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                       name text NOT NULL);
CREATE TABLE contact_phones (
    contact_id bigint NOT NULL REFERENCES contacts (id) ON DELETE CASCADE,
    phone      text   NOT NULL,
    kind       text   NOT NULL DEFAULT 'other',
    PRIMARY KEY (contact_id, phone)
);
CREATE INDEX ON contact_phones (contact_id);
```

If three phone slots are a deliberate fixed business rule, the reviewer reports
S-10 as a finding AND asks the author to confirm and document that intent,
rather than hard-failing it.
