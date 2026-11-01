# Examples : postgres-impl-logical-replication

Working, version-annotated code samples. Statements run on PostgreSQL 15, 16, and
17 ; features newer than v15 are annotated. Verified against the PostgreSQL 17
documentation.

## 1. End-to-end : replicate two tables

```sql
-- ===== PUBLISHER side =====
-- Prerequisite (postgresql.conf, needs restart) : wal_level = logical
SHOW wal_level;                                   -- 'logical'

CREATE ROLE repuser WITH REPLICATION LOGIN PASSWORD 'secret';
-- pg_hba.conf : host  salesdb  repuser  10.0.0.0/24  scram-sha-256
-- The publisher role also needs SELECT on the published tables.

GRANT SELECT ON orders, customers TO repuser;

CREATE PUBLICATION sales_pub FOR TABLE orders, customers;   -- (v15+ syntax fine on v15-17)

-- ===== SUBSCRIBER side (a different database) =====
-- Schema must already exist : copy it once with pg_dump --schema-only.
CREATE SUBSCRIPTION sales_sub
  CONNECTION 'host=pub.db port=5432 user=repuser password=secret dbname=salesdb'
  PUBLICATION sales_pub
  WITH (copy_data = true, disable_on_error = true);
-- Initial COPY of orders + customers, then streaming begins.
```

## 2. REPLICA IDENTITY for a table without a primary key

```sql
-- This table has no PK. UPDATE/DELETE would NOT replicate at DEFAULT.
CREATE TABLE event_log (
  occurred_at timestamptz NOT NULL,
  source      text        NOT NULL,
  payload     jsonb
);

-- Option A : a unique index on NOT NULL columns.
CREATE UNIQUE INDEX event_log_uniq ON event_log (occurred_at, source);
ALTER TABLE event_log REPLICA IDENTITY USING INDEX event_log_uniq;

-- Option B : FULL (logs every column ; costly apply, no index needed).
ALTER TABLE event_log REPLICA IDENTITY FULL;

-- Best : just add a primary key and keep REPLICA IDENTITY DEFAULT.
```

## 3. Row filter and column list (v15+)

```sql
-- Row filter : only EU orders reach the subscriber. (v15+)
CREATE PUBLICATION eu_pub FOR TABLE orders WHERE (region = 'EU');

-- Column list : ship only non-sensitive customer columns. (v15+)
CREATE PUBLICATION lean_pub FOR TABLE customers (id, name, region);

-- Combined on one table : filtered rows AND a column subset. (v15+)
CREATE PUBLICATION eu_lean FOR TABLE customers (id, name, region)
  WHERE (region = 'EU');

-- Whole schema. (v15+)
CREATE PUBLICATION app_pub FOR TABLES IN SCHEMA app;
```

## 4. Insert-only audit feed

```sql
-- publish = 'insert' : UPDATE/DELETE never sent, so REPLICA IDENTITY
-- of audit_log is irrelevant.
CREATE PUBLICATION audit_pub FOR TABLE audit_log WITH (publish = 'insert');

CREATE SUBSCRIPTION audit_sub
  CONNECTION 'host=pub.db user=repuser dbname=appdb'
  PUBLICATION audit_pub
  WITH (copy_data = false);   -- start fresh, no historical copy
```

## 5. Deferred subscription (set up now, start later)

```sql
CREATE SUBSCRIPTION sales_sub
  CONNECTION 'host=pub.db user=repuser dbname=salesdb'
  PUBLICATION sales_pub
  WITH (enabled = false);

-- ... later, e.g. after a maintenance window :
ALTER SUBSCRIPTION sales_sub ENABLE;
```

## 6. Syncing a DDL change

```sql
-- Adding a nullable column : SUBSCRIBER FIRST.
--   on subscriber :
ALTER TABLE orders ADD COLUMN delivery_note text;
--   then on publisher :
ALTER TABLE orders ADD COLUMN delivery_note text;

-- Adding a table to an existing publication :
--   on publisher :
ALTER PUBLICATION sales_pub ADD TABLE shipments;
--   on subscriber : pick up the new table and copy its data :
ALTER SUBSCRIPTION sales_sub REFRESH PUBLICATION;
```

## 7. Resolve a stuck subscription

```sql
-- The subscriber log shows :
--   ERROR: duplicate key value violates unique constraint "orders_pkey"
--   ... transaction ... finished at 0/14C0378

-- Option A : fix the data, replication resumes automatically.
DELETE FROM orders WHERE id = <conflicting id>;   -- on the subscriber

-- Option B : skip the whole transaction (may cause inconsistency).
ALTER SUBSCRIPTION sales_sub SKIP (LSN '0/14C0378');

-- Option C : advance the origin while the subscription is disabled.
ALTER SUBSCRIPTION sales_sub DISABLE;
SELECT pg_replication_origin_advance('pg_16395', '0/14C0379');  -- finish_lsn + 1
ALTER SUBSCRIPTION sales_sub ENABLE;
```

## 8. Monitor lag and slot retention

```sql
-- On the SUBSCRIBER : apply progress.
SELECT subname, received_lsn, latest_end_lsn, last_msg_receipt_time
FROM pg_stat_subscription;

-- On the PUBLISHER : WAL each slot is holding back.
SELECT slot_name, active,
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(),
                                      confirmed_flush_lsn)) AS retained_wal
FROM pg_replication_slots;
-- Alarm when retained_wal grows without bound : a stalled subscriber.
```

## 9. Clean teardown

```sql
-- Normal case : publisher reachable. Drops the slot in the same step.
DROP SUBSCRIPTION sales_sub;

-- Publisher unreachable : detach the slot first, then drop.
ALTER SUBSCRIPTION sales_sub SET (slot_name = NONE);
DROP SUBSCRIPTION sales_sub;
-- Then, once the publisher is reachable, free the orphan slot there :
SELECT pg_drop_replication_slot('sales_sub');
```

## 10. Bidirectional replication (v16+)

```sql
-- Each node publishes its tables and subscribes to the other, with
-- origin = none so changes do not loop back. (v16+)
-- On node A and node B both :
CREATE PUBLICATION node_pub FOR TABLE shared_table;

-- On node A, subscribing to node B :
CREATE SUBSCRIPTION a_from_b
  CONNECTION 'host=nodeB user=repuser dbname=appdb'
  PUBLICATION node_pub
  WITH (origin = none, copy_data = false);
-- Symmetric subscription on node B. origin = none prevents echo loops.
```

## 11. Convert a physical standby with pg_createsubscriber (v17+)

```bash
# Run on the standby host. The standby must be running ; the publisher
# must have wal_level = logical. Skips the initial data copy entirely.
pg_createsubscriber \
  --pgdata /var/lib/postgresql/17/standby \
  --publisher-server 'host=primary.db user=repuser dbname=appdb' \
  --database appdb \
  --dry-run                     # validate first

# Then run for real by removing --dry-run.
```

## Sources

- https://www.postgresql.org/docs/17/logical-replication.html
- https://www.postgresql.org/docs/17/sql-createpublication.html
- https://www.postgresql.org/docs/17/sql-createsubscription.html
- https://www.postgresql.org/docs/17/sql-altertable.html
- https://www.postgresql.org/docs/17/logical-replication-conflicts.html
- https://www.postgresql.org/docs/17/app-pgcreatesubscriber.html
