# Methods : postgres-impl-postgis-3d-raster

Full API surface for PostGIS 3D, raster, and topology. Signatures verified against
the PostGIS 3.4 manual. All functions are available in PostGIS 3.3 and 3.4 running
on PostgreSQL 15, 16, or 17.

## Extensions

| Extension | Created with | Provides |
|-----------|--------------|----------|
| `postgis` | `CREATE EXTENSION postgis` | 2D + core 3D predicates, geometry/geography |
| `postgis_sfcgal` | `CREATE EXTENSION postgis_sfcgal` | solid construction, volume, extrude, tessellate |
| `postgis_raster` | `CREATE EXTENSION postgis_raster` | `raster` type, pixel functions |
| `postgis_topology` | `CREATE EXTENSION postgis_topology` | `TopoGeometry`, `topology` schema |

None of the last three is enabled by `CREATE EXTENSION postgis` alone.

## 3D Functions : Core PostGIS (no postgis_sfcgal)

In PostGIS 3.x the 3D distance / intersection family is core. The SFCGAL backend
for these was removed in PostGIS 3.0.0 ; they now use the GEOS backend.

| Function | Signature | Returns | Notes |
|----------|-----------|---------|-------|
| `ST_3DDistance` | `ST_3DDistance(geometry g1, geometry g2)` | `float` | Min 3D cartesian distance in projected units. SFCGAL version removed 3.0.0. |
| `ST_3DIntersects` | `ST_3DIntersects(geometry geomA, geometry geomB)` | `boolean` | True if geometries intersect in 3D. GEOS backend, supports TINs. Index-aware bbox pre-test. |
| `ST_3DDWithin` | `ST_3DDWithin(geometry g1, geometry g2, double precision distance)` | `boolean` | True if within `distance` (SRID units) in 3D. Index-aware. |
| `ST_3DClosestPoint` | `ST_3DClosestPoint(geometry g1, geometry g2)` | `geometry` | 3D point on g1 closest to g2. Same family as `ST_3DDistance`. |

### Z-coordinate handling (core PostGIS)

| Function | Signature | Purpose |
|----------|-----------|---------|
| `ST_Z` | `ST_Z(geometry a_point)` | Z value of a point geometry (NULL if 2D). |
| `ST_Force3D` | `ST_Force3D(geometry geom [, float Zvalue])` | Add a Z dimension (default 0). |
| `ST_Force2D` | `ST_Force2D(geometry geom)` | Drop the Z (and M) dimension. |
| `ST_MakePoint` | `ST_MakePoint(float x, float y, float z)` | Construct a 3D point. |

## 3D Functions : Require postgis_sfcgal

Calling any of these without `postgis_sfcgal` raises SQLSTATE `42883`
(undefined_function).

| Function | Signature | Returns | Notes |
|----------|-----------|---------|-------|
| `ST_Volume` | `ST_Volume(geometry geom1)` | `float` | Volume of a solid. Returns 0 for any surface, even a closed one. Availability 2.2.0. |
| `ST_3DArea` | `ST_3DArea(geometry geom1)` | `float` | Area of a 3D surface. Returns 0 for solids. Availability 2.1.0. |
| `ST_Extrude` | `ST_Extrude(geometry geom, float x, float y, float z)` | `geometry` | Sweep a surface along the x/y/z vector into a polyhedral surface. Preserves Z. Availability 2.1.0. |
| `ST_MakeSolid` | `ST_MakeSolid(geometry geom1)` | `geometry` | Reinterpret a closed polyhedral surface or closed TIN as a solid. No validation. Availability 2.2.0. |
| `ST_IsSolid` | `ST_IsSolid(geometry geom1)` | `boolean` | True if the geometry is a solid. |
| `ST_Tesselate` | `ST_Tesselate(geometry geom)` | `geometry` | Triangulate a polygon / polyhedral surface into a TIN (or collection of TINs). Preserves 3D. Availability 2.1.0. |
| `ST_3DConvexHull` | `ST_3DConvexHull(geometry geom1)` | `geometry` | 3D convex hull of the input. |

## Raster Functions (postgis_raster)

| Function | Signature | Purpose |
|----------|-----------|---------|
| `ST_AsRaster` | `ST_AsRaster(geometry geom, ...)` | Convert a vector geometry to a raster. |
| `ST_MakeEmptyRaster` | `ST_MakeEmptyRaster(integer width, integer height, float8 upperleftx, float8 upperlefty, float8 scale, ...)` | Create an empty raster with given dimensions and georeferencing. |
| `ST_AddBand` | `ST_AddBand(raster rast, text pixeltype [, double precision initialvalue [, double precision nodataval]])` | Add one or more bands of a given pixel type. |
| `ST_Band` | `ST_Band(raster rast, integer bandnum)` | Extract one or more bands as a new raster. |
| `ST_NumBands` | `ST_NumBands(raster rast)` | Number of bands in the raster. |
| `ST_Value` | `ST_Value(raster rast, integer band, geometry pt)` ; also `ST_Value(raster, integer band, integer x, integer y)` | Pixel value at a point or at column/row coordinates. |
| `ST_SetValue` | `ST_SetValue(raster rast, integer band, geometry pt, double precision newvalue)` | Return a raster with the pixel value changed. |
| `ST_Clip` | `ST_Clip(raster rast, geometry geom [, boolean crop])` | Raster clipped to the input geometry. |
| `ST_Intersection` | `ST_Intersection(raster rast, geometry geom)` | Set of geometry / pixel-value pairs from the raster-geometry overlap. |
| `ST_Intersects` | `ST_Intersects(raster rastA, raster rastB)` ; also `(raster, geometry)` | True if the two arguments spatially intersect. Index-aware. |
| `ST_SummaryStats` | `ST_SummaryStats(raster rast [, integer band])` | Record of count / sum / mean / stddev / min / max for a band. |

Raster functions have many overloaded variants ; the official reference lists the
complete set. The signatures above are the dominant forms.

## raster2pgsql Loader Flags

`raster2pgsql` is a command-line tool that converts GDAL-supported raster files
into SQL, piped into `psql`.

| Flag | Meaning |
|------|---------|
| `-s SRID` | Assign the spatial reference id. |
| `-t WIDTHxHEIGHT` | Cut the raster into tiles, one per table row. ALWAYS use this. |
| `-R` | Register as a filesystem (out-db) raster : only path + metadata stored. |
| `-C` | Apply raster constraints (srid, pixel size, etc.). |
| `-I` | Create a GiST spatial index on the raster column. |
| `-l OVERVIEW_FACTOR` | Build overview (reduced-resolution) tables. |
| `-a` | Append to an existing table instead of creating it. |
| `-d` | Drop and recreate the table. |

```bash
raster2pgsql -s 4326 -t 256x256 -I -C -R /data/dem/*.tif public.dem | psql -d gisdb
```

## Out-db Raster Settings

Out-db rasters store only a file path. Reading their pixels requires :

```sql
SET postgis.enable_outdb_rasters = true;
SET postgis.gdal_enabled_drivers TO 'ENABLE_ALL';
```

Set these per session, or persist them in `postgresql.conf` / `ALTER SYSTEM`.

### In-db vs out-db

| Aspect | In-db | Out-db (`-R`) |
|--------|-------|---------------|
| Pixel data location | inside the table | external file on disk / cloud |
| Table bloat | high for large rasters | minimal (path + metadata only) |
| Pixel-operation speed | faster | small read overhead |
| Self-contained backup | yes | no, external files needed |
| Best for | small / derived rasters | large imagery, DEM coverages |

## Topology (postgis_topology)

`postgis_topology` installs into a dedicated `topology` schema with metadata tables
`topology.topology` and `topology.layer`, plus primitive tables for nodes, edges,
and faces.

| Item | Description |
|------|-------------|
| `TopoGeometry` | Composite type : a geometry defined by references to topology primitives (nodes, edges, faces), not by stored coordinates. |
| `CreateTopology(name, srid, tolerance)` | Create a new topology and register it in `topology.topology`. |
| `AddTopoGeometryColumn(...)` | Add a `TopoGeometry` column to a layer table. |
| `topology.topology` / `topology.layer` | Metadata catalogs for topologies and layers. |

Topology stores each shared boundary edge ONCE. When many features share borders
(census tracts, cadastral parcels), this removes redundant coordinate storage and
lets PostGIS enforce topological consistency across features. Plain `geometry`
duplicates a shared border in every feature that touches it.

## SQLSTATE Reference

| SQLSTATE | Name | Trigger here |
|----------|------|--------------|
| `42883` | `undefined_function` | SFCGAL or raster function called without its extension installed |
| `42704` | `undefined_object` | Unknown SRID or missing extension object |

## Sources

- https://postgis.net/docs/manual-3.4/
- https://postgis.net/docs/manual-3.4/ST_3DDistance.html
- https://postgis.net/docs/manual-3.4/ST_3DIntersects.html
- https://postgis.net/docs/manual-3.4/ST_3DDWithin.html
- https://postgis.net/docs/manual-3.4/ST_Volume.html
- https://postgis.net/docs/manual-3.4/ST_Extrude.html
- https://postgis.net/docs/manual-3.4/ST_3DArea.html
- https://postgis.net/docs/manual-3.4/ST_MakeSolid.html
- https://postgis.net/docs/manual-3.4/ST_Tesselate.html
- https://postgis.net/docs/manual-3.4/RT_reference.html
- https://postgis.net/docs/manual-3.4/using_raster_dataman.html
- https://postgis.net/docs/manual-3.4/Topology.html
