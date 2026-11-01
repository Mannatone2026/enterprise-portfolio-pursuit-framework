# Anti-Patterns : postgres-syntax-full-text-search

Each entry : cause, symptom, SQLSTATE where applicable, and the fix pattern.
Verified against PostgreSQL 17 documentation.

## AP-1 : to_tsquery on raw user input

CAUSE : passing an end-user search string straight into `to_tsquery`.
`to_tsquery` is a strict parser of the `tsquery` operator grammar.

SYMPTOM : the statement aborts with SQLSTATE `42601` (syntax_error) as soon as a
user types a bare `&`, `:`, `!`, an unbalanced parenthesis, or two adjacent words
with no operator between them.

```sql
-- User types: C++ programming
SELECT * FROM doc WHERE search @@ to_tsquery('english', 'C++ programming');
-- ERROR:  syntax error in tsquery: "C++ programming"
```

FIX : use `websearch_to_tsquery`, which never raises an error.

```sql
SELECT * FROM doc WHERE search @@ websearch_to_tsquery('english', $1);
```

## AP-2 : full-text search with no GIN index

CAUSE : running `tsvector @@ tsquery` against a column or expression that has no
supporting GIN index.

SYMPTOM : every search recomputes `to_tsvector` for every row and does a Seq Scan.
Fast on 1000 rows, unusable on 1000000. `EXPLAIN` shows `Seq Scan`.

```sql
EXPLAIN SELECT id FROM doc
WHERE to_tsvector('english', body) @@ websearch_to_tsquery('english', 'x');
-- Seq Scan on doc   <- no index
```

FIX : create a GIN index on the expression or on a generated `tsvector` column.

```sql
CREATE INDEX doc_body_fts ON doc USING gin (to_tsvector('english', body));
```

## AP-3 : config mismatch between index and query

CAUSE : the index expression uses one text search config and the query uses a
different one (or the 1-argument form, which resolves config at run time).

SYMPTOM : the index exists but the planner ignores it ; `EXPLAIN` shows a Seq Scan
even though a GIN index is present. No error is raised.

```sql
CREATE INDEX ON doc USING gin (to_tsvector('english', body));
-- Query uses a different config -> index NOT used :
SELECT id FROM doc WHERE to_tsvector('simple', body) @@ to_tsquery('x');
-- Query uses the 1-arg form -> index NOT used :
SELECT id FROM doc WHERE to_tsvector(body) @@ to_tsquery('x');
```

FIX : repeat the EXACT same 2-argument expression in the query, or switch to a
generated `tsvector` column so the config lives in one place only.

## AP-4 : 1-argument to_tsvector in an index expression

CAUSE : indexing `to_tsvector(body)` (no config argument).

SYMPTOM : the 1-argument `to_tsvector` depends on the session GUC
`default_text_search_config`, so it is not immutable. The index either cannot be
created or behaves inconsistently between sessions with different GUC values.

FIX : always use the 2-argument form with an explicit config in index expressions.

```sql
CREATE INDEX ON doc USING gin (to_tsvector('english', body));
```

## AP-5 : missing coalesce around a nullable column

CAUSE : concatenating a nullable column into a `to_tsvector` input without
`coalesce`. In SQL, `anything || NULL` is `NULL`.

SYMPTOM : whenever `title` (or any source column) is `NULL`, the entire computed
`tsvector` is `NULL`, the row matches nothing, and it silently disappears from all
search results. No error is raised.

```sql
-- WRONG : one NULL column nulls the whole vector.
to_tsvector('english', title || ' ' || body)
```

FIX : wrap every nullable source column in `coalesce(col, '')`.

```sql
to_tsvector('english', coalesce(title,'') || ' ' || coalesce(body,''))
```

## AP-6 : ts_headline in WHERE or on unfiltered rows

CAUSE : calling `ts_headline` before filtering, or inside the `WHERE` clause.

SYMPTOM : `ts_headline` reparses the raw document text for every row of the table
because it cannot use the GIN index. Query time grows with table size, not with
result size.

FIX : filter and `LIMIT` first in a subquery, then call `ts_headline` only on the
surviving rows.

```sql
SELECT id, ts_headline('english', body, q) FROM (
  SELECT id, body FROM doc, websearch_to_tsquery('english',$1) q
  WHERE search @@ q ORDER BY ts_rank(search,q) DESC LIMIT 20
) t, websearch_to_tsquery('english',$1) q;
```

## AP-7 : using full-text search for substring or typo matching

CAUSE : expecting FTS to match partial words, misspellings, or substrings.

SYMPTOM : a search for `databse` (typo) or `data` (substring of `database`)
returns nothing, because FTS matches whole normalized lexemes, not characters.

FIX : use `pg_trgm` for typo tolerance and substring matching. FTS and `pg_trgm`
are complementary, not interchangeable.

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX ON t USING gin (name gin_trgm_ops);
SELECT name FROM t WHERE name % 'databse';   -- trigram similarity
```

## AP-8 : ts_rank_cd or phrase search on a positionless tsvector

CAUSE : storing a `tsvector` with positions stripped (via `strip()` or building it
without positions) and then using `ts_rank_cd` or a `<->` phrase query.

SYMPTOM : `ts_rank_cd` returns meaningless or zero ranks ; `<->` / `<N>` phrase
queries never match, because both features need lexeme positions.

FIX : keep positions in any stored `tsvector` used for ranking or phrase search.
Never call `strip()` on a `tsvector` you will rank or phrase-search.

## AP-9 : ORDER BY ts_rank without LIMIT on a large match set

CAUSE : ranking and sorting every matching row when the user only sees a page.

SYMPTOM : a query matching 500000 rows computes `ts_rank` 500000 times and sorts
them all to return 20. Slow and memory-heavy.

FIX : always pair `ORDER BY ts_rank(...) DESC` with a `LIMIT`. For deep pagination,
consider a materialized rank or keyset pagination ; see
`postgres-impl-explain-analyze`.

## AP-10 : trigger-maintained tsvector column in new code

CAUSE : writing a `BEFORE INSERT OR UPDATE` trigger to keep a `tsvector` column in
sync, as was required before PostgreSQL 12.

SYMPTOM : extra trigger code to maintain ; the column drifts out of sync if the
trigger is dropped, disabled, or a column rename is missed. No error surfaces, the
search index just silently goes stale.

FIX : on v12+ (all of v15/16/17) use a `GENERATED ALWAYS AS (...) STORED` column.
PostgreSQL maintains it automatically and it cannot drift.

```sql
ALTER TABLE doc ADD COLUMN search tsvector
  GENERATED ALWAYS AS (to_tsvector('english', coalesce(body,''))) STORED;
```

## Sources

- https://www.postgresql.org/docs/17/textsearch-intro.html
- https://www.postgresql.org/docs/17/textsearch-tables.html
- https://www.postgresql.org/docs/17/textsearch-controls.html
- https://www.postgresql.org/docs/17/textsearch-indexes.html
- https://www.postgresql.org/docs/17/pgtrgm.html
- https://www.postgresql.org/docs/17/errcodes-appendix.html
