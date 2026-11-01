# Anti-Patterns : JSONB

JSONB-specific anti-patterns with symptom, cause, SQLSTATE (where applicable), detection query, and fix. Sources are official PostgreSQL 17 docs.

---

## AP-1 : Using `json` type when queries are needed

**Symptom** : `EXPLAIN ANALYZE` shows high CPU time despite small data, dropping to fast after switching the type. No GIN index possible. Containment queries always seqscan.

**Cause** : the `json` type stores the input as text and re-parses on EVERY read access. It also cannot be GIN-indexed for containment. The only legitimate reason to use `json` is to preserve key ordering, whitespace, and duplicate keys for cryptographic or external-system reasons.

**SQLSTATE** : not an error path : performance regression.

**Fix pattern** :

```sql
-- Detect
SELECT n.nspname, c.relname, a.attname, t.typname
FROM pg_attribute a
  JOIN pg_class c ON a.attrelid = c.oid
  JOIN pg_type  t ON a.atttypid = t.oid
  JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE t.typname = 'json' AND a.attnum > 0 AND NOT a.attisdropped;

-- Migrate (online via NOT NULL DEFAULT trick if column is small) :
ALTER TABLE event ALTER COLUMN payload TYPE jsonb USING payload::jsonb;
-- Full table rewrite (ACCESS EXCLUSIVE). For large tables : multi-step with
-- a shadow column + backfill + swap. See postgres-impl-zero-downtime-ddl.
```

Source : postgresql.org/docs/17/datatype-json.html (JSON Types).

---

## AP-2 : Missing GIN index on frequent `@>` query

**Symptom** : `WHERE doc @> '...'` is slow on tables with >10k rows. `EXPLAIN` shows `Seq Scan` with `Filter : (doc @> '...'::jsonb)`. Rows-Removed-by-Filter is large.

**Cause** : `@>` containment is a CPU-resolved predicate without a supporting GIN index. No index access method other than GIN supports JSONB containment.

**SQLSTATE** : not an error path : performance regression.

**Fix pattern** :

```sql
-- Detect : tables with jsonb columns and no GIN on them
SELECT n.nspname, c.relname, a.attname
FROM pg_attribute a
  JOIN pg_class c ON a.attrelid = c.oid
  JOIN pg_namespace n ON c.relnamespace = n.oid
  JOIN pg_type t ON a.atttypid = t.oid
WHERE t.typname = 'jsonb' AND a.attnum > 0 AND NOT a.attisdropped
  AND NOT EXISTS (
    SELECT 1 FROM pg_index i
      JOIN pg_am am ON am.oid = (SELECT relam FROM pg_class WHERE oid = i.indexrelid)
    WHERE i.indrelid = c.oid
      AND am.amname = 'gin'
      AND a.attnum = ANY(i.indkey)
  );

-- Remediate :
CREATE INDEX CONCURRENTLY t_doc_gin ON t USING GIN (doc);
-- Or, if all queries are pure containment :
CREATE INDEX CONCURRENTLY t_doc_path_gin ON t USING GIN (doc jsonb_path_ops);
```

Source : postgresql.org/docs/17/datatype-json.html (jsonb Indexing), vooronderzoek §19 #15.

---

## AP-3 : `jsonb_path_ops` opclass used with key-existence queries

**Symptom** : `EXPLAIN` shows Seq Scan on `WHERE doc ? 'key'` (or `?|`, `?&`) even though a GIN index exists. Query latency does not improve after `REINDEX`.

**Cause** : the `jsonb_path_ops` opclass indexes only key-path/value hashes, NOT the keys themselves. Key-existence operators (`?`, `?|`, `?&`) are NOT in this opclass and the planner cannot use the index.

**SQLSTATE** : not an error path : silent index miss.

**Fix pattern** :

```sql
-- Detect SECDEF... wait, wrong skill. Detect GIN opclass per index :
SELECT i.indexrelid::regclass, opc.opcname
FROM pg_index i
  JOIN pg_opclass opc ON opc.oid = ANY(i.indclass::oid[])
  JOIN pg_am am ON am.oid = (SELECT relam FROM pg_class WHERE oid = i.indexrelid)
WHERE am.amname = 'gin';

-- Remediate : add a default-opclass index alongside, OR rewrite query to @>
CREATE INDEX CONCURRENTLY t_doc_gin ON t USING GIN (doc);
-- Or query in containment form :
-- SELECT * FROM t WHERE doc @> '{"refund": null}'
-- but : type-strictness means "any value" containment must use jsonpath @?
```

Source : postgresql.org/docs/17/datatype-json.html (jsonb Indexing).

---

## AP-4 : `(doc->'k')::text` to get a string value

**Symptom** : code expects `'Alice'` but receives `'"Alice"'` (with JSON quotes). Downstream string comparisons fail. Hash mismatches.

**Cause** : `->` returns `jsonb`. Casting `jsonb` to `text` produces the JSON-encoded string form, which preserves quotes around string scalars. `->>` projects to text directly without JSON encoding.

**SQLSTATE** : not an error path : silent string corruption.

**Fix pattern** :

```sql
-- Wrong
SELECT (doc->'name')::text FROM t;       -- '"Alice"'

-- Right
SELECT doc->>'name' FROM t;              -- 'Alice'

-- Detect callers : grep through migrations / queries for ::text casts on -> results
-- (manual code review ; no system catalog tells you this)
```

Source : postgresql.org/docs/17/functions-json.html.

---

## AP-5 : Read-modify-write of whole JSONB in app code

**Symptom** : concurrent UPDATEs lose each other's changes ("last writer wins" on the whole document, not per-key). Bug surfaces only under load.

**Cause** : the application reads the whole `doc`, mutates it in memory, writes the whole `doc` back. Two sessions racing on different keys each fetch the same baseline ; one overwrites the other's commit.

**SQLSTATE** : not an error path : data loss under concurrency.

**Fix pattern** :

```sql
-- Wrong : app code does
--   doc = SELECT payload FROM event WHERE id = $1;
--   doc.status = 'paid';
--   UPDATE event SET payload = doc_serialized WHERE id = $1;

-- Right : let PostgreSQL do the merge :
UPDATE event SET payload = jsonb_set(payload, '{status}', '"paid"')
WHERE id = $1;

-- Or with subscript (v14+) :
UPDATE event SET payload['status'] = '"paid"' WHERE id = $1;
```

Each `jsonb_set` / subscript-update is atomic with respect to the row's other concurrent updates : the lock is per-row, and two sessions updating different keys serialize at the row lock but each preserves the other's changes (the second reads the post-first-update value).

Source : postgresql.org/docs/17/functions-json.html (jsonb_set), postgresql.org/docs/17/datatype-json.html (Subscripting).

---

## AP-6 : Type-strict containment mismatch (`'{"qty":10}' @> '{"qty":"10"}'`)

**Symptom** : `WHERE doc @> '...'` returns zero rows when the application "knows" there are matches. The query worked in tests with hand-typed JSON literals but fails with app-constructed JSON.

**Cause** : JSONB containment is type-strict. Number `10` is NOT contained in string `"10"`. The app constructed the RHS by string-interpolating user input as a string ; the document stores the value as a number.

**SQLSTATE** : not an error path : silent zero-result.

**Fix pattern** :

```sql
-- Wrong : RHS built from a text variable, becomes JSON string
PREPARE p1(text) AS SELECT * FROM event WHERE payload @> ('{"qty":' || $1 || '}')::jsonb;
EXECUTE p1('10');
-- effectively : WHERE payload @> '{"qty":10}' -- works
EXECUTE p1('"10"');
-- effectively : WHERE payload @> '{"qty":"10"}' -- zero rows if doc has number

-- Right : use jsonb_build_object with a typed argument
PREPARE p2(int) AS SELECT * FROM event WHERE payload @> jsonb_build_object('qty', $1);
EXECUTE p2(10);
-- jsonb_build_object honors SQL types : int -> JSON number.
```

Source : postgresql.org/docs/17/datatype-json.html (Containment and Existence).

---

## AP-7 : `WHERE doc @> '{}'` (empty-object containment)

**Symptom** : query returns every non-null row, GIN bitmap-scan touches the whole index, plan is the same as Seq Scan but slower.

**Cause** : the empty object `{}` is contained in every JSONB value. The predicate matches all rows. The planner uses the GIN index but the bitmap covers everything ; the rechecker scans them all anyway.

**SQLSTATE** : not an error path : design mistake.

**Fix pattern** : remove the empty-containment from the predicate. If you meant "is the column non-null" use `doc IS NOT NULL`. If you meant "does the column have any keys" use `jsonb_typeof(doc) = 'object' AND doc <> '{}'::jsonb`.

```sql
-- Wrong
SELECT * FROM event WHERE payload @> '{}' AND id < 1000;

-- Right
SELECT * FROM event WHERE payload IS NOT NULL AND id < 1000;
```

Source : postgresql.org/docs/17/datatype-json.html (Containment semantics).

---

## AP-8 : Indexing `(doc->>'huge_field')` with B-tree

**Symptom** : `CREATE INDEX` succeeds, but `INSERT` raises `54000` (`program_limit_exceeded`) or `54011` (`too_many_columns`) intermittently. "index row size exceeds maximum 2712 for index" appears in logs.

**Cause** : B-tree index entries are capped at ~1/3 of an 8KB page. A `->>` extraction that returns large text (descriptions, long arrays serialized as text) blows past the limit.

**SQLSTATE** : `54000` `program_limit_exceeded` on offending INSERT.

**Fix pattern** : do not B-tree-index large-payload fields. Use GIN (if the field is a JSON sub-structure inside a JSONB), or hash a digest into a side column.

```sql
-- Wrong
CREATE INDEX ON t ((doc->>'description_long_text'));

-- Right options :
-- 1) Index a digest
CREATE INDEX ON t (md5(doc->>'description_long_text'));
-- 2) Use full-text search (tsvector + GIN)
CREATE INDEX ON t USING GIN (to_tsvector('english', doc->>'description_long_text'));
-- 3) Lift the field to its own text column (recommended if accessed often)
ALTER TABLE t ADD COLUMN description text GENERATED ALWAYS AS (doc->>'description_long_text') STORED;
CREATE INDEX ON t USING GIN (to_tsvector('english', description));
```

Source : postgresql.org/docs/17/sql-createindex.html (index row size limits), postgresql.org/docs/17/textsearch-indexes.html.

---

## AP-9 : `jsonb_set` create_missing default surprises

**Symptom** : an `UPDATE ... SET doc = jsonb_set(doc, '{coupon}', '"X10"')` is intended to UPDATE an existing key but silently CREATES it when absent. Downstream code that branches on "has the row been coupon-flagged" misbehaves.

**Cause** : `jsonb_set(target, path, new_value, create_if_missing)` defaults `create_if_missing` to `true`. The intent of update-only is not the default.

**SQLSTATE** : not an error path : silent state change.

**Fix pattern** :

```sql
-- Wrong (silently creates if missing)
UPDATE event SET payload = jsonb_set(payload, '{coupon}', '"X10"') WHERE id = 42;

-- Right (refuse to create) : pass false explicitly
UPDATE event SET payload = jsonb_set(payload, '{coupon}', '"X10"', false) WHERE id = 42;
-- If the key does not exist, payload is returned unchanged.
```

Source : postgresql.org/docs/17/functions-json.html (jsonb_set signature : "create_if_missing boolean DEFAULT true").

---

## AP-10 : `jsonpath` strict mode raising 2203B in production

**Symptom** : intermittent `2203B sql_json_member_not_found` errors from `jsonb_path_query` calls when a document is missing an expected key. The query worked on the test fixture but breaks on real data.

**Cause** : `strict` jsonpath mode raises on missing members. `lax` mode (the default) silently returns no rows. The query is using `strict` either explicitly or via `JSON_VALUE ... ON ERROR ERROR`.

**SQLSTATE** : `2203A` / `2203B` / `2203E` / `2203F` (SQL/JSON Errors class 2203).

**Fix pattern** :

```sql
-- Wrong (strict)
SELECT jsonb_path_query(payload, 'strict $.missing_field') FROM event;
-- ERROR: jsonpath member accessor can only be applied to an object 2203B

-- Right (default lax)
SELECT jsonb_path_query(payload, '$.missing_field') FROM event;
-- (no rows)

-- Or, with explicit silent flag (suppresses errors and returns empty)
SELECT jsonb_path_query(payload, 'strict $.missing_field', '{}', true) FROM event;
-- (no rows, no error)

-- For JSON_VALUE / JSON_EXISTS in v17 :
JSON_VALUE(payload, 'strict $.missing_field' DEFAULT 'n/a' ON ERROR);
```

Source : postgresql.org/docs/17/functions-json.html (jsonpath strict vs lax), postgresql.org/docs/17/errcodes-appendix.html (Class 2203).
