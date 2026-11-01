---
name: postgres-errors-connection-auth
description: >
  Use when connections fail with authentication errors, no pg_hba.conf entry, SSL handshake problems, or too-many-connections.
  Prevents trust auth on production hosts, sslmode=require giving a false sense of security (no server verification), editing pg_hba.conf without reloading, and raising max_connections instead of using a pooler.
  Covers pg_hba.conf rule format and ordering, auth methods (trust / peer / md5 / scram-sha-256 / cert), "no pg_hba.conf entry" and "password authentication failed" diagnosis, password_encryption, SSL sslmode levels, SQLSTATE 28000 / 28P01 / 53300, connection pooling guidance.
  Keywords: pg_hba.conf, authentication failed, no pg_hba.conf entry for host, scram-sha-256, md5, peer, trust, sslmode, SSL connection, password authentication failed, too many connections, 28P01, 53300, cannot connect to postgres, connection refused, max_connections
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-errors-connection-auth

## Quick Reference :

PostgreSQL rejects a client in two distinct phases. First the server checks `pg_hba.conf` (Host-Based Authentication): it scans records top-down and the **first match wins** on connection type, client address, database, and user. No match means `no pg_hba.conf entry for host ...`. Second, the chosen method runs: `scram-sha-256`, `md5`, `peer`, `cert`, etc. A failed password gives `password authentication failed for user ...` (SQLSTATE `28P01`). A connection that gets past auth can still be refused when the server is full: `too many clients already` / `remaining connection slots are reserved ...` (SQLSTATE `53300`).

```
Client connect attempt
   |
   v
pg_hba.conf scan (top-down, first match wins)
   |-- no matching record  --> FATAL: no pg_hba.conf entry  (28000)
   |-- matched, method = reject --> FATAL: pg_hba.conf rejects connection
   v
Run auth method (scram-sha-256 / md5 / peer / cert / ...)
   |-- bad password / verifier mismatch --> FATAL: password authentication failed  (28P01)
   v
Slot check
   |-- max_connections reached --> FATAL: too many clients already  (53300)
   v
Connected
```

ALWAYS use `scram-sha-256` for password auth and `sslmode=verify-full` for remote TLS. NEVER use `trust` on any host that is not a tightly controlled Unix socket. ALWAYS reload (`SELECT pg_reload_conf()`) after editing `pg_hba.conf`.

## When To Use This Skill :

ALWAYS use this skill when :
- A client cannot connect and the server log shows `no pg_hba.conf entry`, `password authentication failed`, or `pg_hba.conf rejects connection`
- An app reports SQLSTATE `28000`, `28P01`, or `53300`
- Configuring or auditing `pg_hba.conf`, auth methods, or `password_encryption`
- Setting up TLS / `sslmode` between client and server, or diagnosing `server does not support SSL`
- The server is out of connection slots (`too many clients already`) and you must decide between raising `max_connections` and adding a pooler

NEVER use this skill for :
- Role privileges, `GRANT`, RLS policies (use the security / roles skill)
- Query-time permission errors like `permission denied for table` (SQLSTATE `42501`) once already connected
- Network-layer failures (`Connection refused`, wrong port, firewall) that never reach `pg_hba.conf`

## Decision Trees :

### Which connection error is this? :

```
Server log / client error mentions...
  "no pg_hba.conf entry for host"      --> missing/too-narrow host record. Add a line + reload.
  "pg_hba.conf rejects connection"     --> a 'reject' record matched first. Reorder or remove it.
  "password authentication failed"     --> wrong password OR method/verifier mismatch (see tree below).
  "no PostgreSQL user name specified"  --> client sent no user; fix the connection string.
  "server does not support SSL"        --> client wants SSL, server has ssl=off (or hostnossl matched).
  "SSL connection is required"         --> hostssl rule, client used sslmode=disable/allow.
  "too many clients already"           --> max_connections reached. Pool, do not just raise it.
  "remaining connection slots are reserved" --> hit max_connections - superuser_reserved_connections.
  "Connection refused" (no FATAL)      --> NOT auth. Wrong host/port, server down, listen_addresses.
```

### password authentication failed -- root cause :

```
Got "password authentication failed for user X" (28P01)?
  |
  |-- Password recently changed?            --> client uses a stale password. Update the client.
  |
  |-- pg_hba method = md5, role password stored as SCRAM?
  |     SELECT rolname FROM pg_authid WHERE rolname='X';  -- check rolpassword prefix
  |     SCRAM verifier + md5 line can still work; md5 verifier + scram line CANNOT.
  |     --> ALTER ROLE X PASSWORD '...' after setting password_encryption correctly.
  |
  |-- password_encryption changed but password never re-set?
  |     --> old verifier still stored. Re-run ALTER ROLE X PASSWORD '...'.
  |
  |-- Right password, still fails on md5->scram switch?
        --> the stored md5 verifier is unusable by a scram-sha-256 line. Re-set the password.
```

### Choosing an auth method :

```
Connection type?
  local (Unix socket), same OS user as DB role   --> peer
  local, service account / scripts               --> scram-sha-256
  host (TCP), password login                     --> scram-sha-256   (NEVER md5, NEVER trust)
  host (TCP), machine-to-machine, certs available --> cert  (with hostssl)
  host, enterprise SSO                            --> ldap / gss
  a path you must hard-block                      --> reject
NEVER: trust on a TCP host. NEVER: md5 for new setups in 2026.
```

### Choosing sslmode (client side) :

```
Where does the client connect from?
  Same host, Unix socket            --> sslmode not applicable (local socket).
  Trusted private network, low risk --> sslmode=require  (encryption only -- see WHY in Pattern 5)
  Public network / production       --> sslmode=verify-full  (encryption + CA + hostname)
  You have a private CA only        --> sslmode=verify-ca minimum, verify-full preferred
NEVER ship production with sslmode=require and call it "secure": it does NOT stop MITM.
```

### Out of connections -- raise the cap or pool? :

```
Hitting "too many clients already" (53300)?
  |
  |-- Are there hundreds of mostly-idle connections? --> connection leak or no pooler.
  |     Fix: PgBouncer in transaction mode. Do NOT raise max_connections.
  |
  |-- Genuinely need many concurrent ACTIVE queries? --> still pool first.
  |     max_connections costs memory + lock contention per slot. 100-300 is the sane band.
  |
  |-- Emergency, locked out as a normal user? --> superuser_reserved_connections keeps
        a superuser slot free: connect as superuser, terminate leaked backends.
```

## Patterns :

### Pattern 1 : Add a pg_hba.conf record and reload

ALWAYS append the new record, then reload. NEVER restart the server for an HBA change.
NEVER assume the edit took effect: verify with `pg_hba_file_rules`.

```
# pg_hba.conf  -- TYPE  DATABASE  USER  ADDRESS  METHOD  [OPTIONS]
# Records are scanned top-down; the FIRST match wins.
hostssl  shop      app       10.0.0.0/24      scram-sha-256
local    all       all                       peer
```

```sql
SELECT pg_reload_conf();                       -- apply without downtime
SELECT line_number, type, database, user_name, address, auth_method, error
  FROM pg_hba_file_rules ORDER BY line_number;  -- error IS NOT NULL = bad line
```

WHY : `pg_hba.conf` is re-read on `SIGHUP`. `pg_reload_conf()` (or `pg_ctl reload`) sends it. A syntax error does not crash the server; it shows up as a non-null `error` in `pg_hba_file_rules` and the bad line is skipped, so a "fixed" rule may silently never load.

### Pattern 2 : Diagnose "no pg_hba.conf entry for host"

ALWAYS read the four facts the error reports: host, user, database, encryption.
NEVER widen the rule to `host all all 0.0.0.0/0 trust` to make the error disappear.

```
FATAL:  no pg_hba.conf entry for host "10.0.0.5", user "app", database "shop", no encryption
```

```
# The record must match ALL FOUR. A too-narrow ADDRESS is the usual cause.
# This rule fails the client above because 10.0.0.5 is outside 10.0.0.0/28:
host   shop   app   10.0.0.0/28   scram-sha-256
# Fix: widen the CIDR to the real client subnet, then reload.
host   shop   app   10.0.0.0/24   scram-sha-256
```

WHY : the message is exact. `no encryption` means the client did not use SSL, so a `hostssl`-only rule will never match it; `host` matches both. A matching record is mandatory: there is no fall-through and no default-allow.

### Pattern 3 : Migrate md5 passwords to scram-sha-256

ALWAYS set `password_encryption` first, then re-set every password, then switch HBA lines.
NEVER flip HBA lines to `scram-sha-256` while passwords are still stored as md5 verifiers.

```sql
SHOW password_encryption;          -- scram-sha-256 is the default since v14
ALTER SYSTEM SET password_encryption = 'scram-sha-256';
SELECT pg_reload_conf();

-- Re-set EACH role's password so a SCRAM verifier gets stored:
ALTER ROLE app PASSWORD 'new-strong-secret';

-- Find roles still on an md5 verifier:
SELECT rolname FROM pg_authid WHERE rolpassword LIKE 'md5%';
```

WHY : `password_encryption` only controls how the *next* `ALTER ROLE ... PASSWORD` stores the verifier. Existing md5 verifiers stay md5 until re-set. A `scram-sha-256` HBA line cannot authenticate a role whose stored verifier is md5, producing `28P01` for a "correct" password.

### Pattern 4 : Enforce TLS with server-verified clients

ALWAYS pair server `ssl = on` with `hostssl` records and clients on `sslmode=verify-full`.
NEVER rely on `host` lines to enforce encryption: `host` accepts SSL and non-SSL alike.

```
# postgresql.conf
ssl = on
ssl_cert_file = 'server.crt'
ssl_key_file  = 'server.key'
```

```
# pg_hba.conf -- hostssl REQUIRES the connection to be SSL-encrypted
hostssl  shop  app  10.0.0.0/24  scram-sha-256
```

```bash
# Client: full verification (CA chain + hostname)
psql "host=db.example.com dbname=shop user=app sslmode=verify-full sslrootcert=root.crt"
```

WHY : server SSL settings are start-time only, so `ssl = on` needs a restart. `hostssl` rejects plaintext attempts with `SSL connection is required`. `verify-full` is the only `sslmode` that both encrypts and proves the server's identity.

### Pattern 5 : Never trust sslmode=require for production

ALWAYS use `verify-full` on untrusted networks. NEVER treat `require` as MITM-safe.

```
sslmode=require       -- encrypts traffic, does NOT verify the server cert  -> MITM possible
sslmode=verify-ca     -- encrypts + cert chains to a trusted CA
sslmode=verify-full   -- encrypts + CA + hostname match  (production default)
```

WHY : `require` only guarantees *some* TLS session exists; it never checks who the peer is. An attacker terminating TLS in the middle satisfies `require` perfectly. Only `verify-ca`/`verify-full` validate the certificate. The libpq default `prefer` is weaker still and is kept only for backward compatibility.

### Pattern 6 : Handle "too many clients already" with a pooler

ALWAYS put a pooler in front before raising `max_connections`. NEVER scale `max_connections`
into the thousands: each slot reserves memory and adds lock-table contention.

```sql
-- Triage: who is holding the slots?
SELECT state, count(*) FROM pg_stat_activity GROUP BY state ORDER BY 2 DESC;

-- Reclaim a leaked idle backend (superuser, via a reserved slot):
SELECT pg_terminate_backend(pid)
  FROM pg_stat_activity
 WHERE state = 'idle' AND state_change < now() - interval '1 hour';
```

WHY : `too many clients already` (SQLSTATE `53300`) means active backends hit `max_connections`. `superuser_reserved_connections` (default 3) keeps a superuser slot free so you can still log in. The fix is a transaction-mode pooler (PgBouncer): hundreds of app clients multiplex onto a small fixed server pool.

## Anti-Patterns :

(Short list, full cause + symptom + fix in references/anti-patterns.md)

- `trust` auth on a TCP / production host : no password at all : exposes the database
- `md5` instead of `scram-sha-256` in 2026 : weak verifier, downgradable
- `sslmode=require` sold as secure : encrypts but does NOT verify the server (MITM)
- Editing `pg_hba.conf` without reload : change silently inactive until `SIGHUP`
- Raising `max_connections` to thousands : memory + lock contention, use a pooler : SQLSTATE `53300`
- Switching HBA to `scram-sha-256` before re-setting passwords : SQLSTATE `28P01`
- Widening a CIDR to `0.0.0.0/0` to clear `no pg_hba.conf entry` : SQLSTATE `28000`

## Reference Links :

- [references/methods.md](references/methods.md) : pg_hba.conf field reference, auth-method table, sslmode table, server/client SSL parameters, diagnostic views and functions
- [references/examples.md](references/examples.md) : working pg_hba.conf records, psql/libpq connection strings, SCRAM migration, pooler config, version-annotated
- [references/anti-patterns.md](references/anti-patterns.md) : anti-patterns with cause + symptom + fix + SQLSTATE

## See Also :

- postgres-errors-deadlocks : other server-side failure classes and SQLSTATE triage
- postgres-core / roles skill : role attributes, GRANT, RLS once a session is established
- Vooronderzoek section : §13 (Security Model), §14 (Configuration Patterns)
- Official docs : https://www.postgresql.org/docs/17/auth-pg-hba-conf.html , https://www.postgresql.org/docs/17/errcodes-appendix.html
