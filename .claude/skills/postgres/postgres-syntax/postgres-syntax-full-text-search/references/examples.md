# Examples : postgres-syntax-full-text-search

Working, version-annotated code samples. Every example targets PostgreSQL 15, 16,
and 17 ; features newer than v15 are annotated. Verified against the PostgreSQL 17
documentation.

## 1. Inspect the normalization pipeline

```sql
-- See exactly what to_tsvector produces : lexemes with positions. (v15+)
SELECT to_tsvector('english', 'a fat cat sat on a mat - it ate a fat rats');
-- 'ate':9 'cat':3 'fat':2,11 'mat':7 'rat':12 'sat':4

-- The 'simple' config does NOT stem and keeps stop words. (v15+)
SELECT to_tsvector('simple', 'The Fat Rats');
-- 'fat':2 'rats':3 'the':1

-- Compare query constructors on the same input. (v15+)
SELECT to_tsquery('english', 'fat & rats')        AS to_tsquery,
       plainto_tsquery('english', 'the fat rats') AS plainto,
       phraseto_tsquery('english', 'the fat rats')AS phraseto,
       websearch_to_tsquery('english', 'fat rats')AS websearch;
-- to_tsquery   : 'fat' & 'rat'
-- plainto      : 'fat' & 'rat'
-- phraseto     : 'fat' <-> 'rat'
-- websearch    : 'fat' & 'rat'
```

## 2. Minimal search : expression index

```sql
-- Schema. (v15+)
CREATE TABLE article (
  id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  title text NOT NULL,
  body  text NOT NULL
);

-- GIN index on a 2-argument to_tsvector expression. (v15+)
CREATE INDEX article_body_fts ON article
  USING gin (to_tsvector('english', body));

-- Query : the to_tsvector config MUST match the index expression exactly. (v15+)
SELECT id, title
FROM article
WHERE to_tsvector('english', body) @@ websearch_to_tsquery('english', $1);
```

## 3. Recommended setup : generated tsvector column

```sql
-- Generated stored column : auto-maintained, no config repeated in queries.
-- GENERATED ALWAYS AS ... STORED requires v12+ (available in v15/16/17). (v12+)
CREATE TABLE doc (
  id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  title  text,
  body   text,
  search tsvector GENERATED ALWAYS AS (
    to_tsvector('english',
      coalesce(title, '') || ' ' || coalesce(body, ''))
  ) STORED
);

CREATE INDEX doc_search_idx ON doc USING gin (search);

-- Query references the column directly. (v15+)
SELECT id, title
FROM doc
WHERE search @@ websearch_to_tsquery('english', $1);
```

## 4. Weighted search with ranking

```sql
-- setweight marks title as A (most important), abstract B, body D. (v12+)
CREATE TABLE paper (
  id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  title    text,
  abstract text,
  body     text,
  search   tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('english', coalesce(title, '')),    'A') ||
    setweight(to_tsvector('english', coalesce(abstract, '')), 'B') ||
    setweight(to_tsvector('english', coalesce(body, '')),     'D')
  ) STORED
);

CREATE INDEX paper_search_idx ON paper USING gin (search);

-- Rank by relevance. The query value is computed once via the FROM-clause join. (v15+)
SELECT id, title, ts_rank(search, q) AS rank
FROM paper, websearch_to_tsquery('english', $1) AS q
WHERE search @@ q
ORDER BY rank DESC
LIMIT 20;

-- Cover-density ranking, normalized into 0-1 with flag 32. (v15+)
SELECT id, title, ts_rank_cd(search, q, 32) AS rank
FROM paper, websearch_to_tsquery('english', $1) AS q
WHERE search @@ q
ORDER BY rank DESC
LIMIT 20;
```

## 5. Highlighted snippets with ts_headline

```sql
-- Filter and LIMIT FIRST, then headline only the 20 surviving rows. (v15+)
SELECT id, title,
       ts_headline('english', body, q,
         'StartSel=<mark>, StopSel=</mark>, MaxFragments=2, '
         'MaxWords=25, MinWords=10') AS snippet
FROM (
  SELECT id, title, body
  FROM doc, websearch_to_tsquery('english', $1) AS q
  WHERE search @@ q
  ORDER BY ts_rank(search, q) DESC
  LIMIT 20
) top, websearch_to_tsquery('english', $1) AS q;
-- ts_headline output is NOT XSS-safe : escape it before rendering in HTML.
```

## 6. Phrase and proximity search

```sql
-- Adjacent, in order : the FOLLOWED BY operator. (v15+)
SELECT to_tsvector('english', 'fatal error in module') @@
       to_tsquery('english', 'fatal <-> error');          -- true

SELECT to_tsvector('english', 'error was not fatal') @@
       to_tsquery('english', 'fatal <-> error');          -- false

-- Up to N-1 lexemes between, in order. (v15+)
SELECT to_tsvector('english', 'fatal and unrecoverable error') @@
       to_tsquery('english', 'fatal <3> error');          -- true

-- websearch_to_tsquery turns a quoted phrase into <-> automatically. (v15+)
SELECT websearch_to_tsquery('english', '"fatal error"');
-- 'fatal' <-> 'error'
```

## 7. Prefix matching (autocomplete-style)

```sql
-- :* matches any lexeme starting with the prefix. (v15+)
SELECT to_tsvector('english', 'postgresql database') @@
       to_tsquery('english', 'postgr:*');                 -- true

-- websearch_to_tsquery does NOT support :* ; build prefix queries with
-- to_tsquery from sanitized input, or append :* in code. (v15+)
SELECT to_tsquery('english', regexp_replace($1, '\s+', ':* & ', 'g') || ':*');
```

## 8. Multi-language : per-row config column

```sql
-- A regconfig column lets each row use its own language. (v15+)
CREATE TABLE post (
  id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  lang   regconfig NOT NULL DEFAULT 'english',
  body   text,
  search tsvector GENERATED ALWAYS AS (to_tsvector(lang, coalesce(body,''))) STORED
);
CREATE INDEX post_search_idx ON post USING gin (search);

-- Query with the matching config for the language being searched. (v15+)
SELECT id FROM post WHERE search @@ websearch_to_tsquery('english', $1);
```

## 9. GiST instead of GIN (small, write-heavy table)

```sql
-- GiST : smaller, cheaper updates, but lossy. Default to GIN unless update
-- cost is a measured problem. (v15+)
CREATE INDEX tag_search_gist ON tag USING gist (search);

-- Longer signature reduces false matches at the cost of size. (v15+)
CREATE INDEX tag_search_gist ON tag
  USING gist (search tsvector_ops(siglen = 256));
```

## 10. Confirm the index is used

```sql
-- Expect a Bitmap Index Scan on the GIN index, never a Seq Scan. (v15+)
EXPLAIN (ANALYZE, BUFFERS)
SELECT id FROM doc
WHERE search @@ websearch_to_tsquery('english', 'postgres');
-- Good  : Bitmap Index Scan on doc_search_idx
-- Bad   : Seq Scan on doc  -> index missing or config mismatch
```

## 11. When to reach for pg_trgm instead

```sql
-- Typo-tolerant name lookup : FTS cannot do this, pg_trgm can. (v15+)
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX customer_name_trgm ON customer USING gin (name gin_trgm_ops);

SELECT name, similarity(name, $1) AS sml
FROM customer
WHERE name % $1                       -- trigram similarity above threshold
ORDER BY sml DESC
LIMIT 10;
-- $1 = 'jonh smiht' still finds 'John Smith'.
```

## Sources

- https://www.postgresql.org/docs/17/textsearch-intro.html
- https://www.postgresql.org/docs/17/textsearch-tables.html
- https://www.postgresql.org/docs/17/textsearch-controls.html
- https://www.postgresql.org/docs/17/textsearch-indexes.html
- https://www.postgresql.org/docs/17/pgtrgm.html
