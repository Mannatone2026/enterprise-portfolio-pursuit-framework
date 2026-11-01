# Reference : Connection + Authentication Anti-Patterns

Each entry : CAUSE, SYMPTOM, FIX. SQLSTATE given where the failure surfaces one.
Verified against PostgreSQL 17 docs. Last verified : 2026-05-19.

## AP-1 : `trust` auth on a TCP / production host

CAUSE : a `host`/`hostssl` record uses method `trust`, often copied from the
default `local all all trust` line or pasted to "make connecting easy".

```
# DANGEROUS
host   all   all   0.0.0.0/0   trust
```

SYMPTOM : anyone who can reach port 5432 connects as any role, including
`postgres`, with no password. No error is raised; that is the problem. The
breach is silent until data is gone.

FIX : `trust` belongs only on a tightly controlled `local` Unix socket, never
on TCP. Use `scram-sha-256` for password login or `cert` for machine auth.
Restrict the ADDRESS to a real subnet. Reload and audit `pg_hba_file_rules`.

```
host   all   all   10.0.0.0/24   scram-sha-256
```

## AP-2 : `md5` instead of `scram-sha-256` in 2026

CAUSE : HBA lines left on `md5` from an old install, or `password_encryption`
left at `md5`.

SYMPTOM : passwords are stored as weak unsalted-ish md5 verifiers. md5 auth is
downgrade-prone and the verifier is the same for every connection. No error;
just a weaker security posture.

FIX : set `password_encryption = 'scram-sha-256'`, re-set every role's password
so a SCRAM verifier is stored, then switch HBA lines to `scram-sha-256`. See
AP-6 for the ordering trap. SCRAM has been the default since v14.

## AP-3 : `sslmode=require` sold as a secure connection

CAUSE : a client connection string uses `sslmode=require` and the team assumes
"SSL is on, so we are safe".

SYMPTOM : traffic is encrypted but the server certificate is never validated. A
man-in-the-middle that terminates TLS and re-originates it satisfies `require`
perfectly. There is no error and no warning. (If a `root.crt` happens to exist,
libpq silently behaves like `verify-ca` for backward compatibility, which makes
the behaviour inconsistent across machines.)

FIX : use `sslmode=verify-full` on any untrusted network. It validates the CA
chain AND the hostname. Ship `sslrootcert` with the trusted CA. Treat `require`
as "encryption only, identity unknown".

## AP-4 : editing pg_hba.conf and forgetting to reload

CAUSE : `pg_hba.conf` is edited and saved, but no reload / `SIGHUP` is sent.

SYMPTOM : the change has zero effect. A newly added record still produces
`FATAL: no pg_hba.conf entry ...` (SQLSTATE `28000`); a removed `reject` still
blocks. The file on disk and the running ruleset disagree, which sends
debugging down the wrong path.

FIX : after every edit run `SELECT pg_reload_conf();` (or `pg_ctl reload`). The
file is only re-read at start and on `SIGHUP`. Then confirm with
`SELECT * FROM pg_hba_file_rules` and check that no row has a non-null `error`
(a syntax error makes the server skip the bad line silently).

## AP-5 : raising max_connections to thousands instead of pooling

CAUSE : `too many clients already` appears, so `max_connections` is bumped to
2000+ to "make it go away".

SYMPTOM : SQLSTATE `53300`. Each slot reserves backend memory and enlarges the
lock and proc tables; thousands of mostly-idle backends degrade throughput for
everyone and can exhaust RAM. The error eventually returns under load.

FIX : keep `max_connections` in the 100-300 band. Put PgBouncer in transaction
mode in front: hundreds of app clients multiplex onto a small fixed server
pool. Also hunt the real cause (a connection leak: many `idle` rows in
`pg_stat_activity` with old `state_change`).

## AP-6 : switching HBA to scram-sha-256 before re-setting passwords

CAUSE : HBA lines are flipped from `md5` to `scram-sha-256`, but the roles'
stored verifiers are still md5.

SYMPTOM : `FATAL: password authentication failed for user "..."` (SQLSTATE
`28P01`) for a password the user swears is correct. A `scram-sha-256` line
cannot authenticate a role whose stored verifier is md5.

FIX : ordering matters. (1) `ALTER SYSTEM SET password_encryption =
'scram-sha-256'` + reload. (2) `ALTER ROLE ... PASSWORD '...'` for every role to
store a SCRAM verifier. (3) only then switch HBA lines. Check leftovers with
`SELECT rolname FROM pg_authid WHERE rolpassword LIKE 'md5%'`.

## AP-7 : widening a CIDR to 0.0.0.0/0 to clear "no pg_hba.conf entry"

CAUSE : `FATAL: no pg_hba.conf entry for host ...` (SQLSTATE `28000`) is met by
broadening the ADDRESS to `0.0.0.0/0` rather than reading the four reported
facts (host, user, database, encryption).

SYMPTOM : the immediate error disappears but the database is now reachable from
every IP on earth. The actual cause (a too-narrow subnet, a `host` vs
`hostssl`/`no encryption` mismatch, or wrong database/user) is masked.

FIX : read the error. Add a record matching exactly the real client subnet,
database, and user. If the message says `no encryption`, the client is not on
SSL, so a `hostssl` rule will never match: either use `host` or fix the client.

## AP-8 : a `reject` record sitting above the rule that should match

CAUSE : because the first match wins, a broad `reject` line placed above a
specific allow line shadows it.

```
host   all   all   10.0.0.0/8   reject          # matches first ...
host   shop  app   10.0.0.5/32  scram-sha-256   # ... so this never runs
```

SYMPTOM : `FATAL: pg_hba.conf rejects connection for host "..."` even though a
correct allow record clearly exists further down.

FIX : order records specific-to-general. Put narrow allow rules above broad
`reject` rules. Verify the effective order in `pg_hba_file_rules` by
`line_number`.

## AP-9 : confusing "Connection refused" with an auth failure

CAUSE : a TCP-level `could not connect ... Connection refused` is treated as a
`pg_hba.conf` problem.

SYMPTOM : no `FATAL` line in the server log at all, because the connection
never reached the auth stage. Usually `listen_addresses = 'localhost'`, a wrong
port, a stopped server, or a firewall.

FIX : this is not an auth error. Confirm the server is up and `listen_addresses`
includes the client-facing interface, the `port` is correct, and the firewall
allows it. Only once the connection reaches the server do `pg_hba.conf` and
`28000`/`28P01` become relevant.
