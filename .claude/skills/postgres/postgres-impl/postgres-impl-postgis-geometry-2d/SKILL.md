---
name: postgres-impl-postgis-geometry-2d
description: >
  Use when querying spatial data, picking geometry vs geography, or speeding up "find points near X" queries with PostGIS.
  Prevents losing the GiST index by using ST_Distance in WHERE (use ST_DWithin), seqscan from missing spatial index, and wrong results from mixing SRIDs without ST_Transform.
  Covers CREATE EXTENSION postgis, geometry vs geography, SRID (4326 / 3857 / projected), GiST spatial index, spatial predicates (ST_Intersects / ST_Within / ST_DWithin / ST_Distance), ST_Transform, ST_Buffer, constructors, measurement functions.
  Keywords: PostGIS, geometry, geography, SRID, 4326, 3857, GiST, ST_Intersects, ST_DWithin, ST_Distance, ST_Transform, ST_Contains, spatial index, spatial query is slow, find points near, distance in meters, my postgis query does seqscan, wrong distance results
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17 with PostGIS 3.3+ (3.4 features marked v3.4+)."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-impl-postgis-geometry-2d

## Quick Reference :

PostGIS adds two spatial column types to PostgreSQL. **`geometry`** uses planar
(flat) math in the units of its coordinate system : fast, supports every
PostGIS function, but distances are meaningful only in a projected SRID.
**`geography`** uses spheroidal math on the WGS84 ellipsoid : slower, smaller
function set, but distances are always in meters and correct globally.

Three rules drive every spatial query :

1. ALWAYS create a `GIST` index on the spatial column. Without it, every
   spatial predicate falls back to a sequential scan.
2. ALWAYS use `ST_DWithin(a, b, dist)` for proximity filters, NEVER
   `ST_Distance(a, b) < dist`. Only `ST_DWithin` keeps the GiST index.
3. ALWAYS keep both operands in the same SRID. Mixed SRIDs raise an error or
   silently produce wrong answers. Reproject with `ST_Transform`.

```
Local / regional data, need accurate distances? -> geometry + projected SRID
Global lat/lon data, need true meters?            -> geography (SRID 4326)
"Find rows within N meters of point X"            -> ST_DWithin (indexed)
"How far apart are A and B exactly?"              -> ST_Distance (after filter)
Reproject between coordinate systems               -> ST_Transform(geom, srid)
```

Install once per database : `CREATE EXTENSION postgis;`.

## When To Use This Skill :

ALWAYS use this skill when :
- Storing points, lines, or polygons and querying them spatially
- Choosing between the `geometry` and `geography` column type
- A spatial query (`ST_Intersects`, `ST_DWithin`, `ST_Contains`) is slow
- Computing distance, area, length, or buffers
- Reprojecting coordinates between SRIDs (4326, 3857, national grids)

NEVER use this skill for :
- Range or interval data that is not spatial : use `postgres-syntax-arrays-ranges`
- 3D solid geometry and volumes : requires `postgis_sfcgal` and the `ST_3D*` family
- Raster / gridded data : requires `postgis_raster`
- Vector-similarity / embedding search : use `pgvector`, not PostGIS

## Decision Trees :

### geometry vs geography :

```
Is the data global scale (crosses regions, poles, antimeridian)?
├─ YES -> need true distances in meters?
│         ├─ YES -> geography (SRID 4326)
│         └─ NO  -> geometry (SRID 4326), predicates only
└─ NO (local / regional)
   └─ Need distance, area, or length in real units?
      ├─ YES -> geometry in a PROJECTED SRID (national grid / 3857)
      └─ NO  -> geometry (SRID 4326) is fine for predicates
```

### Proximity query : ST_DWithin vs ST_Distance :

```
"Rows within N units/meters of X"  -> ST_DWithin(col, X, N)   [GiST-indexed]
"Exact distance between two rows"  -> ST_Distance(a, b)        [no index alone]
"Nearest K rows to X"              -> ORDER BY col <-> X LIMIT K  [KNN GiST]

NEVER : WHERE ST_Distance(col, X) < N
        -> ST_Distance is not a bounding-box predicate, the GiST
           index is skipped, the planner does a Seq Scan.
```

### Which spatial index :

```
General 2D geometry/geography, any ST_* predicate -> GIST (default)
Point-only dataset, very large                    -> SP-GIST (k-d tree)
Data inserted in spatial order, huge table        -> BRIN (smallest, lossy)
3D / N-dimensional queries                         -> GIST (geom gist_geometry_ops_nd)
```

### SRID mismatch handling :

```
Two geometries, different SRID?
├─ Both are real coordinate systems -> ST_Transform one to match the other
├─ One has SRID 0 (undeclared)      -> ST_SetSRID to label it, do NOT Transform
└─ Same SRID                        -> compare directly
```

`ST_SetSRID` only relabels the SRID ; `ST_Transform` actually recomputes the
coordinates. Using the wrong one corrupts data.

## Patterns :

### Pattern 1 : Install PostGIS and declare a typmod spatial column

ALWAYS : declare the column with a typmod : `geometry(Point, 4326)`.
NEVER : use a bare `geometry` column. The typmod enforces type and SRID.

```sql
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE place (
    id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL,
    geom geometry(Point, 4326) NOT NULL
);
```

WHY : `geometry(Point, 4326)` rejects any insert that is not a point or not in
SRID 4326. A bare `geometry` column accepts mixed types and mixed SRIDs, which
silently breaks predicates and indexes later.

### Pattern 2 : GiST spatial index is mandatory

ALWAYS : create a `GIST` index on every spatial column you query.
NEVER : run `ST_Intersects` / `ST_DWithin` / `ST_Contains` on an unindexed
column at scale.

```sql
CREATE INDEX place_geom_gist ON place USING GIST (geom);

-- Use CONCURRENTLY on a live table to avoid a write lock
CREATE INDEX CONCURRENTLY place_geom_gist ON place USING GIST (geom);
```

WHY : every `ST_*` relationship predicate begins with a bounding-box test
(`&&`). The GiST index answers that test. Without the index the planner reads
every heap row : a `Seq Scan` in `EXPLAIN`. Run `ANALYZE place;` after a bulk
load so the planner has fresh statistics.

### Pattern 3 : ST_DWithin for proximity, never ST_Distance in WHERE

ALWAYS : filter proximity with `ST_DWithin(col, point, distance)`.
NEVER : write `WHERE ST_Distance(col, point) < distance`.

```sql
-- RIGHT : indexed, fast
SELECT id, name
FROM place
WHERE ST_DWithin(geom, ST_SetSRID(ST_MakePoint(4.90, 52.37), 4326), 0.05);

-- After ST_DWithin narrows the set, ST_Distance ranks the survivors
SELECT id, name,
       ST_Distance(geom, ST_SetSRID(ST_MakePoint(4.90, 52.37), 4326)) AS d
FROM place
WHERE ST_DWithin(geom, ST_SetSRID(ST_MakePoint(4.90, 52.37), 4326), 0.05)
ORDER BY d;
```

WHY : `ST_DWithin` includes a bounding-box comparison, so the GiST index
prunes candidates first. `ST_Distance` computes an exact value with no
bounding-box phase, so `ST_Distance(...) < x` forces a full scan. Filter with
`ST_DWithin`, then rank the small surviving set with `ST_Distance`.

### Pattern 4 : geography for global distance in meters

ALWAYS : use `geography` when distances must be in meters across wide areas.
NEVER : run `ST_Distance` on a `geometry(Point, 4326)` and expect meters : the
result is in degrees.

```sql
CREATE TABLE city (
    id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    loc geography(Point, 4326) NOT NULL
);
CREATE INDEX city_loc_gist ON city USING GIST (loc);

-- distance is in METERS automatically for geography
SELECT a.id, b.id, ST_Distance(a.loc, b.loc) AS meters
FROM city a, city b
WHERE a.id < b.id;

-- ST_DWithin on geography : the distance argument is meters
SELECT id FROM city
WHERE ST_DWithin(loc, 'SRID=4326;POINT(4.90 52.37)'::geography, 5000);
```

WHY : `geography` math runs on the WGS84 spheroid, so every distance and
length is in meters and is correct near the poles and antimeridian.
`geometry(Point, 4326)` distance is in degrees of lat/lon, which is not a
usable real-world unit. The tradeoff : `geography` is slower and supports fewer
functions, so for local data prefer `geometry` in a projected SRID.

### Pattern 5 : ST_Transform to align SRIDs and get planar units

ALWAYS : reproject with `ST_Transform` before measuring `geometry` distances.
NEVER : compare or measure two geometries that carry different SRIDs.

```sql
-- Reproject WGS84 lat/lon to Web Mercator (3857) for planar measurement
SELECT ST_Length(ST_Transform(geom, 3857)) AS length_m_approx
FROM road;

-- Align operands before a predicate
SELECT *
FROM road r, region g
WHERE ST_Intersects(r.geom, ST_Transform(g.geom, ST_SRID(r.geom)));
```

WHY : a spatial predicate on two different SRIDs raises
`ERROR: Operation on mixed SRID geometries`. `ST_Transform(geom, srid)`
recomputes the coordinates into the target system using the definitions in
`spatial_ref_sys`. The source geometry MUST already carry a valid SRID, or the
transform fails.

### Pattern 6 : Constructors and SRID assignment

ALWAYS : attach the SRID at construction with `ST_SetSRID` or an EWKT literal.
NEVER : leave a constructed geometry at SRID 0 and then query it against
SRID 4326 data.

```sql
-- ST_MakePoint produces SRID 0 ; label it explicitly
INSERT INTO place (name, geom)
VALUES ('Dam', ST_SetSRID(ST_MakePoint(4.8932, 52.3731), 4326));

-- ST_GeomFromText takes the SRID as its second argument
INSERT INTO place (name, geom)
VALUES ('Centraal', ST_GeomFromText('POINT(4.9003 52.3791)', 4326));

-- EWKT literal carries the SRID inline
INSERT INTO place (name, geom)
VALUES ('Rai', 'SRID=4326;POINT(4.8896 52.3411)');
```

WHY : `ST_MakePoint` and `ST_Point` return a geometry with SRID 0 (unknown).
Inserting that into a `geometry(Point, 4326)` column fails the typmod check.
Wrap it in `ST_SetSRID(..., 4326)`, or use `ST_GeomFromText(text, srid)` /
`ST_GeomFromGeoJSON` which set the SRID directly.

### Pattern 7 : Indexed relationship predicates and measurement

ALWAYS : use `ST_Intersects` as the default "do these touch" filter.
NEVER : assume `ST_Within` and `ST_Contains` are the same : they are inverses.

```sql
-- Points that fall inside a polygon (GiST-indexed)
SELECT p.id, p.name
FROM place p, district d
WHERE ST_Within(p.geom, d.geom) AND d.name = 'Centrum';

-- Buffer then intersect : everything within 500 m of a road
SELECT p.id
FROM place p, road r
WHERE ST_Intersects(p.geom, ST_Buffer(r.geom::geography, 500)::geometry);

-- Measurement on a projected SRID
SELECT ST_Area(ST_Transform(geom, 3857))  AS area_m2_approx,
       ST_Perimeter(ST_Transform(geom, 3857)) AS perim_m_approx
FROM district;
```

WHY : `ST_Intersects(a, b)` is true when the geometries share any point ;
`ST_Within(a, b)` means `a` is fully inside `b` ; `ST_Contains(a, b)` is the
inverse (`b` inside `a`). All three start with a bounding-box `&&` test, so a
GiST index accelerates them. `ST_Area` / `ST_Length` / `ST_Perimeter` on a
`geometry` return values in the SRID's units, so transform to a projected SRID
first for meters.

## Anti-Patterns :

(Short list, full detail in references/anti-patterns.md)

- `ST_Distance(col, x) < n` in `WHERE` : skips the GiST index, use `ST_DWithin`
- Missing GiST index on a spatial column : `Seq Scan` on every spatial query
- Mixing two SRIDs without `ST_Transform` : `ERROR: Operation on mixed SRID geometries`
- `geography` for small local datasets : slower than `geometry` + projected SRID
- `ST_Distance` on `geometry(Point, 4326)` expecting meters : result is in degrees
- `ST_MakePoint` left at SRID 0 inserted into a 4326 column : typmod check fails
- `ST_SetSRID` used to "fix" a wrong SRID : it relabels, it does NOT reproject

## Reference Links :

- [references/methods.md](references/methods.md) : Full function and operator tables
- [references/examples.md](references/examples.md) : Working version-annotated code samples
- [references/anti-patterns.md](references/anti-patterns.md) : Anti-patterns with cause, symptom, fix

## See Also :

- postgres-impl-indexing : GiST, SP-GiST, and BRIN index internals and tradeoffs
- postgres-syntax-arrays-ranges : non-spatial interval and list data
- postgres-core-extensions : managing `CREATE EXTENSION` and extension dependencies
- Vooronderzoek section : §10 (PostGIS)
- Official docs : https://postgis.net/docs/manual-3.4/using_postgis_dbmanagement.html
