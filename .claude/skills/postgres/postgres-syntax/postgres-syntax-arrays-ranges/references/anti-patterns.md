# Anti-Patterns : Arrays, Ranges, Multiranges

Each entry : cause, symptom, fix-pattern, and SQLSTATE where a specific error
code applies. Verified against PostgreSQL 17 documentation.

## AP-1 : CSV string in a text column instead of a native array

**Cause** : a list of values is stored as `'tag1,tag2,tag3'` in a `text`
column instead of `text[]`.

**Symptom** : every membership query is `WHERE tags LIKE '%sql%'`, which also
matches `mysql` and `nosql` ; the column cannot be indexed for element search ;
no per-element type checking, so `'1,2,abc'` slips into a numeric list.

**Fix** : declare the column as an array and add a GIN index.

```sql
-- WRONG
CREATE TABLE article (tags text);            -- 'sql,index,postgres'

-- RIGHT
CREATE TABLE article (tags text[] NOT NULL DEFAULT '{}');
CREATE INDEX article_tags_gin ON article USING GIN (tags);
SELECT * FROM article WHERE tags @> ARRAY['sql'];
```

**SQLSTATE** : none ; this is a slow, unsafe design, not a raised error.

## AP-2 : Range-overlap query without a GiST index

**Cause** : a range column is filtered with `&&` / `@>` but has only a btree
index, or no index at all.

**Symptom** : `EXPLAIN` shows `Seq Scan` with `Filter: (valid && ...)` ; query
time grows linearly with table size. Btree cannot serve range-overlap operators.

**Fix** : create a GiST index on the range column.

```sql
CREATE INDEX price_band_valid_gist ON price_band USING GIST (valid);
SELECT * FROM price_band WHERE valid && daterange('2026-03-15','2026-05-15');
```

**SQLSTATE** : none ; the query returns correct rows, only slowly.

## AP-3 : Inclusive upper bound causing off-by-one double-match

**Cause** : adjacent ranges are written with `[]` (both bounds inclusive), so
a boundary value belongs to two ranges at once.

**Symptom** : a date or timestamp at a band boundary matches two rows ; with an
exclusion constraint the second adjacent row is rejected even though the
intervals were meant to abut.

**Fix** : use the canonical `[)` form : lower inclusive, upper exclusive. The
two-argument range constructors already produce `[)`.

```sql
-- WRONG : 2026-04-01 belongs to BOTH bands
daterange('2026-01-01','2026-04-01','[]')
daterange('2026-04-01','2026-07-01','[]')

-- RIGHT : 2026-04-01 belongs only to the second band
daterange('2026-01-01','2026-04-01')   -- [) by default
daterange('2026-04-01','2026-07-01')
```

**SQLSTATE** : `23P01` (`exclusion_violation`) when an exclusion constraint
catches the unintended overlap.

## AP-4 : Expecting `= ANY(column_array)` to use a GIN index

**Cause** : a membership filter is written as `WHERE value = ANY(tags)`,
assuming the GIN index on `tags` accelerates it.

**Symptom** : `EXPLAIN` shows the GIN index is ignored ; the planner scans each
row and walks the array per row. `= ANY` is a scalar quantified comparison, not
a GIN-indexable operator.

**Fix** : rewrite as a containment test with `@>`.

```sql
-- WRONG : not GIN-indexable
SELECT * FROM article WHERE 'sql' = ANY (tags);

-- RIGHT : GIN-indexable
SELECT * FROM article WHERE tags @> ARRAY['sql'];
```

**SQLSTATE** : none ; correct results, missed index.

## AP-5 : Application-side conflict check before INSERT

**Cause** : overlapping-booking prevention is done with a `SELECT` to look for
conflicts, then an `INSERT` if none found.

**Symptom** : under concurrency two transactions both run the `SELECT`, both
see no conflict, both `INSERT` : a double booking lands in the table. The check
and the insert are not atomic.

**Fix** : enforce non-overlap with an `EXCLUDE USING gist` constraint so the
database rejects the conflict atomically.

```sql
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE TABLE room_reservation (
    room   text    NOT NULL,
    during tsrange NOT NULL,
    EXCLUDE USING gist (room WITH =, during WITH &&)
);
```

**SQLSTATE** : `23P01` (`exclusion_violation`) on the losing transaction ;
catch it in application code and report the conflict to the user.

## AP-6 : Missing btree_gist for an EXCLUDE mixing `=` and `&&`

**Cause** : an exclusion constraint scopes overlap by an equality column
(`room WITH =`) but `btree_gist` is not installed. GiST has no built-in
equality opclass for `text` or `integer`.

**Symptom** : `CREATE TABLE` fails with `data type text has no default operator
class for access method "gist"`.

**Fix** : install the extension before creating the table.

```sql
CREATE EXTENSION IF NOT EXISTS btree_gist;
```

**SQLSTATE** : `42704` (`undefined_object`) raised at `CREATE TABLE` time.

## AP-7 : Treating an out-of-bounds array subscript as an error

**Cause** : code assumes `arr[99]` on a 3-element array raises an exception and
wraps it in error handling.

**Symptom** : the error handler never fires ; `arr[99]` silently returns `NULL`,
which then propagates into comparisons as an unknown, producing rows that
quietly drop out of a `WHERE` clause.

**Fix** : guard with `array_length` or `cardinality` before subscripting, and
treat the `NULL` result explicitly.

```sql
SELECT CASE
         WHEN array_length(pay_by_quarter, 1) >= 4
           THEN pay_by_quarter[4]
         ELSE NULL
       END
FROM sal_emp;
```

**SQLSTATE** : `2202E` (`array_subscript_error`) IS raised for an invalid slice
expression or for assigning past the end of a multidimensional array, but NOT
for plain out-of-range element reads.

## AP-8 : Using an array as a searchable many-to-many relationship

**Cause** : foreign keys to another table are stored as an `id[]` array column
to avoid a junction table.

**Symptom** : no referential integrity (a deleted parent leaves dangling ids in
arrays) ; joins back to the referenced table need `unnest` and lose index
efficiency ; per-relationship attributes (added_at, weight) have nowhere to go.

**Fix** : use a proper junction table with foreign keys. Reserve arrays for
bare scalar lists that are read and written as a whole and never need a foreign
key.

```sql
-- WRONG
CREATE TABLE post (id bigint PRIMARY KEY, author_ids bigint[]);

-- RIGHT
CREATE TABLE post_author (
    post_id   bigint REFERENCES post(id) ON DELETE CASCADE,
    author_id bigint REFERENCES author(id),
    PRIMARY KEY (post_id, author_id)
);
```

**SQLSTATE** : none ; this is a modelling error, not a raised error.
