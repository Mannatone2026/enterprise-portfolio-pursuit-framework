# Anti-Patterns : postgres-impl-logical-replication

Each entry : cause, symptom, SQLSTATE where applicable, and the fix pattern.
Verified against the PostgreSQL 17 documentation.

## AP-1 : REPLICA IDENTITY DEFAULT on a PK-less table

CAUSE : a table with no primary key is added to a publication that replicates
`UPDATE`/`DELETE`, while the table keeps the default `REPLICA IDENTITY DEFAULT`.

SYMPTOM : on the publisher, an `UPDATE` or `DELETE` raises an error such as
`cannot update table "t" because it does not have a replica identity and publishes
updates` (SQLSTATE `55000`, object_not_in_prerequisite_state). If `publish` excludes
those operations, the changes are simply never replicated, silently.

FIX : give the table a primary key, or set an explicit replica identity.

```sql
ALTER TABLE t REPLICA IDENTITY USING INDEX t_unique_idx;   -- preferred
-- or, if no usable unique index exists :
ALTER TABLE t REPLICA IDENTITY FULL;
```

## AP-2 : Abandoned replication slot

CAUSE : a subscription is removed on the subscriber side without `DROP
SUBSCRIPTION` (e.g. the subscriber database is dropped), or a subscriber stays
offline indefinitely, leaving its slot on the publisher with no consumer.

SYMPTOM : `pg_wal` on the publisher grows without bound because the slot pins every
WAL segment since `confirmed_flush_lsn`. Eventually the publisher's disk fills and
it stops accepting writes.

FIX : monitor `pg_replication_slots` and drop orphan slots.

```sql
SELECT slot_name, active,
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(),
                                      confirmed_flush_lsn)) AS retained
FROM pg_replication_slots;
SELECT pg_drop_replication_slot('orphan_slot');   -- on the publisher
```

Prevent it : always tear down with `DROP SUBSCRIPTION` (which drops the slot too),
and alarm on slot retention size.

## AP-3 : DDL changed on one side only

CAUSE : an `ALTER TABLE` (new column, type change, dropped column) is run on the
publisher or the subscriber but not the other ; logical replication never
replicates DDL.

SYMPTOM : the apply worker stops with a column-count or type-mismatch error ;
`pg_stat_subscription` shows the subscription not advancing.

FIX : apply every schema change on BOTH sides, ordered safely. Additive changes
(new nullable/defaulted column) : subscriber first, then publisher. Destructive
changes (drop column) : publisher first, then subscriber. After changing the column
set, run `ALTER SUBSCRIPTION s REFRESH PUBLICATION`.

## AP-4 : REPLICA IDENTITY FULL on every table

CAUSE : setting `REPLICA IDENTITY FULL` everywhere as a blanket habit instead of
relying on primary keys.

SYMPTOM : two costs. Apply side : with `FULL`, the subscriber locates the target
row by a full-row scan instead of an index lookup, which is slow on wide or large
tables. Data side : `UPDATE`/`DELETE` cannot be applied if the table has a column
of a type with no default btree/hash operator class (`point`, `box`) unless a PK
or replica identity exists.

FIX : use a primary key with `REPLICA IDENTITY DEFAULT` wherever possible ; use
`USING INDEX` when there is a usable unique index ; reserve `FULL` for the genuine
no-PK, no-unique-index case.

## AP-5 : Treating sequences as replicated

CAUSE : assuming a `serial` / `identity` column's sequence is kept in sync, then
failing over to the subscriber.

SYMPTOM : the column VALUES replicate as ordinary data, but the sequence object on
the subscriber stays at its start value. After promoting the subscriber, new
inserts reuse already-present IDs, causing duplicate-key violations (SQLSTATE
`23505`, unique_violation).

FIX : before promoting a subscriber, advance every sequence past the maximum value
present in its table.

```sql
SELECT setval('orders_id_seq', (SELECT max(id) FROM orders));
```

## AP-6 : Row filter or column list on a non-identity column

CAUSE : a publication that replicates `UPDATE`/`DELETE` uses a row filter `WHERE`
clause or a column list that references a column not covered by the table's
`REPLICA IDENTITY`.

SYMPTOM : `CREATE PUBLICATION` / `ALTER PUBLICATION` is rejected with an error
stating the filter or column list must use only replica identity columns.

FIX : either restrict the filter/column list to replica-identity columns, or widen
the replica identity (add the column to the PK or the `USING INDEX` index, or use
`FULL`), or limit the publication to `publish = 'insert'` where the restriction
does not apply.

## AP-7 : Blind ALTER SUBSCRIPTION ... SKIP

CAUSE : skipping a conflicting transaction with `ALTER SUBSCRIPTION s SKIP (LSN
...)` without checking what else that transaction contained.

SYMPTOM : the conflict clears, but the skipped transaction also dropped every
non-conflicting row change it carried. The subscriber is now silently inconsistent
with the publisher, often discovered much later.

FIX : prefer fixing the subscriber data so the change applies. Only `SKIP` a
transaction after confirming its full contents are safe to discard. For a recurring
problem, create the subscription `WITH (disable_on_error = true)` and resolve
deliberately rather than skipping under pressure.

## AP-8 : CREATE SUBSCRIPTION inside a transaction block

CAUSE : wrapping `CREATE SUBSCRIPTION` (with default `create_slot = true`) inside
`BEGIN ... COMMIT`, or running it in a tool that opens an implicit transaction.

SYMPTOM : the statement fails because creating a replication slot cannot run inside
a transaction block.

FIX : run `CREATE SUBSCRIPTION` as its own statement outside any transaction. If it
must run inside a transaction, use `WITH (create_slot = false)` and create the slot
separately, or `WITH (connect = false)` and finish setup afterward.

## Sources

- https://www.postgresql.org/docs/17/logical-replication.html
- https://www.postgresql.org/docs/17/sql-createpublication.html
- https://www.postgresql.org/docs/17/sql-createsubscription.html
- https://www.postgresql.org/docs/17/sql-altertable.html
- https://www.postgresql.org/docs/17/logical-replication-restrictions.html
- https://www.postgresql.org/docs/17/logical-replication-conflicts.html
- https://www.postgresql.org/docs/17/errcodes-appendix.html
