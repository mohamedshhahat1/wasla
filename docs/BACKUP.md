# Backup and restore

Until this phase the runbook said, in as many words, that there was no backup
system and that `postgres-data` was a Docker volume. This is the answer to
that.

Four scripts, a systemd timer, an off-host destination, and a restore that has
been executed from that destination *after the local copy was destroyed*. Two
distinctions run through the whole page and everything else follows from them:

**A dump is not a backup.** A validated dump next to the database it came from
survives a dropped table and does not survive the host. So a run is not a
success until the artifact has reached somewhere else and been read back there.

**A script is not a procedure.** A `pg_dump` invocation nobody has run proves
nothing, and neither does an upload adapter nobody has downloaded from. The
drill at the bottom of this page removes the local copy before restoring, so
what it proves is the remote copy.

---

## What is backed up, and what is not

| Data | Where it lives | Covered here |
| --- | --- | --- |
| Everything the product is — workspaces, users, conversations, messages, leads, documents, embeddings, invoices, payments, audit log | PostgreSQL | **Yes** |
| Queued and in-flight jobs, rate-limit counters, the refresh-token denylist, OAuth flow state, worker heartbeats | Redis | **No, deliberately** |
| Customer attachments | The media store: a local volume, or an object bucket ([ADR-077](../DECISIONS.md)) | **No, and it is not meant to — see below** |

**Redis is deliberately not backed up.** Everything in it is either
reconstructible or worth losing. A queued job is a message that will be
answered late rather than never — the message itself is in PostgreSQL. A
rate-limit counter resets. A refresh-token denylist that is lost fails *open*
for the remaining lifetime of already-issued refresh tokens, which is the one
entry worth thinking about; the mitigation is that `users.token_version` is in
PostgreSQL and a platform-wide bump revokes every session immediately. Backing
Redis up would create a second copy of a denylist whose whole value is being
current.

**Media durability has a separate owner, and this backup is not it.** A
`pg_dump` carries the `message_media` rows — the transcript, the type, the size,
the storage key — and none of the bytes those keys point at. That is not an
oversight to be corrected by widening the dump. Putting a workspace's video
attachments inside a database backup would make every restore carry them, would
grow the one artifact whose restore time is the recovery time, and would give
two systems the same job.

So the two halves are owned separately, and a disaster recovery needs both:

| | Owner | What it restores |
| --- | --- | --- |
| Metadata and references | This backup, verified off-host ([ADR-075](../DECISIONS.md)) | Which files existed, what they said, who they belong to |
| The bytes | The object store's own durability, versioning and replication | The files themselves |

**With `MEDIA_STORAGE_BACKEND=local` there is no second owner.** The volume is
the only copy, losing the host loses every attachment, and the database restore
comes back with rows whose keys resolve to nothing. Back the volume up with
whatever backs up the host and know that the recovery point for attachments is
whatever that gives you — or set `s3`, which is the sentence this exists to stop
being true ([MEDIA.md](MEDIA.md)).

**With `s3`, durability is the store's** — versioning, replication and a
lifecycle rule configured on the bucket, which outlive this host and do not
depend on a script here running. Wasla does not copy objects into its own
backup, because that would be a second, worse copy of something the provider
already replicates.

**A restore that finds missing objects still works.** Rows whose keys resolve to
nothing report a storage error per file rather than failing the application, so
the product serves while the store is being restored, and a colleague opening
one attachment sees an error about that attachment. What must not happen is
restoring a database from one date against a bucket whose lifecycle rule has
already expired the objects it references — check the bucket's retention against
this backup's retention before assuming both halves cover the same window.

Verified by drill: `docs/RUNBOOK.md` describes the media recovery check, which
stores an object, records its metadata, discards the runtime, and reads the same
bytes and the same canonical type back from a fresh process against the same
off-host store.

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

`pg_dump` has to match the server's major version, and getting the dump off the
host needs a client the database image does not carry. `Dockerfile.backup` is
the one image with both: the `pgvector/pgvector:pg16` base for `pg_dump`, plus
`awscli` for the upload. Adding `postgresql-client` to the *application* image
instead would put it on every container serving traffic for the benefit of a
process that runs once a day.

```sh
docker compose -f docker-compose.prod.yml --profile backup run --rm backup
```

### The schedule

`deploy/systemd/wasla-backup.service` and `.timer`, shipped in the repository
rather than described in prose:

```sh
sudo cp deploy/systemd/wasla-backup.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wasla-backup.timer
```

Daily at 02:17 with a ten-minute jitter. Two properties are worth naming:

- **`Persistent=true`.** A host that was down at 02:17 runs the backup as soon
  as it is back rather than skipping the day. Without it a weekend of downtime
  silently doubles the recovery window and nobody finds out until the restore.
- **`Type=oneshot` with no `Restart=`.** A failed backup should be noticed, not
  retried in a loop against an object store that is refusing us.
  `systemctl is-failed wasla-backup.service` is then a real signal, and
  `wasla_backup_age_seconds` is the other one.

Verify it without waiting a day:

```sh
sudo systemctl start wasla-backup.service
journalctl -u wasla-backup.service -n 50
systemctl list-timers wasla-backup.timer
```

A cron entry works equally well if that is the platform's habit; what matters
is that something outside the stack fires it and that its output is somewhere
an operator will look.

### Storage and encryption

`BACKUP_DIR` is **staging**, not the backup. The backup is what
`scripts/upload_backup.sh` puts somewhere else, and the run fails if it cannot.

### The destination

One backend, `s3`, which is not one provider. `aws s3` with
`BACKUP_S3_ENDPOINT_URL` speaks to AWS, MinIO, Cloudflare R2, Wasabi, Backblaze
B2 and Ceph alike, so a single implementation covers every object store a
deployment is likely to choose without this repository choosing one.

```sh
BACKUP_DESTINATION=s3
BACKUP_S3_BUCKET=wasla-backups
BACKUP_S3_PREFIX=wasla
BACKUP_S3_ENDPOINT_URL=      # empty for AWS; set it for anything else
BACKUP_S3_SSE=AES256         # REQUIRED: encryption at rest, AES256 or aws:kms
BACKUP_S3_SSE_KMS_KEY_ID=    # only for aws:kms, and optional even then
BACKUP_S3_ACCESS_KEY_ID=…    # the backup container's, and nothing else's
BACKUP_S3_SECRET_ACCESS_KEY=…
```

A deployment that needs something else entirely — a second datacentre over
rsync, a tape robot — **replaces `scripts/upload_backup.sh`**. That is a file
boundary rather than a `BACKUP_UPLOAD_COMMAND` string something would have to
`eval`, and the difference is that nothing here ever hands attacker-influenced
text to a shell.

`BACKUP_DESTINATION=none` is **refused**, because a run that reports success
having left the dump on the host is worse than one that fails.
`BACKUP_ALLOW_LOCAL_ONLY=yes` overrides that for a development machine;
`docker-compose.prod.yml` never sets it, and a test asserts as much.

### What is required of the destination

- **Off this host.** Not another directory, not another volume on the same
  disk. The failure being survived is the machine.
- **Encrypted in transit.** `aws s3` is HTTPS unless an endpoint URL says
  otherwise; do not point it at an `http://` endpoint outside a private network.
- **Encrypted at rest, and this is not optional.** `BACKUP_S3_SSE` must be
  `AES256` or `aws:kms`. The uploader refuses to send a dump without it and
  refuses to record a success unless the store confirms, on the object itself,
  that it is encrypted (ADR-090). Nothing here invents its own cryptography — a
  dump is a file, and the platform storing files encrypts it.

  **Why explicit SSE rather than a bucket rule.** A bucket default is a
  reasonable way to run a bucket and a poor thing to *verify*.
  `GetBucketEncryption` is not implemented by every S3-compatible store this
  script is meant to serve, it needs a permission the backup credential does not
  otherwise want, and it describes the bucket's policy rather than the object
  that was just written. Asked against a MinIO with a KMS configured and no
  explicit bucket rule, it answers
  `ServerSideEncryptionConfigurationNotFoundError` — a store that *can* encrypt
  and *does* reporting that it has no configuration. So the contract is the
  smaller and checkable one: ask for encryption on the request, and read it back
  off the object.

  **What is verified, and what is not.** The `head-object` that already checks
  the size now also reads `ServerSideEncryption` and requires it to equal what
  was asked for. The KMS key id is *not* compared: the store answers with a full
  ARN whatever an operator configured — a bare id, an alias — so a comparison
  would fail on correct configurations and prove nothing about wrong ones. That
  the object is encrypted under KMS is what this can check, and it is the
  property that matters here.

  **Not every store can honour it.** MinIO needs a KMS (`MINIO_KMS_SECRET_KEY`
  or KES) and answers `NotImplemented` otherwise. That fails the run, which is
  the intended behaviour: a store that cannot encrypt is not a backup
  destination, and the alternative is a green run over a plaintext copy of the
  whole database.
- **Credentials the application does not hold.** Only the `backup` service is
  given `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`. This is the whole reason
  backups run in their own container: a compromised API must not be able to
  delete the database *and* every copy of it. There is a test.
- **Versioning or object-lock, if the store offers it.** Then a credential
  compromise cannot destroy history either. Not enforced here because not every
  store supports it.

### Verification

`cp` exiting zero says the client believed it finished. `upload_backup.sh` then
asks the store what it actually holds and compares the size — the cheapest
check that tells "uploaded" from "uploaded a zero-byte file because the pipe
broke". Without it a truncated remote copy is indistinguishable from a good one
until the day somebody needs it.

### Retention

Two retentions, with different owners, and conflating them is how a recovery
fails.

**Local staging** is `BACKUP_RETENTION_DAYS`, default 14, pruned by this script
after the upload has succeeded — never before, so a failed run cannot delete
the last good artifact it still has.

**Off-host** is the object store's lifecycle policy, and it should stay there.
A rule in the bucket outlives this host, survives a bug in a shell script, and
cannot be undone by whoever gets the application credentials. If this script
deleted remote objects it would need delete permission on the bucket, which is
exactly the permission a backup uploader should not have.

Fourteen days is a starting point, not a policy. If a retention obligation
exists for billing or audit records, that is a legal question and belongs in a
decision rather than in a default.

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

## The disaster drill

**The one that matters.** The earlier drill proved a dump could be restored;
this one proves the *remote* copy can, because the local copy was destroyed
first. Run on 2026-09-02 against PostgreSQL 16 with pgvector and a MinIO
container standing in for the object store. Synthetic data only — no production
or customer records were touched at any point.

```
source database          wasla_drill, migrated to head 0037
representative rows      2 tenants, 2 users, 2 memberships,
                         1 knowledge base, 1 document,
                         1 document_chunk with a real 1536-dimension embedding

off-host destination     MinIO, bucket wasla-drill-backups, prefix wasla/
                         reached over the S3 API by scripts/upload_backup.sh

backup                   scripts/backup_postgres.sh, in the wasla-backup image
  dump                   staged /backups/wasla_drill-20260902T123236Z.dump
                                (149,668 bytes)
  upload                 uploading to s3://wasla-drill-backups/wasla/
  verify at destination  verified wasla/wasla_drill-20260902T123236Z.dump
                                (149,668 bytes) at the destination
  retention              kept 14 days of local staging, removed 0
  status                 done: wasla_drill is backed up to s3

status file written
  {"outcome": "success",
   "last_success_at": "2026-09-02T12:32:40Z",
   "last_success_artifact": "wasla_drill-20260902T123236Z.dump",
   "last_success_bytes": 149668,
   "destination": "s3",
   "failures_total": 0,
   "failed_stage": ""}

HOST LOSS SIMULATED     docker volume rm -f wasla-drill-staging
                        -> staging volume destroyed
                        -> confirmed: no local copy remains anywhere

recovery host           a new, empty volume: `ls` shows . and .. and nothing else

fetch                   scripts/fetch_backup.sh /recovered
  discovery             newest artifact is wasla_drill-20260902T123236Z.dump
  validation            verified wasla_drill-20260902T123236Z.dump is a readable dump
  result                wrote /recovered/wasla_drill-20260902T123236Z.dump
                              (149,668 bytes)

fresh target            wasla_from_offhost (created by the script, did not exist)
restore                 scripts/restore_postgres.sh /recovered/<artifact> \
                                                    wasla_from_offhost --clean
  schema                38 tables
  extensions            pgcrypto, vector
  migration head        0037   (matched WASLA_EXPECTED_HEAD=0037)
  rows                  2 tenants, 2 users
  result                wasla_from_offhost is restored and verified

verification through the application
  SQLAlchemy read the recovered database with the real models:
  tenants   [('drill-alpha', TenantStatus.ACTIVE), ('drill-beta', TenantStatus.ACTIVE)]
  users     2
  chunk     99999999-9999-9999-9999-999999999999, ordinal 0
  embedding 1536 dimensions, [0.1429, 0.2857, 0.4286, 0.5714, ...]
```

Three things this establishes that the earlier drill did not:

1. **The bytes came from the object store**, not from a file that happened to
   still be on disk. The staging volume was removed and the recovery volume
   started empty — `ls` was run to prove it.
2. **The restore script travels in the backup image.** On the day somebody
   needs it, the application image may not build and the repository may not be
   reachable; what certainly exists is whatever was pulled to run last night's
   backup. The drill found this missing and it was fixed before this was
   written down.
3. **The ORM reads it.** `psql` proving rows exist says the bytes came back;
   `Tenant`, `User` and `DocumentChunk` reading them — enums mapped,
   `vector(1536)` mapped — says the *application* can use what came back.

### The failure cases, also executed

| Probe | Result |
| --- | --- |
| Backup with a user that does not exist | exit 1, `pg_dump exited non-zero; no artefact was kept`, no `.part` left behind |
| Backup with a password in `DATABASE_URL` | the password appears in neither stdout nor stderr |
| Dump succeeds, upload fails | exit 1, status `failure` at stage `upload`, `last_success_at` **unchanged**, local artifact kept |
| No `BACKUP_DESTINATION` | refused: "a dump on the same host as its database is not a backup" |
| `BACKUP_DESTINATION=s3` with no `BACKUP_S3_SSE` | refused before anything is uploaded; status `failure` at stage `upload`, `last_success_at` **unchanged** |
| `BACKUP_S3_SSE` set to something the S3 API does not define | refused, naming the two values that are |
| Store rejects the encryption request (MinIO with no KMS) | exit 1, `NotImplemented` from the store, **nothing left in the bucket**, `last_success_at` unchanged |
| Store accepts the upload but reports no encryption | refused at verification: "reports encryption 'None', not 'AES256'" |
| Remote copy truncated | refused: sizes compared and reported |
| Store does not hold the object | refused |
| Restore from a truncated dump | exit 1 |
| Restore from a file that is not a dump at all | exit 1 |
| Restore over the configured database | refused |
| Restore over the configured database with `WASLA_RESTORE_ALLOW_PRODUCTION=yes` | permitted, with a warning line |
| Restore onto an existing database without `--clean` | refused |
| Restore whose head does not match `WASLA_EXPECTED_HEAD` | exit 1, naming both heads |

---

## Repeating it

Do **not** restore production backups automatically. A scheduled restore that
writes to anything real is a scheduled outage waiting for a bad argument, and
one that writes to a scratch database still costs a database's worth of disk
and IO on whatever host runs it.

Instead, run this by hand on a cadence somebody has agreed to — quarterly is a
reasonable starting point, and after any change to the schema, the dump format
or the destination:

```sh
# 1. Bring the newest off-host artifact to an isolated machine.
sh scripts/fetch_backup.sh /tmp/drill

# 2. Restore into a scratch database that is not production.
WASLA_EXPECTED_HEAD=$(alembic heads | awk '{print $1}') \
  sh scripts/restore_postgres.sh /tmp/drill/<artifact> wasla_drill_$(date +%Y%m%d) --clean

# 3. Read it through the application, not only through psql.
DATABASE_URL=postgresql+asyncpg://…/wasla_drill_$(date +%Y%m%d) \
  python -c "…"   # the block in the drill above

# 4. Destroy the scratch database and write down the date it passed.
```

Step 3 is the one people skip and the one that catches a schema the ORM can no
longer map. Step 4 matters too: a drill whose result nobody recorded is a drill
somebody will argue about.

---

## Recovery objectives

Stated as two separate things, because conflating them is how a number nobody
can meet ends up in a contract.

**What the schedule implies today** — observed facts, not promises:

| | |
| --- | --- |
| Backup frequency | daily at 02:17, jittered, `Persistent=true` |
| Implied worst-case data loss | **~24 hours** of writes |
| Observed restore duration | ~4 minutes for a 150 KB dump on a laptop, of which ~3½ was `pg_restore` |
| What that says about a real database | very little — it scales with data volume and has never been measured against one |

**What has not been adopted:** no RPO or RTO has been agreed by anybody. The
numbers above are what the current configuration produces, not targets it is
held to. Adopting an RPO shorter than a day means continuous archiving
(`archive_command` plus base backups, or a managed provider's point-in-time
recovery), which is a different mechanism rather than a more frequent
`pg_dump`. Adopting an RTO means measuring a restore at production scale, which
nobody has done because there is no production.

---

## What this does not give you

Stated plainly, because a backup page that implies more than it delivers is how
somebody discovers the gap during an incident:

- **No agreed RPO or RTO.** See above: what the schedule implies is written
  down; what anybody has committed to is nothing.
- **No off-host destination is configured by default.** The mechanism ships and
  is proved; choosing a bucket, a region and a lifecycle policy is the
  deployment's, and until that happens `BACKUP_DESTINATION=none` refuses to
  report success.
- **The destination has never been a real cloud.** The drill ran against MinIO
  over the S3 API on a private network. That exercises the same client and the
  same protocol, and it does not exercise a real provider's IAM, its TLS chain
  or its rate limits.
- **This has never run against production**, because there is no production.
  Every command on this page has been executed against local containers with
  synthetic data.
- **Media is not covered.** See the top of this page.
- **Redis is not covered**, deliberately. See the top of this page.
