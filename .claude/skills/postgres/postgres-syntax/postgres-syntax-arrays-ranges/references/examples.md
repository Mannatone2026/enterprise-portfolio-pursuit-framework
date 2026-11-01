# Examples : Arrays, Ranges, Multiranges

Working, version-annotated samples. Verified against PostgreSQL 17 docs.
Unless a sample is marked `v16+`, it runs on PostgreSQL 15, 16, and 17.

## Array Examples

### Declare and populate an array column

```sql
CREATE TABLE sal_emp (
    name           text,
    pay_by_quarter integer[],
    schedule       text[][]
);

-- String-literal form
INSERT INTO sal_emp VALUES (
    'Bill',
    '{10000, 10000, 10000, 10000}',
    '{{"meeting", "lunch"}, {"training", "presentation"}}'
);

-- ARRAY constructor form
INSERT INTO sal_emp VALUES (
    'Carol',
    ARRAY[20000, 25000, 25000, 25000],
    ARRAY[['breakfast', 'consulting'], ['meeting', 'lunch']]
);
```

### Access elements and slices (1-based)

```sql
SELECT pay_by_quarter[3] FROM sal_emp WHERE name = 'Bill';   -- 10000

SELECT schedule[1:2][1:1] FROM sal_emp WHERE name = 'Bill';
-- {{meeting},{training}}

-- Omitted bounds default to the array limits
SELECT schedule[:2][2:] FROM sal_emp WHERE name = 'Bill';
```

### Modify arrays

```sql
-- Replace one element
UPDATE sal_emp SET pay_by_quarter[4] = 15000 WHERE name = 'Bill';

-- Replace a slice
UPDATE sal_emp SET pay_by_quarter[1:2] = '{27000,27000}' WHERE name = 'Carol';

-- Concatenate and use construction functions
SELECT ARRAY[1,2] || ARRAY[3,4];          -- {1,2,3,4}
SELECT array_append(ARRAY[1,2], 3);       -- {1,2,3}
SELECT array_prepend(0, ARRAY[1,2]);      -- {0,1,2}
SELECT array_remove(ARRAY[1,2,3,2], 2);   -- {1,3}
```

### Search arrays

```sql
-- ANY / ALL : scalar quantified comparison
SELECT * FROM sal_emp WHERE 10000 = ANY (pay_by_quarter);
SELECT * FROM sal_emp WHERE 10000 = ALL (pay_by_quarter);

-- Containment and overlap (GIN-indexable)
SELECT * FROM sal_emp WHERE pay_by_quarter @> ARRAY[10000];
SELECT * FROM sal_emp WHERE pay_by_quarter && ARRAY[25000, 99999];

-- Find a position
SELECT array_position(ARRAY['sun','mon','tue','wed'], 'tue');   -- 3
SELECT array_positions(ARRAY[1,4,3,1,3], 1);                    -- {1,4}
```

### GIN index for array containment

```sql
CREATE TABLE article (
    id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tags text[] NOT NULL DEFAULT '{}'
);
CREATE INDEX article_tags_gin ON article USING GIN (tags);

-- Uses the GIN index
EXPLAIN (COSTS OFF)
SELECT id FROM article WHERE tags @> ARRAY['postgres'];
--  Bitmap Heap Scan on article
--    Recheck Cond: (tags @> '{postgres}'::text[])
--    ->  Bitmap Index Scan on article_tags_gin
```

### unnest and unnest WITH ORDINALITY

```sql
-- Plain expansion
SELECT unnest(ARRAY['a','b','c']);   -- 3 rows : a, b, c

-- With ordinal position, in a FROM clause
SELECT a.id, t.tag, t.pos
FROM article a,
     LATERAL unnest(a.tags) WITH ORDINALITY AS t(tag, pos);

-- generate_subscripts as an alternative when you need the index only
SELECT s, pay_by_quarter[s]
FROM sal_emp, generate_subscripts(pay_by_quarter, 1) AS s
WHERE name = 'Bill';
```

### array_agg : rows back into an array

```sql
SELECT array_agg(tag ORDER BY tag) AS all_tags
FROM (VALUES ('sql'), ('index'), ('postgres')) v(tag);
-- {index,postgres,sql}
```

### Random sampling (v16+)

```sql
-- v16+ : array_sample picks n random elements
SELECT array_sample(ARRAY[1,2,3,4,5,6], 3);   -- e.g. {2,6,1}

-- v16+ : array_shuffle reorders the first dimension
SELECT array_shuffle(ARRAY[1,2,3,4]);          -- e.g. {3,1,4,2}
```

## Range Examples

### Range literals and bounds

```sql
SELECT '[3,7)'::int4range;          -- contains 3,4,5,6
SELECT '[4,4]'::int4range;          -- contains only 4
SELECT '[4,4)'::int4range;          -- normalises to 'empty'
SELECT '(,5]'::int4range;           -- unbounded below
SELECT '[2026-01-01,2026-04-01)'::daterange;
```

### Range constructors

```sql
SELECT int4range(10, 20);             -- [10,20)  two-arg form is always [)
SELECT numrange(1.0, 14.0, '(]');     -- (1.0,14.0]
SELECT tstzrange('2026-05-20 09:00', NULL);   -- unbounded upper
```

### Range operators

```sql
SELECT int4range(10, 20) @> 15;                       -- t (contains element)
SELECT int4range(10, 20) @> int4range(12, 18);        -- t (contains range)
SELECT int8range(3, 7) && int8range(5, 12);           -- t (overlaps)
SELECT numrange(1.1, 2.2) -|- numrange(2.2, 3.3);     -- t (adjacent)
SELECT int8range(1, 10) << int8range(100, 110);       -- t (strictly left)
SELECT numrange(5, 15) + numrange(10, 20);            -- [5,20)  union
SELECT int8range(5, 15) * int8range(10, 20);          -- [10,15) intersection
SELECT int8range(5, 15) - int8range(10, 20);          -- [5,10)  difference
```

### Range functions

```sql
SELECT lower(numrange(1.1, 2.2));        -- 1.1
SELECT upper(daterange('2026-01-01','2026-04-01'));   -- 2026-04-01
SELECT isempty('[4,4)'::int4range);      -- t
SELECT lower_inc('[3,7)'::int4range);    -- t
SELECT upper_inc('[3,7)'::int4range);    -- f
SELECT upper_inf('[5,)'::int4range);     -- t
```

### GiST index for range overlap

```sql
CREATE TABLE price_band (
    sku   text NOT NULL,
    valid daterange NOT NULL
);
CREATE INDEX price_band_valid_gist ON price_band USING GIST (valid);

EXPLAIN (COSTS OFF)
SELECT * FROM price_band
WHERE valid && daterange('2026-03-15','2026-05-15');
--  Bitmap Heap Scan on price_band
--    ->  Bitmap Index Scan on price_band_valid_gist
```

## Exclusion Constraint Examples

### Forbid any two overlapping ranges

```sql
CREATE TABLE reservation (
    during tsrange NOT NULL,
    EXCLUDE USING gist (during WITH &&)
);

INSERT INTO reservation VALUES ('[2026-05-20 11:30, 2026-05-20 15:00)');

-- Overlapping insert is rejected (SQLSTATE 23P01)
INSERT INTO reservation VALUES ('[2026-05-20 14:45, 2026-05-20 15:45)');
-- ERROR:  conflicting key value violates exclusion constraint "reservation_during_excl"
```

### Forbid overlap only within the same room

```sql
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE room_reservation (
    id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    room   text    NOT NULL,
    during tsrange NOT NULL,
    EXCLUDE USING gist (room WITH =, during WITH &&)
);

INSERT INTO room_reservation (room, during)
VALUES ('123A', '[2026-05-20 14:00, 2026-05-20 15:00)');

-- Same room + overlap -> rejected
INSERT INTO room_reservation (room, during)
VALUES ('123A', '[2026-05-20 14:30, 2026-05-20 15:30)');
-- ERROR:  conflicting key value violates exclusion constraint

-- Different room -> accepted
INSERT INTO room_reservation (room, during)
VALUES ('123B', '[2026-05-20 14:30, 2026-05-20 15:30)');
```

## Multirange Examples

### Multirange literals and operators

```sql
SELECT '{}'::int4multirange;                     -- empty multirange
SELECT '{[3,7), [8,9)}'::int4multirange;
SELECT '{[3,7), [8,12)}'::int4multirange @> 9;   -- t
SELECT '{[3,7), [8,12)}'::int4multirange @> 7;   -- f
```

### range_agg : union of many ranges

```sql
WITH bookings(during) AS (
    VALUES ('[2026-05-20 09:00,2026-05-20 10:00)'::tstzrange),
           ('[2026-05-20 10:00,2026-05-20 11:30)'::tstzrange),
           ('[2026-05-20 14:00,2026-05-20 15:00)'::tstzrange)
)
SELECT range_agg(during) AS booked FROM bookings;
-- {[2026-05-20 09:00,2026-05-20 11:30), [2026-05-20 14:00,2026-05-20 15:00)}
-- adjacent ranges merge ; disjoint ranges stay separate
```

### range_intersect_agg : common overlap

```sql
WITH windows(r) AS (
    VALUES ('[1,10)'::int4range), ('[5,15)'::int4range), ('[8,20)'::int4range)
)
SELECT range_intersect_agg(r) FROM windows;
-- {[8,10)}
```

### Expand a multirange back to rows

```sql
SELECT unnest('{[1,5), [10,15)}'::int4multirange);
-- [1,5)
-- [10,15)
```
