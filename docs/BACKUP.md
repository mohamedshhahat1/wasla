# Backup and restore

Until this phase the runbook said, in as many words, that there was no backup
system and that `postgres-data` was a Docker volume. This is the answer to
that.

Two scripts, a scheduled one-shot container, and a restore procedure that has
been executed rather than written down. The distinction matters: a `pg_dump`
invocation nobody has run is not a backup solution, and the drill at the bottom
of this page is what makes the difference.

---

## What is backed up, and what is not

| Data | Where it lives | Covered here |
| --- | --- | --- |
| Everything the product is — workspaces, users, conversations, messages, leads, documents, embeddings, invoices, payments, audit log | PostgreSQL | **Yes** |
| Queued and in-flight jobs, rate-limit counters, the refresh-token denylist, OAuth flow state, worker heartbeats | Redis | **No, deliberately** |
| Customer attachments | `media-data`, a local volume ([ADR-023](../DECISIONS.md)) | **No — see below** |

**Redis is deliberately not backed up.** Everything in it is either
reconstructible or worth losing. A queued job is a message that will be
answered late rather than never — the message itself is in PostgreSQL. A
rate-limit counter resets. A refresh-token denylist that is lost fails *open*
for the remaining lifetime of already-issued refresh tokens, which is the one
entry worth thinking about; the mitigation is that `users.token_version` is in
PostgreSQL and a platform-wide bump revokes every session immediately. Backing
Redis up would create a second copy of a denylist whose whole value is being
current.

**Media is not backed up, and this is a known gap.** Attachments live on one
host's volume. Losing it loses every file customers sent, and no amount of
`pg_dump` changes that — the database holds the metadata and the storage key,
not the bytes. The fix is object storage behind the existing `MediaStorage`
protocol, which is P2 work. Until then, back up the volume with whatever backs
up the host, and know that the recovery point for attachments is whatever that
gives you.

---

## Taking a backup

`scripts/backup_postgres.sh`. One `pg_dump` in the custom format — compressed,
selectively restorable, and carrying the `CREATE EXTENSION` statements that
`vector` and `pgcrypto` need so a restore into an empty database works.

```sh
BACKUP_DIR=/var/backups/wasla \
BACKUP_RETENTION_DAYS=14 \
DATABASE_URL=postgresql+asyncpg://wasla:...@postgres:5432/wasla \
  sh scripts/backup_postgres.sh
```

Connection details come from `DATABASE_URL` — the same variable the application
is configured with, so a deployment does not describe its database twice — or
from the standard `PGHOST`/`PGUSER`/`PGPASSWORD`/`PGDATABASE` variables, which
win if both are set.

Four properties worth knowing:

- **The password never reaches a command line or the output.** It is exported
  into the process environment and nothing echoes it. `ps` on a shared host
  shows the host, the user and the database name, and no secret.
- **A dump is written as `.part` and renamed on success.** A restore pointed at
  the directory can never pick up an artefact that was still being written,
  including one from a run the host killed halfway through.
- **Every dump is read back before it is believed.** `pg_restore --list` parses
  the archive's table of contents; an artefact that fails is deleted and the
  run fails loudly. A truncated dump that looks like a backup is worse than no
  backup.
- **Retention prunes only after a successful dump.** A failed run cannot delete
  the last good backup, which is how one bad night becomes no recovery point.

### Where it runs

`pg_dump` has to match the server's major version, and the version that
certainly does is the one inside `pgvector/pgvector:pg16`. So the backup runs
as a one-shot container from that image, exactly like the `migrate` service:

```sh
docker compose -f docker-compose.prod.yml --profile backup run --rm backup
```

Scheduled from the host, because the host already has a scheduler and adding a
long-running one to this stack would be a new process to watch:

```cron
# /etc/cron.d/wasla-backup
17 2 * * *  root  cd /srv/wasla && docker compose -f docker-compose.prod.yml --profile backup run --rm backup >> /var/log/wasla-backup.log 2>&1
```

Or as a systemd timer, if that is the platform's habit. Either is fine; what
matters is that something outside the stack fires it and that its output is
somewhere an operator will look.

### Storage and encryption

`BACKUP_DIR` maps to a host path. **A backup that only exists on the machine
running the database is not a backup** — it survives a dropped table and not a
dead host, and the second is the failure that ends companies.

Copy dumps somewhere else, and encrypt them there. This repository does not
choose where, because that is a deployment decision and hardcoding a cloud
provider into an operational script is how a script stops fitting the
deployment. What it does state is the requirement:

- Off-host, on a schedule at least as often as the backup itself.
- Encrypted at rest — an encrypted bucket (S3 SSE-KMS, GCS CMEK, R2), an
  encrypted volume, or platform-managed disk encryption.
- Access separated from the application's own credentials, so a compromised API
  container cannot read or delete the backups.

Nothing here invents its own encryption. A dump is a file; the platform that
stores files is what encrypts it.

### Retention

`BACKUP_RETENTION_DAYS`, default 14. Dumps older than that are removed after a
successful run, matched by this database's own filename pattern so a shared
directory is left alone. Fourteen days is a starting point, not a policy: it
covers "somebody noticed on Monday what broke a week ago" and it is not an
archival tier. If a retention obligation exists for billing or audit records,
that is a legal question and it belongs in a decision, not in a default.

---

## Restoring

`scripts/restore_postgres.sh`, and the target database is always named:

```sh
sh scripts/restore_postgres.sh /var/backups/wasla/wasla-20260902T085633Z.dump wasla_restored --clean
```

There is deliberately **no "restore into the configured database" path**. The
one thing a restore script must never do is the destructive thing by accident,
so restoring over the database `DATABASE_URL` names requires
`WASLA_RESTORE_ALLOW_PRODUCTION=yes` — an opt-in an operator has to type, which
cannot be reached by leaving an argument off.

What it does, in order:

1. Refuses the configured production database unless explicitly permitted.
2. Creates the target, or refuses to touch an existing one without `--clean`.
3. `pg_restore --exit-on-error`, so a partially restored database is never
   reported as a success. Without that flag `pg_restore` reports errors and
   carries on, and the shape of *that* failure is a database that looks
   restored and is missing a table.
4. **Verifies.** This is the part that makes it a procedure rather than an
   invocation:
   - the schema has tables;
   - `vector` and `pgcrypto` came back, so embeddings are usable;
   - `alembic_version` is populated, and matches `WASLA_EXPECTED_HEAD` if set;
   - representative rows can be counted.

A restore that produces a database the application cannot query is not a
recovery. The verification step is what says so out loud.

### After a restore

```sh
# The dump carries the schema at the moment it was taken. If the code has moved
# on since, bring the restored database up to it before serving from it.
DATABASE_URL=postgresql+asyncpg://wasla:...@postgres:5432/wasla_restored alembic upgrade head

# Then check what the application checks.
curl -fsS http://localhost:8000/health/ready
```

Redis is *not* restored, and after a recovery it is empty. That means queued
work is gone: messages that arrived and were not yet answered are in
PostgreSQL as `messages` rows with no reply, and re-driving them means
enqueueing agent jobs for the affected conversations. Nothing does that
automatically, and it should not — an operator has to decide whether answering
a day-old question is better than silence.

---

## The drill

Run against PostgreSQL 16 with pgvector, on 2026-09-02, before any of this was
committed. Synthetic data only — no production or customer records were
touched at any point.

```
source database          wasla_drill, migrated to head 0037
representative rows      2 tenants, 2 users, 2 memberships,
                         1 knowledge base, 1 document,
                         1 document_chunk with a real 1536-dimension embedding

backup                   scripts/backup_postgres.sh
                         -> wasla_drill-20260902T095931Z.dump (149,666 bytes)
                         -> retention: kept 14 days, removed 0

fresh target             wasla_restored (created by the script, did not exist)
restore                  scripts/restore_postgres.sh <dump> wasla_restored --clean

verification (the script's own)
  schema                 38 tables
  extensions             pgcrypto, vector
  migration head         0037   (matched WASLA_EXPECTED_HEAD=0037)
  rows                   2 tenants, 2 users

verification (through the application)
  SQLAlchemy read the restored database with the real models:
  tenants  [('drill-alpha', TenantStatus.ACTIVE), ('drill-beta', TenantStatus.ACTIVE)]
  users    2
  chunk    99999999-...-999999999999, ordinal 0
  embedding 1536 dimensions, [0.1429, 0.2857, 0.4286, 0.5714, ...]
```

The last block is the one that matters. `psql` proving rows exist says the
bytes came back; the ORM reading them through `Tenant`, `User` and
`DocumentChunk` — enums mapped, `vector(1536)` mapped — says the *application*
can use what came back.

### The failure cases, also executed

| Probe | Result |
| --- | --- |
| Backup with a user that does not exist | exit 1, `pg_dump exited non-zero; no artefact was kept`, no `.part` left behind |
| Backup with a password in `DATABASE_URL` | the password appears in neither stdout nor stderr |
| Restore from a truncated dump | exit 1 |
| Restore from a file that is not a dump at all | exit 1 |
| Restore over the configured database | refused |
| Restore over the configured database with `WASLA_RESTORE_ALLOW_PRODUCTION=yes` | permitted, with a warning line |
| Restore onto an existing database without `--clean` | refused |
| Restore whose head does not match `WASLA_EXPECTED_HEAD` | exit 1, naming both heads |

---

## What this does not give you

Stated plainly, because a backup page that implies more than it delivers is how
somebody discovers the gap during an incident:

- **A recovery point objective is a schedule, not a script.** Nightly backups
  mean up to 24 hours of lost writes. If that is too much, the answer is
  continuous archiving (`archive_command` plus base backups, or a managed
  provider's point-in-time recovery), not a more frequent `pg_dump`.
- **No off-host copy is configured here.** The script writes to a directory.
  Getting that directory somewhere else is the deployment's job and is not
  automated by this repository.
- **This has never run against production**, because there is no production.
  Every command on this page has been executed against local containers with
  synthetic data.
- **Media is not covered.** See the top of this page.
