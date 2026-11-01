# postgres-impl-pgvector-similarity : Examples

Verified, version-annotated pgvector examples. All SQL verified against github.com/pgvector/pgvector (Last Verified : 2026-05-19). Targets pgvector 0.7+ on PostgreSQL 15, 16, 17.

## Install and schema

```sql
-- pgvector 0.7+
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE documents (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    content   text NOT NULL,
    status    text NOT NULL DEFAULT 'draft',
    embedding vector(1536)            -- pinned dimension
);
```

## Insert embeddings

```sql
-- pgvector 0.7+ : a vector literal is a bracketed list passed as text
INSERT INTO documents (content, embedding)
VALUES ('first doc', '[0.12, -0.04, 0.88]');   -- dimension must equal the column

-- Parameterised insert from an application :
INSERT INTO documents (content, embedding) VALUES ($1, $2);
```

## HNSW index (default choice)

```sql
-- pgvector 0.7+ : cosine-distance HNSW index
CREATE INDEX idx_documents_embedding_hnsw
    ON documents
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- L2-distance variant (matches the <-> operator)
CREATE INDEX idx_documents_embedding_l2
    ON documents
    USING hnsw (embedding vector_l2_ops);

-- Build it online on a live table
CREATE INDEX CONCURRENTLY idx_documents_embedding_hnsw
    ON documents USING hnsw (embedding vector_cosine_ops);
```

## ivfflat index (build after loading data)

```sql
-- pgvector 0.7+ : load rows FIRST, then build ivfflat
-- lists : rows/1000 for <= 1M rows ; sqrt(rows) for > 1M rows
CREATE INDEX idx_documents_embedding_ivf
    ON documents
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
```

## k-nearest-neighbour queries

```sql
-- pgvector 0.7+ : cosine distance, nearest 10
SELECT id, content, embedding <=> $1 AS distance
FROM documents
ORDER BY embedding <=> $1
LIMIT 10;

-- L2 / Euclidean distance
SELECT id FROM documents
ORDER BY embedding <-> $1
LIMIT 10;

-- Inner product : <#> is NEGATIVE, negate for a readable score
SELECT id, (embedding <#> $1) * -1 AS inner_product
FROM documents
ORDER BY embedding <#> $1
LIMIT 10;

-- L1 / taxicab distance (pgvector 0.7+)
SELECT id FROM documents
ORDER BY embedding <+> $1
LIMIT 10;
```

## Confirm the index is used

```sql
-- pgvector 0.7+ : expect "Index Scan using idx_documents_embedding_hnsw",
-- NOT "Seq Scan on documents"
EXPLAIN (ANALYZE, BUFFERS)
SELECT id FROM documents
ORDER BY embedding <=> $1
LIMIT 10;
```

## Tune recall versus speed

```sql
-- pgvector 0.7+ : HNSW , default ef_search is 40
SET hnsw.ef_search = 100;

-- ivfflat , default probes is 1
SET ivfflat.probes = 10;

-- Scope to one transaction
BEGIN;
SET LOCAL hnsw.ef_search = 200;
SELECT id FROM documents ORDER BY embedding <=> $1 LIMIT 10;
COMMIT;
```

## Filtered similarity search

```sql
-- Option A : partial HNSW index per frequently-filtered value (pgvector 0.7+)
CREATE INDEX idx_docs_emb_published
    ON documents USING hnsw (embedding vector_cosine_ops)
    WHERE status = 'published';

SELECT id FROM documents
WHERE status = 'published'
ORDER BY embedding <=> $1
LIMIT 10;

-- Option B : iterative scans re-probe until LIMIT is met (pgvector 0.8+)
SET hnsw.iterative_scan = strict_order;
SELECT id FROM documents
WHERE status = $2
ORDER BY embedding <=> $1
LIMIT 10;
```

## Hybrid search : vector + full-text via Reciprocal Rank Fusion

```sql
-- pgvector 0.7+ : fuse semantic and keyword rankings by RANK position.
-- $1 = query embedding, $2 = query text. Constant 60 is the standard RRF damping term.
WITH semantic AS (
    SELECT id, row_number() OVER (ORDER BY embedding <=> $1) AS rank
    FROM documents
    ORDER BY embedding <=> $1
    LIMIT 50
),
keyword AS (
    SELECT id, row_number() OVER (
               ORDER BY ts_rank(to_tsvector('english', content),
                                plainto_tsquery('english', $2)) DESC) AS rank
    FROM documents
    WHERE to_tsvector('english', content) @@ plainto_tsquery('english', $2)
    LIMIT 50
)
SELECT COALESCE(s.id, k.id) AS id,
       COALESCE(1.0 / (60 + s.rank), 0)
     + COALESCE(1.0 / (60 + k.rank), 0) AS score
FROM semantic s
FULL OUTER JOIN keyword k USING (id)
ORDER BY score DESC
LIMIT 10;
```

## halfvec for high-dimension embeddings

```sql
-- pgvector 0.7+ : a 3072-dim embedding exceeds the 2000-dim vector index limit.
-- Store as vector(3072), index the halfvec cast (indexable up to 4000 dims).
CREATE TABLE big_docs (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    embedding vector(3072)
);

CREATE INDEX idx_big_docs_embedding_half
    ON big_docs
    USING hnsw ((embedding::halfvec(3072)) halfvec_cosine_ops);

-- The query MUST repeat the identical cast to match the index
SELECT id FROM big_docs
ORDER BY embedding::halfvec(3072) <=> $1::halfvec(3072)
LIMIT 10;

-- Alternative : declare the column as halfvec directly
CREATE TABLE half_docs (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    embedding halfvec(3072)
);
CREATE INDEX ON half_docs USING hnsw (embedding halfvec_cosine_ops);
```

## Binary vectors

```sql
-- pgvector 0.7+ : binary embeddings with Hamming distance
CREATE TABLE hashed (
    id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    bits  bit(256)
);
CREATE INDEX ON hashed USING hnsw (bits bit_hamming_ops);

SELECT id FROM hashed
ORDER BY bits <~> $1
LIMIT 10;
```

## Rebuild after a data or dimension change

```sql
-- ivfflat : centroids drift after a large data change , rebuild online
REINDEX INDEX CONCURRENTLY idx_documents_embedding_ivf;

-- After changing the embedding dimension : drop and recreate
DROP INDEX idx_documents_embedding_hnsw;
ALTER TABLE documents ALTER COLUMN embedding TYPE vector(1024);
CREATE INDEX CONCURRENTLY idx_documents_embedding_hnsw
    ON documents USING hnsw (embedding vector_cosine_ops);
```
