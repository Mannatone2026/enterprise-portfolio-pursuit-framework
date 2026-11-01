# Schema Archaeology : Methods Reference

Catalog tables, statistics views, size functions, and psql meta-commands used to
inspect an existing database. Column names verified against PostgreSQL 17
official documentation.

## Introspection surfaces

| Surface | Scope | Speed | Use when |
|---------|-------|-------|----------|
| `pg_catalog` | PostgreSQL-specific, complete | fast | default for all PostgreSQL introspection |
| `information_schema` | SQL-standard subset | slower | the query must run on other SQL vendors too |

`pg_catalog` and `information_schema` are both always on the `search_path`, so
their objects can be referenced unqualified.

## Core catalog tables

### pg_class : tables, indexes, views, sequences

| Column | Meaning |
|--------|---------|
| `oid` | object identifier; cast `'schema.name'::regclass` to get it |
| `relname` | object name (not schema-qualified) |
| `relnamespace` | OID of the containing schema (join `pg_namespace`) |
| `relkind` | `r` table, `i` index, `v` view, `m` matview, `S` sequence, `p` partitioned table, `f` foreign table, `t` TOAST |
| `relpages` | size in 8 KB pages, as of the last `VACUUM`/`ANALYZE` |
| `reltuples` | estimated live row count, as of the last `VACUUM`/`ANALYZE` |
| `relhasindex` | true if the table has (or recently had) any index |

### pg_attribute : columns

| Column | Meaning |
|--------|---------|
| `attrelid` | OID of the table the column belongs to |
| `attname` | column name |
| `attnum` | column position; **negative for system columns**, gaps after a drop |
| `atttypid` | data type OID (join `pg_type`) |
| `attnotnull` | true if `NOT NULL` |
| `atthasdef` | true if the column has a `DEFAULT` (look in `pg_attrdef`) |
| `attidentity` | `a` GENERATED ALWAYS AS IDENTITY, `d` BY DEFAULT, empty otherwise |
| `attgenerated` | `s` STORED generated column, empty otherwise |
| `attisdropped` | true for a dropped column still occupying an `attnum` slot |

ALWAYS filter `attnum > 0 AND NOT attisdropped` to get only live user columns.

### pg_constraint : keys, checks, exclusions

| Column | Meaning |
|--------|---------|
| `conname` | constraint name |
| `contype` | `c` check, `f` foreign key, `p` primary key, `u` unique, `x` exclusion, `n` not-null (domains only) |
| `conrelid` | table the constraint is defined on (the child for an FK) |
| `confrelid` | referenced table (the parent for an FK; 0 for non-FK) |
| `conkey` | int2 array of `attnum`s the constraint covers on `conrelid` |
| `confkey` | int2 array of referenced `attnum`s on `confrelid` |
| `convalidated` | false for an FK/CHECK added `NOT VALID` and not yet validated |
| `condeferrable` / `condeferred` | constraint deferral flags |

FK direction : incoming references = `WHERE confrelid = target`, outgoing
references = `WHERE conrelid = target`.

### pg_depend : the dependency graph

| Column | Meaning |
|--------|---------|
| `classid` | catalog OID describing the dependent object |
| `objid` | OID of the dependent object |
| `objsubid` | column number when the dependent object is a table column, else 0 |
| `refclassid` | catalog OID describing the referenced object |
| `refobjid` | OID of the referenced object |
| `refobjsubid` | column number of the referenced object, else 0 |
| `deptype` | dependency kind, see codes below |

`deptype` codes : `n` normal (dependent needs explicit `CASCADE`), `a` auto
(dependent auto-drops with the referenced object), `i` internal (direct drop of
the dependent is disallowed), `e` extension (dependent drops only via
`DROP EXTENSION`), `x` auto-extension, `P`/`S` partition dependencies.

## Statistics views

### pg_stat_user_indexes / pg_stat_all_indexes

| Column | Meaning |
|--------|---------|
| `schemaname`, `relname`, `indexrelname` | schema, table, index names |
| `relid`, `indexrelid` | table and index OIDs |
| `idx_scan` | number of index scans the planner has performed |
| `last_idx_scan` | timestamp of the most recent scan (v16+) |
| `idx_tup_read` | index entries returned by scans |
| `idx_tup_fetch` | live table rows fetched via this index |

`idx_scan = 0` flags a never-used index. Counters reset on `pg_stat_reset()` and
on crash recovery, so verify the window before acting.

### pg_stat_user_tables / pg_stat_all_tables

| Column | Meaning |
|--------|---------|
| `schemaname`, `relname` | schema and table names |
| `seq_scan`, `idx_scan` | sequential vs index scan counts |
| `n_live_tup`, `n_dead_tup` | estimated live and dead tuple counts |
| `n_mod_since_analyze` | rows changed since the last `ANALYZE` |
| `n_ins_since_vacuum` | rows inserted since the last `VACUUM` |
| `n_tup_hot_upd` | HOT updates (no index churn) |
| `last_vacuum`, `last_autovacuum` | timestamps of the last manual / auto vacuum |
| `last_analyze`, `last_autoanalyze` | timestamps of the last manual / auto analyze |

High `n_dead_tup` relative to `n_live_tup` signals bloat. A high
`n_mod_since_analyze` signals stale planner statistics.

### pg_stat_activity (current sessions)

Key columns : `pid`, `usename`, `application_name`, `state`, `wait_event_type`,
`wait_event`, `query_start`, `xact_start`, `query`, `backend_type`. Used to spot
`idle in transaction` sessions that block `VACUUM` and hold locks.

## Database object size functions

| Function | Measures |
|----------|----------|
| `pg_relation_size(regclass [, fork])` | one fork; main data fork only by default |
| `pg_table_size(regclass)` | heap + TOAST + FSM + VM, indexes excluded |
| `pg_indexes_size(regclass)` | all indexes attached to the table |
| `pg_total_relation_size(regclass)` | heap + TOAST + all indexes (table + indexes) |
| `pg_database_size(name)` | the whole database |
| `pg_column_size("any")` | bytes for one stored value, after compression |
| `pg_size_pretty(bigint)` | formats a byte count as `kB`/`MB`/`GB` text |

`pg_total_relation_size` equals `pg_table_size + pg_indexes_size`.

## Object description helpers

| Function | Returns |
|----------|---------|
| `pg_describe_object(classid, objid, objsubid)` | human-readable name of an object identified by a `pg_depend` OID triple |
| `pg_identify_object(classid, objid, objsubid)` | a row of `(type, schema_name, name, identity)` |
| `'schema.name'::regclass` | the OID of a table/index/view by name |
| `oid::regclass` | the name of an object from its OID |

## pg_catalog vs information_schema view map

| Goal | pg_catalog | information_schema |
|------|-----------|--------------------|
| List tables | `pg_class` + `pg_namespace` | `information_schema.tables` |
| List columns | `pg_attribute` + `pg_class` | `information_schema.columns` |
| List constraints | `pg_constraint` | `table_constraints` + `key_column_usage` |
| List foreign keys | `pg_constraint` `contype='f'` | `referential_constraints` |
| List indexes | `pg_index` + `pg_class` | not exposed |
| Usage statistics | `pg_stat_user_*` | not exposed |
| Object OIDs / system columns | yes | not exposed |

## psql meta-commands

| Command | Lists | `+` variant |
|---------|-------|-------------|
| `\d name` | one object in detail | `\d+` adds size, storage, comments |
| `\dt [pattern]` | tables | `\dt+` size, description |
| `\di [pattern]` | indexes | `\di+` size |
| `\dv` | views | `\dv+` definition |
| `\dm` | materialized views | `\dm+` size |
| `\df [pattern]` | functions | `\df+` source, volatility, owner |
| `\dn` | schemas | `\dn+` privileges |
| `\dp` / `\z` | table access privileges | |
| `\du` / `\dg` | roles | `\du+` description |
| `\dx` | installed extensions | `\dx+` member objects |
| `\det` | foreign tables | `\det+` server, options |
| `\l` | databases | `\l+` size, description |
| `\dy` | event triggers | |

Patterns accept wildcards : `\dt sales*`, `\df public.*`. `\d` with no pattern
lists all relations in the search path.
