# PostgreSQL Claude Skills

38 deterministic PostgreSQL skills for Claude Code, vendored into this repo so
they auto-discover for anyone working in it (no per-developer setup needed).

Claude Code scans `.claude/skills/` and activates a skill when a request matches
its `description`/keyword triggers, so these load automatically — no config
required.

## Layout

Skills are grouped by category, each in its own folder with a `SKILL.md` plus a
`references/` directory (`methods.md`, `examples.md`, `anti-patterns.md`):

- `postgres-core/` — architecture, MVCC, security model, RLS, schema design, version matrix (6)
- `postgres-syntax/` — DML/DDL, UPSERT/MERGE, JSONB, CTEs, window functions, arrays/ranges, FTS, lateral joins (8)
- `postgres-impl/` — indexing, partitioning, PostGIS, pgvector, replication, migrations, vacuum/bloat, etc. (15)
- `postgres-errors/` — deadlocks, constraint violations, serialization, timeouts, connection/auth, replication lag (6)
- `postgres-agents/` — query, migration, and schema reviewer agent skills (3)

Targets PostgreSQL 15, 16, and 17.

## Provenance

Vendored from the **PostgreSQL-Claude-Skill-Package**
(https://github.com/Impertio-Studio/PostgreSQL-Claude-Skill-Package),
authored by the OpenAEC Foundation. Licensed under MIT — see `LICENSE` in this
folder.

To update, re-copy `skills/source/` from the upstream package.
