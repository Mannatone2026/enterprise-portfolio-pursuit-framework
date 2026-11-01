# Examples : postgres-impl-postgis-3d-raster

Working, version-annotated code samples. All examples target PostgreSQL 15, 16,
and 17 with PostGIS 3.3+. PostGIS-version-specific notes are annotated. Verified
against the PostGIS 3.4 manual.

## 1. Install and verify the extensions

```sql
CREATE EXTENSION IF NOT EXISTS postgis;            -- base
CREATE EXTENSION IF NOT EXISTS postgis_sfcgal;     -- 3D solids
CREATE EXTENSION IF NOT EXISTS postgis_raster;     -- raster
CREATE EXTENSION IF NOT EXISTS postgis_topology;   -- topology

-- See what is installed and at which version :
SELECT extname, extversion
FROM pg_extension
WHERE extname LIKE 'postgis%'
ORDER BY extname;

-- Full PostGIS build info, including whether SFCGAL is linked :
SELECT postgis_full_version();
```

## 2. Core 3D predicates : no postgis_sfcgal needed

```sql
-- 3D distance between two points. (PostGIS 3.x core, GEOS backend)
SELECT ST_3DDistance('POINT Z(0 0 0)'::geometry,
                      'POINT Z(0 0 10)'::geometry);   -- 10

-- 3D intersection test. (PostGIS 3.x core)
SELECT ST_3DIntersects('LINESTRING Z(0 0 0, 0 0 5)'::geometry,
                       'POINT Z(0 0 3)'::geometry);    -- true

-- 3D within-distance, index-aware. (PostGIS 3.x core)
SELECT a.id, b.id
FROM sensor a
JOIN sensor b ON a.id < b.id
WHERE ST_3DDWithin(a.geom, b.geom, 50);

-- Closest 3D point on a line to a point. (PostGIS 3.x core)
SELECT ST_AsText(
  ST_3DClosestPoint('LINESTRING Z(0 0 0, 10 0 0)'::geometry,
                    'POINT Z(4 4 0)'::geometry));      -- POINT Z(4 0 0)
```

## 3. Z-coordinate handling

```sql
-- Read the Z value. (core)
SELECT ST_Z('POINT Z(2 3 7)'::geometry);               -- 7

-- Promote a 2D geometry to 3D with a default Z. (core)
SELECT ST_AsText(ST_Force3D('POINT(1 2)'::geometry, 0)); -- POINT Z(1 2 0)

-- Drop the Z dimension. (core)
SELECT ST_AsText(ST_Force2D('POINT Z(1 2 9)'::geometry)); -- POINT(1 2)
```

## 4. Extrude a 2D footprint into a 3D solid

```sql
-- Requires postgis_sfcgal. (PostGIS 3.3+)
-- Extrude a square footprint 10 metres upward.
SELECT ST_AsText(
  ST_Extrude('POLYGON((0 0, 4 0, 4 4, 0 4, 0 0))'::geometry, 0, 0, 10));
-- Returns a POLYHEDRALSURFACE (an open shell, not yet a solid).

-- ST_Volume on a surface is 0 ; on a solid it is non-zero.
SELECT ST_Volume(geom)               AS surface_vol,   -- 0
       ST_Volume(ST_MakeSolid(geom)) AS solid_vol      -- 64
FROM (
  SELECT ST_Extrude('POLYGON((0 0, 4 0, 4 4, 0 4, 0 0))'::geometry,
                    0, 0, 4) AS geom
) f;
```

## 5. Per-building volume from footprint and height

```sql
-- Requires postgis_sfcgal. (PostGIS 3.3+)
CREATE TABLE building (
  id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  footprint geometry(Polygon, 28992) NOT NULL,   -- a projected SRID
  height_m  numeric NOT NULL
);

SELECT id,
       ST_Volume(
         ST_MakeSolid(
           ST_Extrude(footprint, 0, 0, height_m))) AS volume_m3
FROM building;
```

## 6. Triangulate a surface into a TIN

```sql
-- Requires postgis_sfcgal. (PostGIS 3.3+)
SELECT ST_AsText(
  ST_Tesselate('POLYGON((0 0, 4 0, 4 4, 0 4, 0 0))'::geometry));
-- Returns a TIN (collection of triangles), 3D coordinates preserved.
```

## 7. Load a raster : in-db vs out-db

```bash
# Out-db, tiled, indexed, with constraints : large DEM coverage. (raster2pgsql)
raster2pgsql -s 4326 -t 256x256 -I -C -R /data/dem/*.tif public.dem \
  | psql -d gisdb

# In-db, tiled : small derived raster kept inside the database.
raster2pgsql -s 4326 -t 100x100 -I -C /data/heatmap.tif public.heat \
  | psql -d gisdb
```

```sql
-- Out-db pixel reads need these enabled in the session. (postgis_raster)
SET postgis.enable_outdb_rasters = true;
SET postgis.gdal_enabled_drivers TO 'ENABLE_ALL';
```

## 8. Build a raster from scratch

```sql
-- Empty 100x100 raster, then add one 32-bit-float band. (postgis_raster)
SELECT ST_AddBand(
         ST_MakeEmptyRaster(100, 100, 0.0, 0.0, 1.0, -1.0, 0, 0, 4326),
         '32BF'::text, 0, -9999)             AS rast;

-- Inspect a raster's bands.
SELECT ST_NumBands(rast) AS bands FROM dem LIMIT 1;
```

## 9. Sample and clip rasters with vector geometry

```sql
-- Elevation at a single sensor point. (postgis_raster)
SELECT s.id, ST_Value(d.rast, 1, s.geom) AS elevation_m
FROM sensor s
JOIN dem d ON ST_Intersects(d.rast, s.geom);

-- Mean elevation inside each parcel : clip the DEM, then summarise.
SELECT p.id,
       (ST_SummaryStats(ST_Clip(d.rast, p.geom))).mean AS avg_elev_m
FROM parcel p
JOIN dem d ON ST_Intersects(d.rast, p.geom)
GROUP BY p.id, d.rast, p.geom;

-- Raster-geometry intersection : geometry / pixel-value pairs.
SELECT (gv).val AS pixel_value, (gv).geom AS pixel_geom
FROM (
  SELECT ST_Intersection(d.rast, p.geom) AS gv
  FROM dem d JOIN parcel p ON ST_Intersects(d.rast, p.geom)
) s;
```

## 10. Set up a topology

```sql
-- Requires postgis_topology. (PostGIS 3.3+)
SELECT topology.CreateTopology('city_topo', 28992, 0.001);

-- Add a TopoGeometry layer to an existing table :
SELECT topology.AddTopoGeometryColumn(
  'city_topo', 'public', 'parcel', 'topo_geom', 'POLYGON');

-- Inspect registered topologies :
SELECT name, srid, precision FROM topology.topology;
```

## 11. Decide raster vs vector

```sql
-- WRONG : elevation as millions of discrete vector points.
--   CREATE TABLE elevation_pt (geom geometry(PointZ,4326), z numeric);
--   -> bloated, slow, no native grid operations.

-- RIGHT : elevation as a tiled raster, parcels as vector, joined on demand.
SELECT p.id, (ST_SummaryStats(ST_Clip(d.rast, p.geom))).mean
FROM parcel p JOIN dem d ON ST_Intersects(d.rast, p.geom)
GROUP BY p.id, d.rast, p.geom;
```

## Sources

- https://postgis.net/docs/manual-3.4/
- https://postgis.net/docs/manual-3.4/ST_3DDistance.html
- https://postgis.net/docs/manual-3.4/ST_Extrude.html
- https://postgis.net/docs/manual-3.4/ST_Volume.html
- https://postgis.net/docs/manual-3.4/ST_Tesselate.html
- https://postgis.net/docs/manual-3.4/RT_reference.html
- https://postgis.net/docs/manual-3.4/using_raster_dataman.html
- https://postgis.net/docs/manual-3.4/Topology.html
