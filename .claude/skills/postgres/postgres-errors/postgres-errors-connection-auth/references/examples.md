# Reference : Connection + Authentication Examples

Working, version-annotated examples. All verified against PostgreSQL 17 docs
(auth-pg-hba-conf, libpq-ssl, runtime-config-connection). Last verified : 2026-05-19.
Annotation `v15+` means valid for the supported range 15/16/17; `v16+`/`v17+`
mark features introduced later.

## 1. A sane pg_hba.conf (v15+)

```
# TYPE      DATABASE   USER   ADDRESS           METHOD
# --- local socket: OS user maps to DB role, no password ---
local       all        all                      peer

# --- loopback TCP: still require a password ---
host        all        all    127.0.0.1/32      scram-sha-256
host        all        all    ::1/128           scram-sha-256

# --- app server subnet: SSL mandatory + password ---
hostssl     shop       app    10.0.0.0/24       scram-sha-256

# --- replication standby: SSL + password ---
hostssl     replication  replicator  10.0.1.5/32   scram-sha-256

# --- everything else is denied implicitly (no fall-through) ---
```

Apply and verify :

```sql
SELECT pg_reload_conf();
SELECT line_number, type, database, user_name, address, auth_method, error
  FROM pg_hba_file_rules
 ORDER BY line_number;
-- Any row with error IS NOT NULL = a broken line that was skipped.
```

## 2. Regex and include directives (v16+)

```
# v16+ : a DATABASE/USER token starting with / is a POSIX regex.
host    "/^tenant_\d+$"   all   10.0.0.0/24   scram-sha-256

# v16+ : split rules across files.
include       extra_hba.conf
include_if_exists  local_overrides.conf
```

## 3. Diagnosing "no pg_hba.conf entry for host"

```
-- Server log line :
FATAL:  no pg_hba.conf entry for host "10.0.0.5", user "app", database "shop", no encryption
```

The four reported facts must ALL be matched by one record :

```
# BROKEN : ADDRESS too narrow (10.0.0.5 is outside /28).
host    shop   app   10.0.0.0/28   scram-sha-256

# FIXED  : widen to the real client subnet, then reload.
host    shop   app   10.0.0.0/24   scram-sha-256
```

`no encryption` in the message means the client did not negotiate SSL. A
`hostssl`-only record would never match it; use `host` or fix the client to
use SSL.

## 4. Migrating md5 passwords to scram-sha-256 (v15+)

```sql
-- Step 1 : confirm / set the encryption algorithm.
SHOW password_encryption;                              -- scram-sha-256 default since v14
ALTER SYSTEM SET password_encryption = 'scram-sha-256';
SELECT pg_reload_conf();

-- Step 2 : find roles still on an md5 verifier.
SELECT rolname FROM pg_authid WHERE rolpassword LIKE 'md5%';

-- Step 3 : re-set EACH such role's password (stores a SCRAM verifier now).
ALTER ROLE app PASSWORD 'new-strong-secret';

-- Step 4 : only AFTER every password is re-set, switch HBA lines.
-- pg_hba.conf :  host shop app 10.0.0.0/24 scram-sha-256
SELECT pg_reload_conf();
```

## 5. Connection strings (libpq / psql)

```bash
# Local Unix socket, peer auth (no password)
psql -U app -d shop

# Loopback TCP with password
psql "host=127.0.0.1 port=5432 dbname=shop user=app"

# Production: full TLS verification (encryption + CA + hostname)
psql "host=db.example.com dbname=shop user=app \
      sslmode=verify-full sslrootcert=/etc/ssl/certs/root.crt"

# URI form, full verification
psql "postgresql://app@db.example.com/shop?sslmode=verify-full&sslrootcert=root.crt"

# v17+ : direct TLS, skips the SSL-request round-trip (v17 server, ALPN required)
psql "host=db.example.com dbname=shop user=app sslmode=verify-full sslnegotiation=direct"
```

```bash
# Environment-variable form (no secrets on the command line)
export PGHOST=db.example.com PGDATABASE=shop PGUSER=app
export PGSSLMODE=verify-full PGSSLROOTCERT=/etc/ssl/certs/root.crt
psql
```

## 6. Enabling server-side TLS (v15+)

```
# postgresql.conf  (restart required: these are start-time parameters)
ssl = on
ssl_cert_file = '/etc/postgresql/server.crt'
ssl_key_file  = '/etc/postgresql/server.key'
# ssl_ca_file only needed if you authenticate clients by certificate:
ssl_ca_file   = '/etc/postgresql/root.crt'
```

```bash
# server.key must not be group/world readable or the server refuses to start.
chmod 600 /etc/postgresql/server.key
chown postgres:postgres /etc/postgresql/server.key
```

```
# pg_hba.conf : hostssl forces the connection to be encrypted.
hostssl  shop  app  10.0.0.0/24  scram-sha-256
```

Verify a live session is encrypted :

```sql
SELECT ssl, version, cipher
  FROM pg_stat_ssl
  JOIN pg_stat_activity USING (pid)
 WHERE pid = pg_backend_pid();
```

## 7. Certificate authentication (v15+)

```
# pg_hba.conf : authenticate purely by the client certificate CN.
hostssl  shop  app  10.0.0.0/24  cert  clientcert=verify-full
```

```bash
# Client supplies its certificate + key.
psql "host=db.example.com dbname=shop user=app sslmode=verify-full \
      sslcert=client.crt sslkey=client.key sslrootcert=root.crt"
```

The client certificate CN must equal the requested DB role (or be remapped via
`map=` and `pg_ident.conf`). Server needs `ssl_ca_file` set.

## 8. Triaging "too many clients already" (v15+)

```sql
-- What state are the slots in?
SELECT state, count(*)
  FROM pg_stat_activity
 GROUP BY state
 ORDER BY 2 DESC;

-- Idle connections held open far too long (a leak / missing pooler):
SELECT pid, usename, client_addr, state, state_change
  FROM pg_stat_activity
 WHERE state = 'idle'
   AND state_change < now() - interval '30 minutes'
 ORDER BY state_change;

-- Reclaim them (run as a superuser via a reserved slot):
SELECT pg_terminate_backend(pid)
  FROM pg_stat_activity
 WHERE state = 'idle'
   AND state_change < now() - interval '1 hour';
```

## 9. PgBouncer in transaction mode (the real fix for slot exhaustion)

```ini
; pgbouncer.ini
[databases]
shop = host=10.0.0.2 port=5432 dbname=shop

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432
auth_type = scram-sha-256
auth_file = /etc/pgbouncer/userlist.txt
pool_mode = transaction       ; release the server connection at COMMIT/ROLLBACK
max_client_conn = 2000        ; app-facing connections
default_pool_size = 25        ; actual server connections per database
```

Apps connect to PgBouncer on `:6432`; only `default_pool_size` server slots are
used. Transaction mode is incompatible with session state : session-level
advisory locks, `LISTEN`/`NOTIFY`, and plain `SET` (use `SET LOCAL`).

## 10. Reading SQLSTATE in application code

```python
# psycopg 3
import psycopg
try:
    conn = psycopg.connect("host=db.example.com dbname=shop user=app "
                           "sslmode=verify-full sslrootcert=root.crt")
except psycopg.OperationalError as e:
    code = getattr(e.diag, "sqlstate", None)
    if code == "28P01":
        raise RuntimeError("Bad password or md5/scram verifier mismatch")
    if code == "28000":
        raise RuntimeError("No pg_hba.conf entry for this host/user/database")
    if code == "53300":
        raise RuntimeError("Server connection slots exhausted -- add a pooler")
    raise
```
