---
name: postgres-syntax-full-text-search
description: >
  Use when building search over text columns, ranking results by relevance, or choosing between full-text search and trigram fuzzy matching.
  Prevents to_tsquery syntax errors on raw user input (use websearch_to_tsquery), seqscan from missing GIN index, and config mismatch between index expression and query.
  Covers tsvector / tsquery, to_tsvector, plainto_tsquery / phraseto_tsquery / websearch_to_tsquery, @@ match, ts_rank / ts_rank_cd, ts_headline, GIN vs GiST index choice, generated tsvector column (v12+), setweight, FTS vs pg_trgm decision.
  Keywords: full text search, tsvector, tsquery, to_tsvector, websearch_to_tsquery, ts_rank, GIN, ts_headline, setweight, text search, search is slow, how to do search in postgres, fuzzy search, relevance ranking, search returns nothing
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-syntax-full-text-search

## Quick Reference :

PostgreSQL full-text search (FTS) matches a `tsvector` (a normalized document : sorted lexemes plus positions) against a `tsquery` (a normalized query : lexemes joined by boolean operators) via the `@@` match operator. The pipeline is always : raw text -> `to_tsvector(config, text)` -> `tsvector` ; raw query -> a query constructor -> `tsquery` ; then `tsvector @@ tsquery`. Normalization (stemming, stop-word removal, lowercasing) happens on BOTH sides, which is why FTS finds `friendly` when you search `friend` and `LIKE` cannot.

Four query constructors exist and the choice is the single highest-leverage decision. `to_tsquery` parses operator syntax (`&` `|` `!` `<->`) and RAISES a syntax error on malformed input : NEVER feed it raw end-user text. `plainto_tsquery` ANDs every word. `phraseto_tsquery` requires the words in order (`<->`). `websearch_to_tsquery` accepts Google-style syntax (quotes, `or`, leading `-`) and NEVER raises an error : it is the ONLY safe constructor for untrusted user input.

Performance comes from a GIN index. `to_tsvector` on a column is computed per row at query time unless indexed : without a GIN index every search is a seqscan. ALWAYS index either an expression `to_tsvector('english', body)` (must use the 2-argument form) or a generated `tsvector` column (v12+, the preferred modern pattern). The query MUST repeat the exact same config used in the index expression or the index is skipped. Rank with `ts_rank` / `ts_rank_cd`, snippet with `ts_headline`, prioritize fields with `setweight`. For typo-tolerant or substring matching, FTS is the wrong tool : use `pg_trgm` instead.

## When To Use This Skill :

ALWAYS use this skill when :
- Building keyword search over one or more text columns
- Ranking search results by relevance to a query
- Choosing a query constructor (`to_tsquery` vs `plainto_tsquery` vs `phraseto_tsquery` vs `websearch_to_tsquery`)
- Deciding between a GIN and a GiST index for text search
- Deciding between an expression index, a generated `tsvector` column, or a trigger-maintained column
- Generating highlighted snippets of matching text (`ts_headline`)
- Weighting title vs body for ranking (`setweight`)
- Deciding between full-text search and trigram fuzzy matching

NEVER use this skill for :
- Typo tolerance, substring match, or `LIKE`/`ILIKE` acceleration : see `pg_trgm` and `postgres-core-indexing-strategy`
- General GIN indexing of JSONB or arrays : see `postgres-syntax-jsonb`, `postgres-core-indexing-strategy`
- Reading `EXPLAIN` output to confirm index usage : see `postgres-impl-explain-analyze`
- Semantic / vector similarity search : that is `pgvector`, a different extension

## Decision Trees :

### Which query constructor? :

```
Does the query string come from an end user (search box, API param)?
├── Yes : websearch_to_tsquery(config, input)
│         NEVER raises an error on bad input. Supports "phrase",
│         the word "or", and leading "-" for NOT. Safe by construction.
└── No (you build the query string in code) :
    ├── Need raw operator control (&, |, !, <->, prefix :*) :
    │       to_tsquery(config, 'fat & (cat | rat) & !dog')
    │       RAISES syntax error on malformed input. Build it yourself.
    ├── Plain words, all must appear, order irrelevant :
    │       plainto_tsquery(config, 'fat cat rat')   -> 'fat' & 'cat' & 'rat'
    └── Plain words that must appear as an adjacent phrase :
            phraseto_tsquery(config, 'fat cat')      -> 'fat' <-> 'cat'
```

### GIN or GiST index for FTS? :

```
Is this the default / general case?
├── Yes : GIN.
│         CREATE INDEX ON docs USING gin (to_tsvector('english', body));
│         Lossless (no false matches), fastest lookup. The preferred type.
│         Tradeoff : larger on disk, slower to update than GiST.
└── Special case : small table with very frequent row updates,
    or you also index tsquery values :
        GiST. Smaller, cheaper updates, BUT lossy : produces false
        matches that PostgreSQL must recheck against the heap row.
        Default to GIN unless update cost is a measured problem.
```

### How to materialize the tsvector for indexing? :

```
PostgreSQL version >= 12 (always true for v15/16/17)?
├── Yes : generated tsvector column (the preferred modern pattern).
│         ALTER TABLE docs ADD COLUMN search tsvector
│           GENERATED ALWAYS AS
│           (to_tsvector('english', coalesce(title,'') || ' ' ||
│                                    coalesce(body,''))) STORED;
│         CREATE INDEX ON docs USING gin (search);
│         Query : WHERE search @@ websearch_to_tsquery('english', $1)
│         No config repeated in queries. Auto-maintained by Postgres.
└── Lighter setup, no schema change :
        Expression index on the 2-arg to_tsvector :
        CREATE INDEX ON docs USING gin (to_tsvector('english', body));
        Query MUST repeat 'english' verbatim or the index is skipped.
        Trigger-maintained tsvector columns are the LEGACY pattern :
        only keep one if it predates v12. Do not build new ones.
```

### Full-text search or pg_trgm? :

```
What kind of matching is needed?
├── Whole words / linguistic stems ("run" finds "running"),
│   boolean queries, relevance ranking :
│       Full-text search (this skill). tsvector @@ tsquery + GIN.
└── Typos, misspellings, substrings, accelerating LIKE '%x%' :
        pg_trgm. similarity() / % operator / GIN gin_trgm_ops.
        Character-level, language-agnostic. See references/methods.md.
```

## Patterns :

### Pattern 1 : Use websearch_to_tsquery for all user input

ALWAYS use `websearch_to_tsquery` when the query string originates from an end user.
NEVER pass raw user input to `to_tsquery` : a stray `&`, `:`, `!`, or unbalanced paren raises SQLSTATE `42601` (syntax_error) and the request fails.

```sql
-- SAFE : user types anything, never errors.
SELECT id, title
FROM docs
WHERE search @@ websearch_to_tsquery('english', $1);
-- $1 = 'fat cat'              -> 'fat' & 'cat'
-- $1 = '"fat cat" -dog'       -> 'fat' <-> 'cat' & !'dog'
-- $1 = 'cat or rat'           -> 'cat' | 'rat'
-- $1 = 'C++ & ::: !!!'        -> harmless tsquery, NO error raised

-- UNSAFE : the same input crashes to_tsquery.
SELECT id FROM docs
WHERE search @@ to_tsquery('english', $1);   -- $1 = 'C++ & :::' -> ERROR 42601
```

WHY : `to_tsquery` is a strict parser of the `tsquery` operator grammar ; any token it cannot parse aborts the statement. `websearch_to_tsquery` is a lenient parser designed for search boxes : it interprets a small Google-like syntax and silently drops anything it does not understand. Source : postgresql.org/docs/17/textsearch-controls.html (Parsing Queries).

### Pattern 2 : GIN index on a 2-argument to_tsvector expression

ALWAYS pass an explicit config (the 2-argument form `to_tsvector('english', body)`) when indexing an expression.
NEVER index the 1-argument `to_tsvector(body)` : it depends on the session GUC `default_text_search_config`, so the index is not immutable and PostgreSQL rejects it or it silently mismatches.

```sql
CREATE INDEX docs_body_fts ON docs
  USING gin (to_tsvector('english', body));

-- Uses the index : config matches the index expression EXACTLY.
SELECT id FROM docs
WHERE to_tsvector('english', body) @@ websearch_to_tsquery('english', $1);

-- Does NOT use the index : 1-arg form is a different expression.
SELECT id FROM docs
WHERE to_tsvector(body) @@ websearch_to_tsquery('english', $1);   -- seqscan
```

WHY : an expression index stores the result of one exact expression. The planner only uses it for a `WHERE` clause containing the byte-identical expression. The 1-argument `to_tsvector` resolves its config at run time and is therefore not a stable expression. Source : postgresql.org/docs/17/textsearch-tables.html (Creating Indexes).

### Pattern 3 : Generated tsvector column (v12+, preferred)

ALWAYS prefer a `GENERATED ALWAYS AS ... STORED` `tsvector` column over a hand-written trigger for new schemas (v12+).
NEVER write a `BEFORE INSERT OR UPDATE` trigger to maintain a `tsvector` in new code : the generated column does the same job, declaratively, with no trigger to forget.

```sql
ALTER TABLE docs ADD COLUMN search tsvector
  GENERATED ALWAYS AS (
    to_tsvector('english',
      coalesce(title, '') || ' ' || coalesce(body, ''))
  ) STORED;

CREATE INDEX docs_search_idx ON docs USING gin (search);

-- Query never repeats the config : it lives in the column definition.
SELECT id, title
FROM docs
WHERE search @@ websearch_to_tsquery('english', $1);
```

WHY : the generated column is recomputed by PostgreSQL on every INSERT/UPDATE of its source columns, so it can never drift out of sync. Queries reference the column directly, so no config string is repeated and no expression must match. Wrapping each source column in `coalesce(col, '')` is mandatory : `NULL` anywhere in the concatenation makes the whole `tsvector` `NULL`. Source : postgresql.org/docs/17/textsearch-tables.html (Creating Indexes).

### Pattern 4 : setweight for field priority, then ts_rank

ALWAYS apply `setweight` per source field (A = most important) when title and body should not rank equally.
NEVER expect `ts_rank` to know a title hit matters more than a body hit on its own : weight labels are the only signal it has.

```sql
ALTER TABLE docs ADD COLUMN search tsvector
  GENERATED ALWAYS AS (
    setweight(to_tsvector('english', coalesce(title, '')),    'A') ||
    setweight(to_tsvector('english', coalesce(abstract, '')), 'B') ||
    setweight(to_tsvector('english', coalesce(body, '')),     'D')
  ) STORED;

CREATE INDEX docs_search_idx ON docs USING gin (search);

-- ts_rank reads the {D, C, B, A} weight array (default {0.1,0.2,0.4,1.0}).
SELECT id, title, ts_rank(search, q) AS rank
FROM docs, websearch_to_tsquery('english', $1) AS q
WHERE search @@ q
ORDER BY rank DESC
LIMIT 20;
```

WHY : `setweight` labels every lexeme with a class A-D. `ts_rank` multiplies each matching lexeme by the class weight from its array (ordered `{D, C, B, A}`, default `{0.1, 0.2, 0.4, 1.0}`). Use `ts_rank_cd` (cover density) instead when proximity of terms matters ; it needs positional info, so never strip positions from the stored `tsvector`. Source : postgresql.org/docs/17/textsearch-controls.html (Ranking).

### Pattern 5 : ts_headline for highlighted snippets

ALWAYS run `ts_headline` only on the already-filtered, already-limited result set, never inside the `WHERE` clause.
NEVER call `ts_headline` on every row of a large table : it reparses the document text and is not index-accelerated.

```sql
SELECT id, title,
       ts_headline('english', body, q,
         'StartSel=<mark>, StopSel=</mark>, MaxFragments=2, MaxWords=25')
FROM (
  SELECT id, title, body
  FROM docs, websearch_to_tsquery('english', $1) AS q
  WHERE search @@ q
  ORDER BY ts_rank(search, q) DESC
  LIMIT 20
) top, websearch_to_tsquery('english', $1) AS q;
```

WHY : `ts_headline` extracts and marks up an excerpt around the matched lexemes. It works on the raw document text, not the indexed `tsvector`, so its cost is proportional to document size times row count. Filtering and `LIMIT`-ing first keeps it to 20 calls. Its output is NOT XSS-safe : escape it before rendering in HTML. Source : postgresql.org/docs/17/textsearch-controls.html (Highlighting Results).

## Anti-Patterns :

(Short list ; full cause / symptom / fix in references/anti-patterns.md)

- `to_tsquery` on raw user input : SQLSTATE `42601` syntax_error : use `websearch_to_tsquery`
- FTS query with no GIN index : silent seqscan, slow at scale
- Config mismatch between index expression and query : index skipped, seqscan
- 1-argument `to_tsvector` in an index expression : not immutable, index unusable
- Missing `coalesce` around a nullable source column : whole `tsvector` becomes `NULL`
- `ts_headline` in `WHERE` or on unfiltered rows : reparses every document, no index help
- Using FTS for substring or typo matching : wrong tool, use `pg_trgm`
- Stripping positions from a stored `tsvector` then calling `ts_rank_cd` or phrase search : both need positions

## Reference Links :

- [references/methods.md](references/methods.md) : Full API surface, `tsvector` / `tsquery` types, all constructor and ranking signatures, parameter tables
- [references/examples.md](references/examples.md) : Working code samples, version-annotated, end-to-end search setup
- [references/anti-patterns.md](references/anti-patterns.md) : Anti-patterns with cause, symptom, SQLSTATE, and fix

## See Also :

- `postgres-syntax-jsonb` : the other major GIN use case ; same index family, different operators
- `postgres-core-indexing-strategy` : when GIN beats GiST/B-tree across all index types
- `postgres-impl-explain-analyze` : confirm a FTS query actually hits the GIN index
- Vooronderzoek section : §21 (PROPOSE-ADD postgres-syntax-full-text-search), §22 item 1
- Official docs : https://www.postgresql.org/docs/17/textsearch.html
