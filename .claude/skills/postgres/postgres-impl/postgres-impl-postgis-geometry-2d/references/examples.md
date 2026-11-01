# Examples : PostGIS 2D Geometry and Geography

Working, version-annotated samples. Verified against the PostGIS 3.4 manual.
Baseline is PostGIS 3.3+ ; samples needing 3.4 are marked `v3.4+`.

## Setup Examples

### Install and create a geometry table

```sql
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE place (
    id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL,
    geom geometry(Point, 4326) NOT NULL
);

CREATE INDEX place_geom_gist ON place USING GIST (geom);
```

### Create a geography table for global data

```sql
CREATE TABLE city (
    id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL,
    loc  geography(Point, 4326) NOT NULL
);
CREATE INDEX city_loc_gist ON city USING GIST (loc);
```

### Build the index without locking a live table

```sql
CREATE INDEX CONCURRENTLY place_geom_gist ON place USING GIST (geom);
ANALYZE place;
```

## Insert Examples

### Constructors with explicit SRID

```sql
-- ST_MakePoint returns SRID 0 ; ST_SetSRID labels it
INSERT INTO place (name, geom)
VALUES ('Dam Square', ST_SetSRID(ST_MakePoint(4.8932, 52.3731), 4326));

-- ST_GeomFromText takes the SRID as the second argument
INSERT INTO place (name, geom)
VALUES ('Centraal Station', ST_GeomFromText('POINT(4.9003 52.3791)', 4326));

-- EWKT literal carries the SRID inline
INSERT INTO place (name, geom)
VALUES ('RAI', 'SRID=4326;POINT(4.8896 52.3411)');

-- Geography accepts an EWKT geography literal
INSERT INTO city (name, loc)
VALUES ('Amsterdam', 'SRID=4326;POINT(4.9041 52.3676)'::geography);
```

### Line and polygon from WKT

```sql
CREATE TABLE road (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                   geom geometry(LineString, 4326));
INSERT INTO road (geom)
VALUES (ST_GeomFromText('LINESTRING(4.89 52.37, 4.90 52.38, 4.91 52.39)', 4326));

CREATE TABLE district (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                       name text, geom geometry(Polygon, 4326));
INSERT INTO district (name, geom)
VALUES ('Centrum',
        ST_GeomFromText('POLYGON((4.88 52.36,4.92 52.36,4.92 52.40,4.88 52.40,4.88 52.36))', 4326));
```

## Proximity Query Examples

### ST_DWithin : indexed proximity filter

```sql
-- geometry : distance is in SRID units (degrees for 4326)
SELECT id, name
FROM place
WHERE ST_DWithin(geom, ST_SetSRID(ST_MakePoint(4.90, 52.37), 4326), 0.05);

-- geography : distance argument is in METERS
SELECT id, name
FROM city
WHERE ST_DWithin(loc, 'SRID=4326;POINT(4.90 52.37)'::geography, 5000);
```

### Filter then rank : ST_DWithin + ST_Distance

```sql
SELECT id, name,
       ST_Distance(loc, 'SRID=4326;POINT(4.90 52.37)'::geography) AS meters
FROM city
WHERE ST_DWithin(loc, 'SRID=4326;POINT(4.90 52.37)'::geography, 5000)
ORDER BY meters
LIMIT 10;
```

### K-nearest-neighbour with the `<->` operator

```sql
-- The GiST index serves the <-> distance ordering directly
SELECT id, name
FROM place
ORDER BY geom <-> ST_SetSRID(ST_MakePoint(4.90, 52.37), 4326)
LIMIT 5;
```

### Verify the index is used

```sql
EXPLAIN (ANALYZE, COSTS OFF)
SELECT id FROM place
WHERE ST_DWithin(geom, ST_SetSRID(ST_MakePoint(4.90,52.37),4326), 0.05);
--  Index Scan using place_geom_gist on place
--    Index Cond: (geom && ...)
--    Filter: st_dwithin(geom, ...)
```

## Relationship Predicate Examples

### Points inside a polygon

```sql
SELECT p.id, p.name
FROM place p
JOIN district d ON ST_Within(p.geom, d.geom)
WHERE d.name = 'Centrum';
```

### Intersecting features

```sql
SELECT r.id
FROM road r
JOIN district d ON ST_Intersects(r.geom, d.geom)
WHERE d.name = 'Centrum';
```

### Contains is the inverse of Within

```sql
-- These two return the same pairs
SELECT 1 WHERE ST_Within(
  ST_GeomFromText('POINT(4.90 52.38)',4326),
  ST_GeomFromText('POLYGON((4.88 52.36,4.92 52.36,4.92 52.40,4.88 52.40,4.88 52.36))',4326));

SELECT 1 WHERE ST_Contains(
  ST_GeomFromText('POLYGON((4.88 52.36,4.92 52.36,4.92 52.40,4.88 52.40,4.88 52.36))',4326),
  ST_GeomFromText('POINT(4.90 52.38)',4326));
```

## Transformation Examples

### Reproject between SRIDs

```sql
-- 4326 lat/lon to Web Mercator (3857)
SELECT ST_AsText(ST_Transform(geom, 3857)) FROM place WHERE id = 1;

-- Align operands before a predicate when SRIDs differ
SELECT r.id
FROM road r, district d
WHERE ST_Intersects(r.geom, ST_Transform(d.geom, ST_SRID(r.geom)));
```

### ST_SetSRID vs ST_Transform

```sql
-- ST_SetSRID : relabel only, coordinates UNCHANGED (use to fix SRID 0)
SELECT ST_AsEWKT(ST_SetSRID(ST_MakePoint(4.9, 52.37), 4326));
-- SRID=4326;POINT(4.9 52.37)

-- ST_Transform : RECOMPUTE coordinates into a new system
SELECT ST_AsEWKT(ST_Transform(ST_SetSRID(ST_MakePoint(4.9,52.37),4326), 3857));
-- SRID=3857;POINT(545540... 6867892...)
```

## Measurement Examples

### Area and length on a projected SRID

```sql
-- Transform to a projected SRID so units are meters
SELECT name,
       ST_Area(ST_Transform(geom, 3857))      AS area_m2_approx,
       ST_Perimeter(ST_Transform(geom, 3857)) AS perimeter_m_approx
FROM district;

-- Or cast to geography for accurate meters without choosing a projection
SELECT name, ST_Area(geom::geography) AS area_m2 FROM district;
```

### Buffer in meters via geography

```sql
-- Everything within 500 meters of a road
SELECT p.id, p.name
FROM place p
JOIN road r ON ST_Intersects(p.geom::geography, ST_Buffer(r.geom::geography, 500));
```

## Clustering Examples

### Density-based clustering with ST_ClusterDBSCAN

```sql
SELECT id, name,
       ST_ClusterDBSCAN(geom, eps := 0.01, minpoints := 3) OVER () AS cluster_id
FROM place;
```

### K-means clustering with ST_ClusterKMeans

```sql
SELECT id, name,
       ST_ClusterKMeans(geom, 5) OVER () AS cluster_id
FROM place;
```

Both functions are window functions : the `OVER ()` clause is mandatory and
each row receives a cluster id (`NULL` for noise points in DBSCAN).
