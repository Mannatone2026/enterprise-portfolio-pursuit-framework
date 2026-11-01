---
name: postgres-syntax-arrays-ranges
description: >
  Use when storing list-valued columns, modelling time/number intervals, or preventing overlapping bookings with exclusion constraints.
  Prevents seqscan on range-overlap queries from missing GiST index, CSV-in-text instead of native array, and off-by-one errors from inclusive vs exclusive range bounds.
  Covers array literals + operators (= ANY, @>, &&, ||), unnest WITH ORDINALITY, array_agg, range types (int4range, tstzrange, daterange), range operators (@>, &&, -|-), multirange (v14+), GiST exclusion constraints for non-overlap.
  Keywords: array, ARRAY, ANY, unnest, array_agg, range type, int4range, tstzrange, daterange, multirange, range overlap, exclusion constraint, GiST, EXCLUDE USING gist, overlapping bookings, how to store a list, prevent double booking, range query is slow
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-syntax-arrays-ranges

## Quick Reference :

PostgreSQL has two native multi-value types that replace ad-hoc string hacks.
**Arrays** store an ordered list inside one column (`int[]`, `text[]`). **Range
types** store an interval (`[lower, upper)`) as a single value with built-in
overlap and containment logic.

Two rules drive every decision here :

1. ALWAYS use a native array instead of a comma-separated string in a `text`
   column. CSV-in-text cannot be indexed, validated, or searched element-wise.
2. ALWAYS build a GiST index (or an `EXCLUDE USING gist` constraint) before
   running range-overlap (`&&`) queries at scale. Without it the planner does a
   sequential scan on every row.

```
Need to store many values in one row?       -> array column + GIN index
Need to model a start..end interval?         -> range type ([) bounds)
Need to forbid two rows overlapping in time? -> EXCLUDE USING gist (... WITH &&)
Need a union of disjoint intervals?          -> multirange + range_agg
```

Array subscripts are **1-based**. Range bounds default to `[)` : lower
inclusive, upper exclusive. Both defaults are load-bearing : ignoring them
produces silent off-by-one bugs.

## When To Use This Skill :

ALWAYS use this skill when :
- A column must hold a list of values (tags, role ids, scores per quarter)
- Modelling a time, date, or numeric interval (booking window, price band, validity period)
- Preventing overlapping reservations of a shared resource (room, vehicle, seat)
- A range-overlap or array-containment query is slow and you suspect a missing index
- Merging or aggregating many intervals into their union

NEVER use this skill for :
- JSON document storage or key-value attributes : use `postgres-syntax-jsonb`
- Generating a row series from a single range : use `generate_series` (set-returning function)
- Spatial geometry intervals : use PostGIS range/geometry types

## Decision Trees :

### Array column vs separate child table :

```
Will you filter, join, or aggregate on the individual values?
├─ YES, and values reference another entity   -> child table + FK (NOT an array)
├─ YES, simple membership / containment only  -> array column + GIN index
└─ NO, values are read/written as a whole set -> array column (no index needed)

Do values need their own attributes (qty, added_at)?
└─ YES -> child table. An array stores bare scalars only.
```

### Range type vs two scalar columns :

```
Do you store a start and an end of the same subtype?
├─ NO  -> two ordinary columns
└─ YES -> Do you ever query overlap, containment, or adjacency?
          ├─ NO  -> two columns is acceptable
          └─ YES -> range type. Overlap (&&) on two columns needs
                    (start_a < end_b AND start_b < end_a) by hand;
                    a range type makes it one operator + one GiST index.
```

### Which index for a multi-value column :

```
Array column, queried with @> <@ && or = ANY -> GIN index
Range column, queried with @> <@ && -|-      -> GiST index
Range column, exact equality only            -> btree (default) is enough
EXCLUDE constraint on a range                -> GiST (the constraint creates it)
```

### Exclusion constraint scoping :

```
Forbid ANY two rows overlapping?
└─ EXCLUDE USING gist (during WITH &&)

Forbid overlap only within the same group (same room, same vehicle)?
└─ CREATE EXTENSION btree_gist;
   EXCLUDE USING gist (room WITH =, during WITH &&)
   (the = operator needs btree_gist; && needs core GiST)
```

## Patterns :

### Pattern 1 : Native array column instead of CSV text

ALWAYS : declare the column with the element type plus `[]`.
NEVER : store `'1,2,3'` in a `text` column and split it at query time.

```sql
CREATE TABLE article (
    id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title text NOT NULL,
    tags  text[] NOT NULL DEFAULT '{}'
);

INSERT INTO article (title, tags)
VALUES ('Indexing 101', ARRAY['postgres', 'index', 'performance']);
```

WHY : a `text[]` column is type-checked per element, supports `@>` / `&&` /
`= ANY`, and is GIN-indexable. A CSV string is opaque : every search becomes a
full-table `LIKE '%tag%'` scan that also matches substrings of other tags.

### Pattern 2 : Membership test with `= ANY` and the GIN-indexed `@>`

ALWAYS : use `@>` (contains) for indexed membership filters.
NEVER : assume `= ANY(array)` uses a GIN index. It does not.

```sql
CREATE INDEX article_tags_gin ON article USING GIN (tags);

-- Index-eligible : "rows whose tags contain 'postgres'"
SELECT id, title FROM article WHERE tags @> ARRAY['postgres'];

-- Overlap : "rows sharing ANY tag with this set"
SELECT id FROM article WHERE tags && ARRAY['index', 'sql'];

-- = ANY scans the array per row; fine for a small literal list,
-- NOT index-accelerated against a column array.
SELECT * FROM article WHERE 'postgres' = ANY (tags);
```

WHY : the `array_ops` GIN opclass indexes `@>`, `<@`, and `&&`. `= ANY` is a
row-by-row scalar comparison, so it never consults the GIN index on `tags`.
Rewrite membership filters as `tags @> ARRAY[value]` to get the index.

### Pattern 3 : Expand arrays to rows with `unnest` and `WITH ORDINALITY`

ALWAYS : add `WITH ORDINALITY` when the element position matters.
NEVER : rely on `unnest` output order without `ORDINALITY` if you then need the index.

```sql
-- One row per tag, preserving 1-based position
SELECT a.id, t.tag, t.pos
FROM article a,
     LATERAL unnest(a.tags) WITH ORDINALITY AS t(tag, pos);

-- Inverse of array_agg : rebuild an array from rows
SELECT array_agg(tag ORDER BY pos) AS tags
FROM (SELECT 'x' AS tag, 1 AS pos) s
GROUP BY ();
```

WHY : `unnest(arr) WITH ORDINALITY AS t(value, n)` adds a `bigint` column with
the element's ordinal position. `array_agg(expr ORDER BY ...)` is the reverse :
it collapses rows back into an ordered array. Pair them to transform array data
through relational operators.

### Pattern 4 : Range type with explicit `[)` bounds

ALWAYS : keep the default `[)` form : lower inclusive, upper exclusive.
NEVER : mix `[]` and `[)` across rows of the same column. Inconsistent bounds
break overlap math.

```sql
CREATE TABLE price_band (
    sku        text NOT NULL,
    valid      daterange NOT NULL,
    unit_price numeric(10,2) NOT NULL
);

-- Two-arg constructor always yields [) ; 3-arg form sets bounds explicitly
INSERT INTO price_band VALUES
    ('A-1', daterange('2026-01-01', '2026-04-01'), 9.99),   -- Jan..Mar
    ('A-1', daterange('2026-04-01', '2026-07-01'), 8.99);   -- Apr..Jun

-- No off-by-one : Apr 1 belongs to the second band, not both
SELECT unit_price FROM price_band
WHERE sku = 'A-1' AND valid @> DATE '2026-04-01';
```

WHY : with `[)` bounds adjacent ranges (`[Jan,Apr)` and `[Apr,Jul)`) touch
without overlapping, so a boundary date lands in exactly one row. `[]` on both
would make April 1 match both bands and trigger any exclusion constraint.

### Pattern 5 : GiST index for range-overlap queries

ALWAYS : create a GiST index before filtering a range column with `&&` / `@>`.
NEVER : leave a range-overlap query on a btree-only column : btree cannot
answer `&&`, so the planner falls back to a sequential scan.

```sql
CREATE INDEX price_band_valid_gist ON price_band USING GIST (valid);

-- Index-eligible overlap query
SELECT * FROM price_band
WHERE valid && daterange('2026-03-15', '2026-05-15');
```

WHY : range overlap is not a btree-orderable predicate. Only GiST (and SP-GiST)
expose `&&`, `@>`, `<@`, `-|-` to the planner. Without the GiST index, a
`WHERE valid && ...` filter reads every heap row.

### Pattern 6 : `EXCLUDE USING gist` to prevent overlapping bookings

ALWAYS : enforce non-overlap with an exclusion constraint, not application code.
NEVER : check for conflicts with a `SELECT` then `INSERT` : that race window
lets two concurrent transactions both pass the check.

```sql
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE room_reservation (
    id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    room   text   NOT NULL,
    during tsrange NOT NULL,
    EXCLUDE USING gist (room WITH =, during WITH &&)
);

INSERT INTO room_reservation (room, during)
VALUES ('123A', '[2026-05-20 14:00, 2026-05-20 15:00)');

-- Same room, overlapping window -> rejected with SQLSTATE 23P01
INSERT INTO room_reservation (room, during)
VALUES ('123A', '[2026-05-20 14:30, 2026-05-20 15:30)');
```

WHY : the database enforces "no two rows share a room AND overlap in time"
atomically under concurrency. `room WITH =` needs the `btree_gist` extension
(GiST has no built-in equality opclass for `text`); `during WITH &&` uses the
core range GiST opclass. The constraint also builds the GiST index for free.

### Pattern 7 : Merge intervals with multirange and `range_agg`

ALWAYS : use `range_agg` to collapse many ranges into their union.
NEVER : hand-roll interval merging with sorted loops in SQL.

```sql
-- range_agg returns a multirange : the union of all input ranges
SELECT range_agg(during) AS booked
FROM room_reservation
WHERE room = '123A';
-- -> {[2026-05-20 14:00,2026-05-20 15:00)}  (disjoint parts stay separate)

-- A multirange literal is a brace-wrapped set of ranges
SELECT '{[1,5), [10,15)}'::int4multirange @> 3;   -- t
SELECT '{[1,5), [10,15)}'::int4multirange @> 7;   -- f
```

WHY : multirange types (`int4multirange`, `tstzmultirange`, ...) represent a
union of disjoint ranges as one value. `range_agg` is the aggregate that builds
one. Multirange and `range_agg` exist since v14, so they are available in every
supported version (15, 16, 17).

## Anti-Patterns :

(Short list, full detail in references/anti-patterns.md)

- CSV string in a `text` column instead of a native array : no index, no type check
- `WHERE col && range` on a btree-only range column : seqscan, SQLSTATE not raised but query is slow
- `[]` inclusive upper bound on adjacent ranges : off-by-one double-match
- `= ANY(column_array)` expecting GIN acceleration : never index-eligible, rewrite as `@>`
- Application-side conflict check before INSERT : race condition, use `EXCLUDE USING gist`
- Missing `btree_gist` for an `EXCLUDE` mixing `=` and `&&` : SQLSTATE 42704 (undefined object)
- Out-of-bounds array subscript treated as an error : it returns NULL, not SQLSTATE 2202E

## Reference Links :

- [references/methods.md](references/methods.md) : Full operator and function tables for arrays, ranges, multiranges
- [references/examples.md](references/examples.md) : Working version-annotated code samples
- [references/anti-patterns.md](references/anti-patterns.md) : Anti-patterns with cause, symptom, fix, SQLSTATE

## See Also :

- postgres-syntax-jsonb : when values are documents or key-value attributes, not a flat list
- postgres-impl-indexing : GIN and GiST index internals, opclass selection, bloat
- postgres-syntax-lateral-joins : `LATERAL unnest(...)` to expand array columns per row
- postgres-errors-constraints : handling SQLSTATE 23P01 exclusion violations in application code
- Vooronderzoek section : §5 (Query Features), §19 (Anti-Patterns)
- Official docs : https://www.postgresql.org/docs/17/arrays.html , https://www.postgresql.org/docs/17/rangetypes.html
