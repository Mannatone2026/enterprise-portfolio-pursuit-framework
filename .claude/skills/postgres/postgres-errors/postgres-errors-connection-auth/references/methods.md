# Reference : Connection + Authentication API Surface

Verified against PostgreSQL 17 official docs (auth-pg-hba-conf, errcodes-appendix,
libpq-ssl, runtime-config-connection). Last verified : 2026-05-19.

## pg_hba.conf record format

A record is one line. Fields are whitespace-separated. Records are scanned
top-down and the **first match wins** on connection type + client address +
database + user. There is no fall-through and no backup record.

```
TYPE  DATABASE  USER  ADDRESS  METHOD  [OPTIONS]
```

For an IP record with a separate netmask column :

```
TYPE  DATABASE  USER  IP-ADDRESS  IP-MASK  METHOD  [OPTIONS]
```

### Connection type (TYPE) keywords

| Keyword | Matches |
|---------|---------|
| `local` | Unix-domain socket connections (no ADDRESS field) |
| `host` | TCP/IP connections, SSL or non-SSL, GSSAPI-encrypted or not |
| `hostssl` | TCP/IP connections that are SSL-encrypted only |
| `hostnossl` | TCP/IP connections that are NOT SSL-encrypted |
| `hostgssenc` | TCP/IP connections with GSSAPI encryption only |
| `hostnogssenc` | TCP/IP connections without GSSAPI encryption |

### DATABASE / USER fields

- `all` : matches any database / any role
- a literal name : matches exactly that database / role
- `sameuser` : the requested database has the same name as the requesting role
- `samerole` : the requesting role is a member of a role named like the database
- `replication` (DATABASE) : matches physical replication connections only
- `+rolename` : matches roles that are members of `rolename`
- `/regex` : a string starting with `/` is a POSIX regular expression (v16+)
- `@filename` : include names listed in an external file
- Multiple names may be comma-separated.

### ADDRESS field (host / hostssl / hostnossl / hostgss... only)

- CIDR notation : `10.0.0.0/24`, `192.168.12.10/32`, `::1/128`
- IP plus separate mask column : `10.0.0.0  255.255.255.0`
- `all` : any IP address
- `samehost` / `samenet` : the server's own addresses / subnets
- A host name or domain suffix : `localhost`, `.example.com`

## Authentication methods (METHOD)

| Method | Use | Notes |
|--------|-----|-------|
| `trust` | Unix socket on a tightly controlled host ONLY | No auth at all. NEVER on TCP. |
| `reject` | Hard-block a connection path | Connection refused unconditionally. |
| `scram-sha-256` | Default password auth (preferred since v10) | Salted challenge-response. |
| `md5` | Legacy password auth | An `md5` line also accepts a SCRAM verifier; avoid for new setups. |
| `password` | Cleartext password over the wire | NEVER without TLS. |
| `peer` | `local` connections | Matches the client OS user to the DB role name. |
| `ident` | `host` connections | OS user via an ident server. Spoofable; legacy. |
| `cert` | `hostssl` connections | Authenticates via the SSL client certificate CN. |
| `gss` / `sspi` | Kerberos / Windows SSO | Enterprise environments. |
| `ldap` / `radius` / `pam` / `bsd` | External directory / service auth | |

Optional METHOD options follow as `key=value` (e.g. `map=mapname`,
`clientcert=verify-full`, `ldapserver=...`).

## Reloading pg_hba.conf

The file is read at server start and on every `SIGHUP`. Editing it does nothing
until reloaded. A reload never drops existing sessions.

```sql
SELECT pg_reload_conf();          -- SQL, requires a connected superuser-ish role
```

```bash
pg_ctl reload -D /var/lib/postgresql/data   -- shell
kill -HUP <postmaster_pid>                  -- equivalent signal
```

Windows exception : changes to `pg_hba.conf` are applied to subsequent new
connections immediately, without a reload.

## Diagnostic views and functions

| Object | Purpose |
|--------|---------|
| `pg_hba_file_rules` | Parsed view of `pg_hba.conf`. A non-null `error` column flags a bad line. Pre-test edits here before reloading. |
| `pg_stat_activity` | One row per backend : `pid`, `state`, `usename`, `client_addr`, `state_change`. Triage slot exhaustion. |
| `pg_authid` | Role catalog. `rolpassword` shows the stored verifier; `md5` prefix = md5 verifier, `SCRAM-SHA-256$` prefix = SCRAM verifier. |
| `pg_terminate_backend(pid)` | Force-close a backend, freeing its connection slot. |
| `pg_cancel_backend(pid)` | Cancel the current query without dropping the connection. |
| `current_setting('max_connections')` | Read an active connection parameter. |

## password_encryption

| Aspect | Value |
|--------|-------|
| Parameter | `password_encryption` |
| Allowed values | `scram-sha-256`, `md5` |
| Default | `scram-sha-256` (since PostgreSQL 14) |
| Scope | Controls how the NEXT `CREATE ROLE` / `ALTER ROLE ... PASSWORD` stores the verifier. Existing verifiers are unchanged. |
| Reload | `SIGHUP` (no restart) |

## SSL : client sslmode (libpq)

| sslmode | Encryption | Server identity verified | Notes |
|---------|-----------|--------------------------|-------|
| `disable` | No | No | Plaintext. |
| `allow` | Maybe | No | Encrypt only if the server insists. |
| `prefer` | Maybe | No | libpq default. Encrypt if available. Kept for backward compat only. |
| `require` | Yes | **No** | Encrypted but MITM-vulnerable: the server cert is NOT checked. |
| `verify-ca` | Yes | CA chain only | Cert must chain to a trusted CA in `root.crt`. |
| `verify-full` | Yes | **CA + hostname** | Production default. Analogous to browser `https://`. |

Related client parameters : `sslrootcert` (trusted CA file, default
`~/.postgresql/root.crt`), `sslcert` / `sslkey` (client certificate + key for
`cert` auth), `sslnegotiation=direct` (v17+ : skip the libpq SSL-request
round-trip, requires a v17 server with ALPN).

## SSL : server parameters (postgresql.conf)

| Parameter | Default | Scope | Purpose |
|-----------|---------|-------|---------|
| `ssl` | `off` | restart | Enable SSL/TLS listening. |
| `ssl_cert_file` | `server.crt` | restart | Server certificate. |
| `ssl_key_file` | `server.key` | restart | Server private key (must be unreadable by others). |
| `ssl_ca_file` | empty | restart | CA file used to verify CLIENT certificates (`cert` auth). |

## Connection-slot parameters (postgresql.conf)

| Parameter | Default | Scope | Purpose |
|-----------|---------|-------|---------|
| `max_connections` | 100 | restart | Hard cap on concurrent backends. Sizes shared memory. |
| `superuser_reserved_connections` | 3 | restart | Slots kept for superusers when the server is otherwise full. |
| `reserved_connections` | 0 | restart | v16+. Slots kept for roles with `pg_use_reserved_connections`. |
| `listen_addresses` | `localhost` | restart | Network interfaces the server binds. Not auth, but a frequent "cannot connect" cause. |
| `port` | 5432 | restart | TCP port. |

A standby's `max_connections` must be >= the primary's.

## SQLSTATE codes for this domain

| Code | Condition name | Class | Meaning |
|------|----------------|-------|---------|
| `28000` | `invalid_authorization_specification` | 28 Invalid Authorization Specification | No matching `pg_hba.conf` record / invalid role spec. |
| `28P01` | `invalid_password` | 28 Invalid Authorization Specification | Password authentication failed. |
| `53300` | `too_many_connections` | 53 Insufficient Resources | `max_connections` reached. |
| `53400` | `configuration_limit_exceeded` | 53 Insufficient Resources | A configured limit (other than connections) exceeded. |

## Typical FATAL messages

```
FATAL:  no pg_hba.conf entry for host "10.0.0.5", user "app", database "shop", no encryption
FATAL:  pg_hba.conf rejects connection for host "...", user "...", database "..."
FATAL:  password authentication failed for user "app"
FATAL:  role "app" does not exist
FATAL:  database "shop" does not exist
FATAL:  sorry, too many clients already
FATAL:  remaining connection slots are reserved for roles with the SUPERUSER attribute
psql: error: connection to server ... failed: server does not support SSL, but SSL was required
```
