# FOLLOW-UPS, HEADER MEDIA & REALTIME — CANONICAL MERGE RE-RUN

**Verdict: FOLLOW-UPS, HEADER MEDIA & REALTIME MERGED — push and the first GitHub Actions run are recorded in the commit that follows this report**

Supersedes `FOLLOW_UPS_HEADER_MEDIA_REALTIME_MERGE_VERIFICATION.md` (c59e02c, on its own branch, not merged).
FRM-03 (open event streams never told `closed` on shutdown under the shipped entrypoint) and FRM-04 (an hour-bound
test) are fixed on `followups-shutdown-hours-fix-20261024`, together with FRM-06 (the stage's realtime suites
would have skipped on GitHub CI). The whole merge verification was re-run at `7e19cbc`, day and night, then merged
into `worktree-billing-google-auth` as one merge commit whose tree is byte-identical to the verified head.

## Phase status

| Phase | State | Evidence (E:\wasla-frm-notes\rerun\) | Cairo (UTC+3) | Commit or push |
| --- | --- | --- | --- | --- |
| A0 state, ancestry, untracked, infra | done | evidence/untracked_start.sha256, net_ips.txt | 24 Oct 11:11–11:40 | fix branch from b7d4ccf |
| A1–A3 FRM-03 reproduced; real-signal test fails at b7d4ccf | done | evidence/stop_b7d4ccf_*, logs/A3_* | 12:01–12:58 | — |
| A4 FRM-03 fix | done | logs/A4_*, diag_*, evidence/stop_ee0be25_* | 12:04–12:58 | ee0be25 |
| A5 / A6 mutants; FRM-04 fix | done: 7 / 7 KILLED | evidence/mutants_partA.jsonl, logs/shift_* | 12:05–13:12 | 8f91ec9 |
| A7 HB-05 sweep | done: 1 HOUR-BOMB (FRM-04), 0 unclassified | evidence/HB05_table.md | 13:00 | — |
| A8 docs, ADR-157 | done | — | 12:55 | 4b3aef8 |
| FRM-06 (found by the CI emulation) | done | logs/B_ci_redis_skip.log, A_frm06_ci_env.log | 13:15–13:19 | 7e19cbc = SFX_HEAD |
| A9 Part A gates at 7e19cbc | done: green | logs/A9b_*, clock_a9b_day.log | 13:20–13:40 | — |
| B day gates at 7e19cbc | done: green (FRM-07 in the extra day lane pair) | section 13–24 | 13:20–17:35 | — |
| clock changed on the machine (15:22–17:38 UTC real) | runs in that window voided and re-run | section "Clock change" | — | — |
| B night gates (real clock, from 22:00:31) | done: green; night lane pair 0 failed | section "Night" | 22:00–23:33 | — |
| merge | done | — | 23:34 | 3bc7c74 |
| gates on the final tree | done: green | logs/F_* | 23:35– | — |
| report commit, push, GitHub Actions | see the next commit | — | — | — |

## 1. Executive Summary

- **FRM-03 closed.** uvicorn 0.35 registers `Server.handle_exit` in `capture_signals()` before it imports the app,
  so the stage's class-level patch never ran under the entrypoint. The API now starts as `python -m app.serve`, a
  uvicorn `Server` subclass whose exit handler closes every stream (`server_restart`) and then continues uvicorn's
  exit. `docker stop` with a stream open: before ~26.6 s, no `closed`, the graceful bound hit (137 at this Docker's
  ~3 s default); after `closed` in 0.06–0.14 s, exit 0 in 1.2–1.5 s, directly and through nginx, 1 and 20
  streams, -t 30 and the default, 5 stop / start cycles. A new test sends a real SIGTERM / SIGINT to the
  entrypoint in a child process (Linux): it fails at b7d4ccf on the missing `closed` (25.1–25.3 s) and passes at
  every later gate; M-R01..M-R06 killed.
- **FRM-04 closed** test-only (quiet hours switched off through the owner's settings path, asserted); green inside
  and outside quiet hours and under shifted clocks; M-Q01 killed; the HB-05 sweep of all tests/ found no other
  hour bomb.
- **FRM-06 closed** (found here): the stage's realtime suites skipped wherever `WASLA_TEST_REDIS_URL` was unset —
  ci.yml sets it in no job, so GitHub's skip gate would have failed; they now default to database 15 on the
  suites' Redis, as the AI and RAG harnesses do.
- **Findings recorded:** FRM-05 (two unreproduced SIGINT-5 timing failures under host load during A4),
  FRM-07 (pre-existing timing / race tests, unchanged since origin, failing only under load or a Windows
  socket timeout; pass alone), the day model lane's use of the developer's Redis DB 15 (my CI_ENV option), and the
  machine-clock change (runs in that window voided and re-run).
- **Night, real clock, dev stack stopped:** both full lanes together 0 failed (7,420 / 7,424 passed); populated
  upgrade U-1..U-6 and the restart after it; APP-E2E V-1..V-11; clock run 2; refused downgrades — all green.

## 2. Source Branches and Exact Heads

| Name | Value |
| --- | --- |
| ORIGIN_CANONICAL_START_HEAD | `8752324191365bbb901bd95a204b68dbaff5dd93` (= origin at the start, 0 / 0) |
| FUR_HEAD / FUR_CODE_HEAD | `3215eecbbdfb645151938a9f6c39d4f7cd2c6da4` / `6d630c3` (not moved) |
| FRM02_HEAD | `b7d4ccf0024860fabd924b1cc38b6247e5afe00a` (not moved) |
| SFX_HEAD | `7e19cbc0a22c3984e82bbe4bdd423fd220573c42` on `followups-shutdown-hours-fix-20261024` (local) |
| ORIGIN_MIGRATION_HEAD | 0120; stage / SFX head 0126 |
| MAIN_HEAD | `8e3a1ba5a63651b7b7da3bdf14845f40a9d93667` (untouched) |
| LAST_ADR before / after | ADR-156 / ADR-157 |
| Tools | Docker 29.8.0 (default stop grace measured ~3 s), Compose v5.5.1, pgvector/pgvector:pg16 (16.15), redis:7-alpine, Python 3.12.7 (Windows venv) / 3.12.14 (wasla-frm-linux), uvicorn 0.35.0 (venv, image, linux) |
| app.__file__ | asserted per run: E:\wasla-frm-fix / -verify / -lane / -mut / -origin, /w/verify, /w/fix, /w/mut in wasla-frm-linux |

Ancestry: `8752324 ⊂ 6d630c3 ⊂ 3215eec ⊂ b7d4ccf ⊂ 7e19cbc` (each `git merge-base --is-ancestor` true);
`git diff --name-only 3215eec b7d4ccf` = tests/integration/test_campaign_messaging_limit.py.

## 3. Infrastructure Reused and Reset

Decision applied (section 6): the wasla-frm-* containers, network and venv of the last attempt reused; none of
their data, images-as-results or evidence.

| Item | Action |
| --- | --- |
| wasla-frm-pg / -pg2 / -pg3 (Africa/Cairo) | kept; every wasla_frm_* database dropped (23 / 12 / 6), proven by container name and port |
| wasla-frm-pg4 | re-created (same name, image, port 55994; log_lock_waits on) without `deadlock_timeout=200ms` → 1 s |
| wasla-frm-redis..redis4 | FLUSHALL, proven by container name and port |
| wasla-frm-minio | bucket wasla-media emptied with `mc rm --recursive --force` (96 → 0 objects) |
| wasla-frm-net | pg*, redis*, minio connected to it (IPs in evidence/net_ips.txt) |
| wasla-frm:b7d4ccf | kept for P-RT2's "before"; built wasla-frm:ee0be25 (A4) and wasla-frm:7e19cbc (Part B) from `git archive` |
| wasla-frm-linux (new) | python:3.12-slim@sha256:78387bc3… (the Dockerfile's base), worktrees mounted read-only, its own venv from requirements.lock (`--require-hashes`, uvloop 0.22.1 included) + the pinned dev extras (evidence/linux_freeze.txt) |
| venv E:\wasla-frm-notes\venv | the last attempt's: the lock minus uvloop (no Windows build) + dev extras + time-machine 3.5.1 (shift plugin only) |
| notes | E:\wasla-frm-notes\rerun\ (the last attempt's files read, never overwritten) |

The suite blocks live DNS (tests/conftest.py:225), so the Linux lanes use IP literals.

## 4. Decisions Applied

| ID | Applied as |
| --- | --- |
| SD-01 | default: `app/serve.py` (`python -m app.serve`) builds uvicorn's Config and runs `StreamClosingServer(uvicorn.Server)`, whose `handle_exit` calls `close_every_stream("server_restart")` then `super().handle_exit`; the class-level patch and its two `type: ignore` lines removed from app/main.py. Option table: 33 Config fields identical (evidence/sd01_option_table.md); difference: the uvicorn CLI read `UVICORN_*` variables, the launcher does not (none set by any shipped file) |
| SD-02 | no `--workers` / `WEB_CONCURRENCY` in any shipped file; uvicorn's Config still reads WEB_CONCURRENCY; with WEB_CONCURRENCY=2 the real-signal test passed (first `closed` 1.63 s / 0.47 s) — recorded, not a gate |
| SD-03 | the entrypoint already `exec`s; PID 1 = `python -m app.serve …` (was `/opt/venv/bin/python /opt/venv/bin/uvicorn …`); no STOPSIGNAL (SIGTERM); Dockerfile unchanged |
| SD-04 | launch sites: entrypoint api (both branches → launcher), compose dev `command: ["api"]` + UVICORN_RELOAD, compose prod image CMD, ci.yml docker job (image default); README developer command → `python -m app.serve --reload`; lifespan logs `events.shutdown_hook active=true|false` (`isinstance(signal.getsignal(SIGTERM).__self__, StreamClosingServer)`) |
| SD-05 | met in every run (sections 7, 22); plus: a hub closed for shutdown closes a late subscriber at once; the launcher exits 0 after a graceful shutdown whether or not it is PID 1 (uvicorn re-raises the captured signal; the launcher's SIGTERM handler raises SystemExit(0)) |
| SD-06 | files: app/serve.py (new), app/main.py, app/services/realtime.py (where the close path lives — the prompt's "app/core/events.py" mapped to it), scripts/entrypoint.sh, tests, docs, README, DECISIONS.md |
| SD-07 | `test_events_stream`'s live server is `StreamClosingServer`; the shutdown test asserts it and its docstring names test_events_shutdown_signal |
| HB-01 | FRM-04 and FRM-06 commits touch tests/ only (proven) |
| HB-02 | FRM-04: same 25 recipients, same per-transition assertions, same 25 sends |
| HB-03 | default: quiet hours switched off through `CampaignSettingsService.update` (owner role), asserted with the message the prompt names |
| HB-04 | real time (outside) pass; shifted 23:00 (prev. day), 03:00, 08:59:30, 09:00:30, and 21:59:30 / 22:00:30 of the previous day pass; 21:59:30 / 22:00:30 of the run's day (+10 h forward) SHIFT-ARTEFACT (app/core/events.py:264 trims with the shifted app clock against Redis's real ids); inside real quiet hours: Part B night lanes |
| HB-05 | evidence/HB05_table.md: 96 tests under the quiet_hours mark; 38 PINNED, 57 INVARIANT, 1 BOMB (FRM-04, now OFF); unmarked tests run with QuietHours.contains patched False (TZ-03 autouse fixture); 21 unmarked clock/quiet hits are pure functions or prose; 0 unclassified |
| HB-06 | 26 files (clock_selection_hb06.txt) |

## 5. FRM-03: Reproduction at b7d4ccf

uvicorn 0.35.0 `server.py`: l. 69–71 `async def serve(...)` → `with self.capture_signals(): await self._serve(sockets)`;
l. 321 `original_handlers = {sig: signal.signal(sig, self.handle_exit) for sig in HANDLED_SIGNALS}`;
l. 77–78 `if not config.loaded: config.load()` (the import of app.main:app, after the handler is bound);
l. 330–331 after shutdown `signal.raise_signal(captured_signal)`.

| Image | docker stop | streams | `closed` | first / last end (s) | stop (s) | exit | "timeout graceful shutdown exceeded" | app.shutdown |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| b7d4ccf | -t 30 | 1 | 0 / 1 | 25.56 (cut) | 26.61 | 0 | yes | yes |
| b7d4ccf | default (~3 s on this Docker) | 1 | 0 / 1 | 3.09 (killed) | 3.59 | **137** | no | **no** |
| b7d4ccf | -t 30 | 20 | 0 / 20 | 25.20 / 25.27 | 26.31 | 0 | yes | yes |

PID 1 in the container: `/opt/venv/bin/python /opt/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
--no-proxy-headers --timeout-graceful-shutdown 25` (exec). Real-signal test at b7d4ccf (a throwaway copy of the tree
with the new test file, Linux): SIGTERM-1 / -5, SIGINT-1 / -5 each fail first on the missing `closed`
(25.15–25.34 s; SIGTERM exit −15), the plain-uvicorn case fails (no SD-04 line).

## 6. FRM-03: The Fix

| File | Change |
| --- | --- |
| app/serve.py (new, 120 lines) | `StreamClosingServer`, `stream_shutdown_active()`, `parse()`, `config_from()`, `main()`; `__main__` runs `app.serve.main` so the class is the importable one |
| app/main.py | `install_stream_shutdown` and its 2 `type: ignore` removed; the SD-04 start-up line |
| app/services/realtime.py | `StreamHub._closed_with`: set by `close_all`; `subscribe` closes a late subscriber at once |
| scripts/entrypoint.sh | both api `exec uvicorn …` lines → `exec python -m app.serve --host … --port … [--reload | --timeout-graceful-shutdown …]` |
| tests/integration/test_events_stream.py | SD-07 |
| tests/integration/test_events_shutdown_signal.py (new) | section 7 |

## 7. The Real-Signal Test

`tests/integration/test_events_shutdown_signal.py`: starts `sh scripts/entrypoint.sh api` (this interpreter on PATH,
the worktree as PYTHONPATH, the child's `app.__file__` asserted) on a free port against the session's database and
Redis; waits for /health/live; tickets issued with `issue_ticket`; streams read to the first keep-alive; `os.kill`
SIGTERM or SIGINT; asserts every stream's `closed` = {"reason": "server_restart"}, first ≤ 2 s and last ≤ 5 s, a late
stream refused or closed at once, exit 0 within 10 s, no "timeout graceful shutdown exceeded" / "running task(s)",
`app.shutdown` logged, `events.shutdown_hook active=true`, closed_total{server_restart} +streams (+1 when the late
stream was served) — read from the Redis counter hash, because /metrics is gone once the process exits. Parameters:
SIGTERM / SIGINT × 1 / 5 streams; plus `test_plain_uvicorn_says_streams_will_be_cut` (active=false). Own wall-clock
bound 240 s per test (no pytest-timeout in the lock); the child is always reaped. POSIX-only (skipif win32).

| Run | Result | first `closed` (s) | exit (s) |
| --- | --- | --- | --- |
| at b7d4ccf (A3) | 4 / 4 signal cases fail on the missing `closed`; plain case fails | — (cut 25.1–25.3) | 25.2–25.8 |
| A4 at the fix, 5 runs | runs 1, 2, 5 green; runs 3, 4: SIGINT-5 failed (FRM-05) | 0.0015–0.036 | 0.65–2.5 |
| diag loops (full file ×4 under load, ×5 under CPU burn; SIGINT-5 ×8) | 17 + 8 green | 0.0015–0.03 | 0.65–1.1 |
| A9 at 4b3aef8, 3 runs | 15 / 15 | ≤ 0.03 | ≤ 1.2 |
| A9b at 7e19cbc, 3 runs | 15 / 15 | | |
| Part B at 7e19cbc (/w/verify), 3 runs | 15 / 15 | SIGTERM-5: 0.003–0.005 (last ≤ 0.010) | 0.73–0.89 |
| SD-02, WEB_CONCURRENCY=2 | 2 / 2 | 1.63 (1 stream) / 0.47 (5) | 2.07 / 0.91 |

## 8. FRM-04 and the Hour Sweep

Reproduction (shifted 23:00 Cairo, b7d4ccf): `assert 0 == 25` (logs/shift_FRM04_b7d4ccf_2300.log). Fix 8f91ec9
(test only): `quiet_hours_off()` through `CampaignSettingsService.update(actor=owner, TENANT_OWNER, enabled=False,
start/end unchanged)`, asserted. HB-04 hours: section 4. HB-05: section 4 (no further HOUR-BOMB, so no FRM-0N hour
commit). HB-06: 26 files.

## 9. Mutation Matrix of this Stage

| Mutant | Change | Killer (platform) | Verdict |
| --- | --- | --- | --- |
| M-R01 | launcher runs plain `uvicorn.Server` | real-signal SIGTERM-1 (Linux) | KILLED |
| M-R02 | plain Server + the class-level hook installed in the lifespan (after capture_signals) | real-signal SIGTERM-1 (Linux) | KILLED |
| M-R03 | `handle_exit` without `close_every_stream` | real-signal SIGTERM-1 (Linux) AND test_a_graceful_shutdown_closes_every_stream_first (Windows) | KILLED by both |
| M-R04 | reason `membership_revoked` | real-signal SIGTERM-1 (Linux) | KILLED |
| M-R05 | entrypoint runs `uvicorn app.main:app … --no-proxy-headers` again | real-signal SIGTERM-1 (Linux; the test runs the entrypoint) | KILLED (first spec INVALID: my anchor had a literal `\n`; corrected and re-run) |
| M-R06 | `stream_shutdown_active` always True | test_plain_uvicorn_says_streams_will_be_cut (Linux) | KILLED |
| M-Q01 | `_campaign_updated` hints on every update (`if True:`) | the FRM-04 test at the real time (Windows) and under shifted 23:00 Cairo | KILLED (6 > 4 hints) both |

Control green first (4 killers); every file restored and SHA-256-checked; `git status` under app/ scripts/ tests/
clean after each. Spot-check and regression lane at SFX_HEAD: section 21.

## 10. Documentation and ADR Changes

DECISIONS.md: ADR-157 (SD-01..SD-07, HB-01..HB-06), ADR-155 amended 2026-10-24. docs/DEPLOYMENT.md "Restart and
drain" (the launcher, about a second, the hook line, plain uvicorn cuts streams; the 25 s bound a backstop);
docs/RUNBOOK.md ("The inbox does not update" step 5; "Deploy a specific version": how the API is started and
stopped); docs/OBSERVABILITY.md (closed_total{server_restart} on every deploy; the hook line); README developer
command. test_documentation_truth 27 passed (247 operations / 19 unauthenticated, README range 0001–0126 unchanged).

## 11. Part A Commits and the Diff Guard

| Commit | Message | Files |
| --- | --- | --- |
| ee0be25 | fix(events): close every stream before uvicorn's graceful shutdown under the real entrypoint (FRM-03) | app/main.py, app/serve.py (A), app/services/realtime.py, scripts/entrypoint.sh, tests/integration/test_events_shutdown_signal.py (A), tests/integration/test_events_stream.py |
| 8f91ec9 | test(events): make the per-transition hint test independent of the hour (FRM-04) | tests/integration/test_events_publish.py |
| 4b3aef8 | docs: stream shutdown under the real entrypoint and hour-bound tests (ADR-157, FRM-03, FRM-04) | DECISIONS.md, README.md, docs/DEPLOYMENT.md, docs/OBSERVABILITY.md, docs/RUNBOOK.md |
| 7e19cbc | test: give the realtime suites CI's Redis when WASLA_TEST_REDIS_URL is unset (FRM-06) | tests/redis_url.py and 5 test files |

`git diff --stat b7d4ccf 7e19cbc`: 15 files, +606 −60 (app 3, scripts 1, tests 7, DECISIONS + README + docs 4; stat in the
commit table); `git diff --name-only <FRM-04 / FRM-06 commit>^ <commit> -- . ':!tests'` empty;
`git diff --exit-code b7d4ccf 7e19cbc -- alembic nginx .github requirements.lock pyproject.toml Dockerfile
docker-compose*.yml deploy` exit 0. `type: ignore`: app 60 → 58, tests 172 → 172. Order note: FRM-06 was found
after the docs commit, so it is last.

## FRM-05 (new) — two unreproduced SIGINT-5 failures during A4

Runs 3 and 4 of five at ee0be25's code (09:06–10:10 UTC), concurrent with Windows shifted-clock runs: run 3
first `closed` 3.61 s after SIGINT (assert ≤ 2 s); run 4 the child stayed at "Waiting for application
shutdown." > 15 s (exit not within the test's 15 s wait). Not reproduced since: 8 × SIGINT-5 alone, 4 × the
full file with Windows stream suites running, 5 × the full file under 6 CPU burners — 17 full-file runs and
8 single runs green (first `closed` 1.5–36 ms, exit 0.65–2.5 s). Child logs of the two failures were not kept
(the test deletes them); a diag copy keeping them ran every later loop. Watch item: any recurrence is a stop.


## 12. What Lands

`git log --oneline 8752324..7e19cbc`: the 22 stage commits (phases 1–2 a058bb1; 3 8113a5f; 4 705bd34; 5 fb7124b;
6 980868a; 7 888f2c7; 8 4ffbad0; 9 324d435, 1a3b262; 10 a100716; 11 98919e9; 12 f13b8d3; 13 31e40c0, a3b3add,
b4cb7fb, 946aa15; 14 f9f2f69, 8eb21f6; 15 a57535b, 636e7a0; FSR-02/03 6d630c3; report 3215eec), FRM-02 b7d4ccf,
and this stage's ee0be25 (FRM-03), 8f91ec9 (FRM-04), 4b3aef8 (docs, ADR-157), 7e19cbc (FRM-06).
Migrations: 0001–0120 byte-identical; 0121–0126 added (6 files); no other migration.
Pre-existing tests changed: the stage's seven (test_ai_invariants, test_campaign_quiet_hours,
test_conversation_endpoints, test_omnichannel_oracles, test_route_authorization, test_request_field_bounds,
test_whatsapp_models — each recorded by the stage report), test_campaign_messaging_limit (FRM-02), and this
stage's: none of those; the stage's own new files test_events_stream (SD-07), test_events_publish (FRM-04,
FRM-06), test_campaign_progress_budget, test_follow_up_invariants (FRM-06); new: test_events_shutdown_signal,
tests/redis_url.shared_redis_url.

## 13. The Calendar

DT-05 over the 27 test files changed since 8752324: 56 hits; the last table's classes unchanged (SAFE-PINNED 10
rows, DST-FIXTURE 4, N/A 2) plus 4 prose / docstring hits (N/A); **0 BOMB**. The real date (2026-10-24) is before
the DST end, so the DST fixtures need no re-check yet. HB-05 at SFX_HEAD: 0 HOUR-BOMB, 0 unclassified.
Time-shifted runs: section "Part B results" of the draft — the +100-day lane (92 tests / 103 cases, every one a
SHIFT-ARTEFACT: process clock vs object store, Redis or PostgreSQL, each with its comparing line) and the five
dated runs (the same 8 SHIFT-ARTEFACTs at each moment).

## 14–24. Part B Results Ledger (SFX_HEAD = 7e19cbc; sections 14–24 of the prompt)

| Gate | Result | Evidence | Cairo / UTC |
| --- | --- | --- | --- |
| alembic (fresh, pg4) | 126 steps 13.4 s; check; 0/0/0; preflight ok; invariants 70 / 0 (CR-01..CR-17, FU-01..FU-08, RT-I01 = 0); head→0120→head 6/6; base→head 126/126; doc truth 27 | logs/B_alembic_gates.log | 13:20 / 10:20 |
| static (E:\wasla-frm-verify) | ruff, black (1,039), mypy app tests (900), mypy scripts (12) clean; type: ignore app 58 (b7d4ccf 60), tests 172 | logs/B_static.log | 13:10 / 10:10 |
| TZ-06 (pg3 Africa/Cairo) | 16 / 0 | logs/B_tz06.log | 13:32 / 10:32 |
| CI monitoring job | promtool config ok; 81 rules; rule tests SUCCESS; amtool SUCCESS | logs/B_ci_monitoring.log | 13:23 / 10:23 |
| CI docker job | CI-exact liveness attempt 8 (18 s); with DB+Redis: ready ok (postgresql up, redis up); uid 1001; PID 1 `python -m app.serve --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 25`; serve.config proxy_headers=False timeout 25 workers 1; `events.shutdown_hook active=true` | logs/B_ci_docker.log | 13:45 / 10:45 |
| SD-02 two workers (recorded, not a gate) | WEB_CONCURRENCY=2, SIGTERM-1 / -5 pass; first closed 1.63 s / 0.47 s; exit 2.07 s / 0.91 s | logs/SD02_workers2.log | 13:47 / 10:47 |
| probes P-FU1..P-TZ1 | 7 / 7 reproduce at origin, 7 / 7 the new behaviour at 7e19cbc | evidence/probes_origin.json, probes_verify.json | 13:48–13:52 / 10:48–10:52 |
| calendar (DT-05) | 27 test files changed since 8752324; 56 hits; the last table's classes unchanged + 4 prose/docstring (N/A); 0 BOMB | evidence/calendar_sweep_raw.txt | 13:50 / 10:50 |
| HB-05 at SFX_HEAD | 96 marked tests; FRM-04's test HOUR-SAFE-OFF; 0 HOUR-BOMB; 0 unclassified | evidence/hour_tests_7e19cbc.tsv | 13:51 / 10:51 |
| day populated upgrade, attempt 1 | U-1..U-5 PASS; U-6 FAIL in my script: by day the origin's own campaign reached one recipient at 10:32, so the running campaign rightly waits `contact_cap` to the next day (5 of 6 reached once, 0 again); predicate corrected (completed, or waiting contact_cap with exactly the capped pending) | logs/upgrade_day_attempt1.log | 13:30–13:47 / 10:30–10:47 |
| day populated upgrade, attempt 2 | **PASS**: 0120→0126 6 steps; 64 tables, 271 rows, 0 differences (271 vs 230 at night: by day origin's campaign sent during population - business_initiated_sends 0→4, messages 22→35, usage_events 22→48, audit_logs 15→13); U-1 338 s, 21 sweeps, 0 runs, 0 reminders, 1 send (the old due follow-up), 0 to others; U-2, U-2i, U-3, U-3i, U-4 day (sent once), U-5, U-6 (waits contact_cap for the 1 capped, 4 + 1 before reached once, 0 again, paused stays paused) PASS; invariants after 0 | logs/upgrade_day.log, evidence/upgrade_*_day.json | 13:42–13:55 / 10:42–10:55 |
| stream through nginx (nginx:1.27-alpine, shipped nginx.conf, image wasla-frm:7e19cbc) | `nginx -t` ok; hold 300.0 s, 20 keep-alives, no 504; ticket 0 and token 0 in 34 nginx + 18 API lines; nginx logs `$uri` only; reused ticket 401 | evidence/nx_hold.json, logs/nx_* | 13:46–13:52 / 10:46–10:52 |
| docker stop through nginx | -t 30 ×1: closed 0.094 s, stop 1.36 s, exit 0; default ×1: 0.078 s, 1.31 s, 0; -t 30 ×20: 20/20 by 0.14 s, 1.34 s, 0, counter +20; 5 cycles: 0.063–0.109 s, 1.22–1.33 s, 0; bound hit 0; app.shutdown each; active=true | logs/B_nginx_series.log | 13:52–14:02 / 10:52–11:02 |
| P-RT2 | before (wasla-frm:b7d4ccf): no `closed`, cut 25.56 s, stop 26.61 s, bound hit; after (wasla-frm:7e19cbc, direct): closed 0.093 s, stop 1.45 s, exit 0 (default 1.55 s; 20 streams 20/20 by 0.125 s, +20) | evidence/stop_*_30_1.json | 12:55 / 14:03 |
| clock run 1 (outside quiet hours, machine zone, verify) | 218 passed, 5 skipped (Windows POSIX-only), 0 failed; 10:41–10:56 UTC, 13:41–13:56 Cairo; uptime 2 d 04:06 | logs/clock_run1_outside.log | 13:41–13:56 / 10:41–10:56 |

| Linux (wasla-frm-linux, /w/verify, 7e19cbc) | signal 3/3 (15/15), stream 3/3 (42/42), capacity 3/3, response 3/3; walls 57–70 s / 47–53 s / 23–25 s / 1.3–2 s; SIGTERM-5 race: first 0.004 s last 0.007 s exit 0.73 s; first 0.003 s last 0.006 s exit 0.75 s; first 0.005 s last 0.010 s exit 0.89 s | logs/B_linux_* | 14:00–14:25 / 11:00–11:25 |
| kept-data sweep (ci.yml selection: 29 suites + AI + tool invariants; fresh migration-built db each) | 3 / 3: 304 passed, 0 skipped each (8:29, 7:46, 7:45) | logs/kept_k1..3.log, evidence/kept_selection_k*.txt | 13:49–14:14 / 10:49–11:14 |
| sequence_harness suites with kept data (8 files) + AI/tool invariants | invariant checks pass (120 passed, 5 Windows skips); the 2 non-vacuity tests fail by design: no AI tool data to sweep - evidence for the recommendation (they would need their own non-vacuity rule) | logs/kept_seq.log | 14:14–14:17 / 11:14–11:17 |
| clock run 3 (TZ=UTC0, verify) | 218 passed, 5 skipped, 0 failed | logs/clock_run3_utc.log | 14:05–14:20 / 11:05–11:20 |
| races (pg4/redis4, verify; two follow-up workers, per-recipient header files with 2 workers, campaign replicas at the messaging limit, stream suite, 200-stream capacity) | 3 / 3: 38 passed each (10:34, 11:09, 10:47), 0 Redis keys after; the suites assert 0 steps twice, 0 people twice, uploads per file, streams uncounted on leave; real-signal race on Linux (above) | logs/races_run1..3.log | 14:21–14:55 / 11:21–11:55 |
| dated shifted runs (17 files: the stage's new test files + every file this stage changed; Python clock only) | first attempt void (DB-name case: tag with "T"); re-run: 2026-10-29 21:30 and 2026-10-30 09:30 Cairo: 8 failed, 123 passed, 5 skipped each — all SHIFT-ARTEFACT (classification below) | logs/shiftlane_d*.log | 14:3x– / 11:3x– |

Dated-run failures, classified (the same 8 at each moment so far):

| Test | Assertion | Class | Clocks compared (comparing line) |
| --- | --- | --- | --- |
| test_events_publish::test_an_inbound_message_and_the_reply_are_hinted_without_their_words | `[] == ['inbound','outbound']` | SHIFT-ARTEFACT (process vs Redis) | app/core/events.py:264 `oldest = int((time.time() - retention) * 1000)` (shifted) trims entries whose ids Redis stamped with its real clock |
| test_events_publish::test_a_takeover_and_a_follow_up_are_hinted | `assert ([])` | SHIFT-ARTEFACT (process vs Redis) | same line |
| test_events_publish::test_a_campaign_is_hinted_per_transition_never_per_recipient | `assert ([])` (hints, not sends) | SHIFT-ARTEFACT (process vs Redis) | same line |
| test_events_stream::test_last_event_id_resumes_without_a_gap_or_a_duplicate | `0 >= 2` replayed | SHIFT-ARTEFACT (process vs Redis) | same line |
| test_events_stream::test_a_last_event_id_older_than_the_stream_starts_with_resync | IndexError (no entries) | SHIFT-ARTEFACT (process vs Redis) | same line |
| test_follow_up_invariants::test_fu07_a_template_follow_up_sent_in_quiet_hours | `{} == {'fu07…': 1}` | SHIFT-ARTEFACT (process vs database) | scripts/omnichannel_invariants.py:793 `f.sent_at >= fs.created_at`: created_at = ANCHOR − 2 d (shifted process clock, 2026-10-27) vs sent_at from `_local()` = PostgreSQL now() yesterday 23:30 (2026-10-23) |
| test_follow_up_sequences::test_a_reply_cancels_the_next_step_and_the_next_ai_reply_starts_again | restarted step None | SHIFT-ARTEFACT (process vs database) | app/services/follow_up_sequences.py:229 → `_wrote_since` (`Message.created_at > previous.anchor_at`): inbound created_at = PostgreSQL now() (2026-10-24) vs anchor_at from the shifted process clock (2026-10-29) |
| test_inbox_incremental::test_updated_since_is_inclusive_and_conditional | `304 == 200` | SHIFT-ARTEFACT (process vs database) | the reply's `updated_at` = `onupdate=func.now()` (app/db/base.py:56, PostgreSQL real clock, 2026-10-24) vs `updated_since` = shifted process now − 1 h (2026-10-29) |
| dated runs 2027-04-29 21:30, 2027-01-01 00:30, 2028-02-29 10:00 Cairo | 8 failed / 123 passed / 5 skipped each; the same 8 tests at all five moments (sorted list hash 71c7eadd) — classified above | logs/shiftlane_d*.log | to 12:15 UTC |
| time-shifted lane +100 days (MODEL-BUILT, Python clock only, 2027-02-01) | 7,317 passed, 103 failed (92 tests), 21 skipped, 2 deselected, 1:14:31 | logs/shiftlane_plus100.log | 14:05–15:20 / 11:05–12:20 |

+100-day failures, classified (every one passes in the real-clock lanes):

| Group | Tests | Class | Clocks compared |
| --- | --- | --- | --- |
| object store | 51 `StorageError: The file store is unavailable`, 5 `KeyError: 'id'` (upload refused), 1 `503 == 201`, 3 `DID NOT RAISE InjectedFailureError` (the store fails before the injected point), and the media reconciliation / retention / purge suites whose first error follows from it (MediaStorageState PENDING, FAILED vs STORED, `0 == 1` stored, gauges `2.0 == 1.0`, `[] == [key]` after purge, status event owed) | SHIFT-ARTEFACT (process vs object store) | app/core/object_store.py:379 `now = dt.datetime.now(dt.UTC)` → `x-amz-date` (l. 387); proven: real clock HEAD 404, +100 d HEAD 403 (evidence/s3_skew_probe.txt) |
| realtime hints / replay | 5 (as in the dated runs) | SHIFT-ARTEFACT (process vs Redis) | app/core/events.py:264 |
| process vs database windows | fu07, cr04, e14 invariants; hold counted; follow-up restart; updated_since 304; retried create key, retention sweep for create keys ("after a day"), retention "too young" ×2, 0120 cap "old"; analytics overview / deleted workspace / consumption / page total / summary / split by channel; document publishing race; sweep finishes only what nothing else will | SHIFT-ARTEFACT (process vs database) | each compares a moment from `datetime.now()` (shifted) with a column stamped by `server_default` / `onupdate=func.now()` (app/db/base.py:50, 56) or an SQL `now()` window (PostgreSQL's real clock) |
| per-day limits | test_a_workspace_sends_ten_tests_a_day (`201 == 429`), test_the_export_is_excel_safe… (`200 == 429`) | SHIFT-ARTEFACT (process vs database/Redis) | app/services/campaign_service.py:1576 `since = moment - TEST_SEND_WINDOW` (shifted process clock) vs `CampaignTestSend.created_at > since` (l. 1583, database clock): none counted; the export limit likewise counts rows stamped by the database |
| day lane pair (extra; together, 15:30–16:54 Cairo, outside quiet hours) | MODEL-BUILT with CI's Redis setup (WASLA_TEST_REDIS_URL unset): 7,418 passed, **2 failed**, 21 skipped, 2 deselected; MIGRATION-BUILT: 7,423 passed, **1 failed**, 17 skipped, 2 deselected; tracked tree clean; 0 Redis keys after → **FRM-07** | logs/lane_model_day.log, lane_migrations_day.log | 15:30–16:54 / 12:30–13:54 |

**FRM-07** (load-sensitive pre-existing tests; recorded before anything else): test_ai_turn_charging::test_ten_overlapping_failing_turns_charge_nothing (`gate.decided 0 == 10`), test_media_parser_containment::test_the_audit_amplifying_pdf_is_refused_or_killed_and_never_stalls_the_loop (`1.236 < 1.0` loop stall), test_ai_reply_bounds::test_an_agent_created_without_a_ceiling_gets_one_in_the_database (asyncpg connect TimeoutError, migration lane). All three exist unchanged at 8752324 (green on GitHub); all three pass alone at 7e19cbc (logs/suspects_alone.log, 3 / 3 in 48 s). The machine carried the developer stack's wasla-api-1 at ~84 % CPU all day (untouched). Rule applied: the required night pair must be 0 failed; any failure there is a stop.
| mutation spot-check (E:\wasla-frm-mut at 7e19cbc; after the day lanes; 900 s limit each) | controls green (31 / 224 / 1 / 4 killers); stage: M-S01, 02, 04, 05, 07, 08, 09, 16, 17, 18, 21, 22, 23, 26, 27, 29, 31, 32, 33, 34, 35, 36, 38, 40 — 24 / 24 KILLED; regression: M-F02, M-F13, M-F31 (CPM-06 test), M-C01+M08, M-C09, M-H01, M-D01 — 7 / 7 KILLED; this stage: M-R01..M-R06 (Linux), M-Q01 — 7 / 7 KILLED; 0 survived, 0 hung, every file restored (SHA-256), git status clean | evidence/spot_fur.txt, spot_cpm.txt, spot_dt.txt, spot_partA.jsonl | 16:57–17:35 / 13:57–14:35 |

## Night plan (from 22:00 Cairo = 19:00 UTC)
1. MODEL-BUILT (CI's Redis setup) + MIGRATION-BUILT together, nothing else running — must be 0 failed (FRM-07 rule)
2. night populated upgrade U-1..U-6, then the API restart with a real SIGTERM (proof/v11_restart.py in wasla-frm-linux) and the worker hold (copies/frr_restart_hold.py)
3. APP-E2E V-1..V-10 at night + V-11 (proof/v11_restart.py --inbound); refused downgrades on copies of wasla_frr_e2e
4. clock run 2 (inside quiet hours)
5. merge, gates on the final tree, report commit, push, GitHub Actions

**Deviation found at night (recorded before anything else):** the day MODEL-BUILT lane ran with `CI_ENV=1`
(WASLA_TEST_REDIS_URL unset). With it unset, tests/integration/ai_harness.py:62 and rag_harness.py:52 default to
`redis://localhost:6379/15` — on this machine the developer stack's Redis (wasla-redis-1), database 15 (not 0).
So the day model lane's AI / RAG suites used the developer's Redis DB 15 (12:30–13:54 UTC), and so did the first night model attempt (19:01–19:12 UTC) and the sequential attempt 2 (19:49–20:00 UTC). Nothing in this stage reads or relies on that data; the developer's DB 0 was not used. It surfaced when the
user stopped the dev stack at ~20:00 UTC: the night CI_ENV lane (attempt 3) errored at once (32 F / 37 E at 1 %,
log kept in logs/void/). CI_ENV is not used again; FRM-06's CI behaviour stays proven by the targeted run against
this stage's Redis (logs/A_frm06_ci_env.log). Night attempts: 1 (together, under dev-stack load): stopped after
the FRM-07 failure; 2 (sequential): stopped when the user stopped the dev stack; 3 (CI_ENV): void as above;
4: both lanes together with the dev stack stopped (model restarted at ~20:08 UTC, migrations from 19:56 UTC).


## Clock change (reported by the user; recorded before anything else)

The machine's clock was changed during the stage and is back on automatic time sync. Windows System log
(Kernel-General, event 1): at real 15:22:18 UTC the clock was set to 18:58:55 (+3 h 36 m 37 s); at machine
21:14:39 it was set back to 17:38:02 UTC (real). My earlier note "the machine slept 18:21–21:58" is wrong: that
jump was this clock change.

Check after the restore (2026-10-24): machine `date -u` 17:42:13Z; https://www.google.com Date header
17:42:14 GMT; w32tm against time.windows.com offset −0.022 s; PostgreSQL (wasla-frm-pg) now() 17:42:15 UTC;
Redis TIME 1792863735 = 17:42:15 UTC; `date -u` in wasla-frm-pg 17:42:16Z — all within 3 s.

Runs made while the clock was changed — **not evidence**, re-run:

| Run | Machine time (UTC) | Real time (UTC) |
| --- | --- | --- |
| night lane pair attempt 1 (together, dev stack up; FRM-07 failure seen) | 19:01–19:12 | 15:24–15:35 |
| night lane attempt 2 (sequential) | 19:49–20:00 | 16:12–16:23 |
| night lane attempt 3 (CI_ENV; void anyway) | 19:55–20:05 | 16:18–16:28 |
| night lane pair attempt 4 (dev stack stopped): model 1 failed (test_pdf_extraction_bounds 1.9997 s), migrations 1 failed (WinError 121) | 19:56–21:09 | 16:19–17:32 |
| test_whatsapp_inbound_concurrency 3× alone | ~21:11–21:13 | ~17:34–17:36 |

Everything before 15:22:18 UTC ran on the real clock and stands (Part A, Part B day gates, the day lane pair
with FRM-07, the spot-check). The night gates restart after the real time passes 19:00 UTC (22:00 Cairo); the
clock is not changed by me. User decision (2026-10-24): re-run the night pair once more; continue to the merge
only if both lanes are 0 failed, otherwise stop NOT MERGED.

## Night (real clock, verified 19:00:31 UTC by machine / Google / PostgreSQL; dev stack stopped by the user)

| Gate | Result | Evidence | Cairo / UTC |
| --- | --- | --- | --- |
| MODEL-BUILT (pg / redis, verify), together with MIGRATION-BUILT, inside quiet hours | 7,443 collected (2 deselected, as CI); **7,420 passed, 0 failed**, 21 skipped (OpenAI ×11, schema parity ×4, tmpfs ×1, POSIX-only signal ×5); 58:49; tracked tree clean; 0 Redis keys after | logs/lane_model_night.log | 22:00:54–23:00:07 / 19:00:53–20:00:06 |
| MIGRATION-BUILT (pg2 / redis3, lane; fresh `alembic upgrade head`) | 7,443 collected (2 deselected); **7,424 passed, 0 failed**, 17 skipped (OpenAI ×11, tmpfs ×1, signal ×5); 59:04; clean; 0 keys | logs/lane_migrations_night.log | 22:01:09–23:00:33 / 19:01:09–20:00:33 |

Collected 7,443 = the last attempt's 7,438 + the 5 cases of test_events_shutdown_signal. The user's 0-failed
criterion is met; continuing.
| night populated upgrade (attempt 2; attempt 1's U-1..U-6 also all PASS, its restart hold void: my hold ran in a second shell with a newly generated credential key — "The key this credential was encrypted with is not configured" — 0 sends, 2 pending outbound rows per failed step attempt, observation only) | 0120→0126 6 steps 4 s; 64 tables, 230 rows, 0 differences; U-1 337 s, 21 sweeps, 0 runs, 0 reminders, 1 send (the old due follow-up); U-2, U-2i, U-3, U-3i PASS; U-4 night waits to 09:00 Cairo, quiet_hours, 0 attempts, requested_at unchanged; U-5 PASS; U-6 night 0 sends before 09:00, then completed at 09:00:30 (SimClock): 6 / 6 once, 0 again, paused stays paused | logs/upgrade_night.log, evidence/upgrade_*_night.json | 23:15–23:26 / 20:15–20:26 |
| restart after the night upgrade (real SIGTERM, entrypoint in wasla-frm-linux) + worker hold | stop A: 2 streams closed server_restart 0.005 / 0.006 s, exit 0 in 0.63 s, bound not hit, app.shutdown, active=true; restarted and stopped again likewise; then 341 s / 21 sweeps of the follow-up, agent and campaign workers: 2 sends = 2 steps already pending before the restart, 0 new runs, 0 steps twice, 0 recipients twice, invariants ok → PASS | evidence/upgrade_night_restart.json, upgrade_night_restart_hold.json | 23:26–23:33 / 20:26–20:33 |
| clock run 2 (inside quiet hours, machine zone, verify) | 218 passed, 5 skipped (Windows POSIX-only), 0 failed (FRM-04's test included) | logs/clock_run2_inside.log | 23:01–23:13 / 20:01–20:13 |
| APP-E2E V-1..V-10 (night, lane worktree, launcher's server class in process) | 10 / 10 PASS | logs/e2e_night.log | 23:19–23:20 / 20:19–20:20 |
| V-11 (real SIGTERM to the entrypoint in wasla-frm-linux, owner + member streams) | stop: both closed server_restart 0.003 s, exit 0 in 0.67 s; restarted with new tickets; signed webhook 200 → message.created on both streams in 0.219 s; stop again 0.002 s, 0.62 s; outbound 42→42, follow-ups sent 5→5, campaign recipients 30→30, inbound +1 → PASS | evidence/e2e_v11.json | 23:21–23:22 / 20:21–20:22 |
| refused downgrades (copies of wasla_frr_e2e) | 0125, 0124, 0123, 0121 refused (exit 1, "Nothing has been changed"), stamp 0126, 0/0/0; 0122 with default rows only → 0120 exit 0, 0/0/0 | logs/refused_downgrades.log | 23:23 / 20:23 |


## 25. Untracked Files

14 paths (11 documents at the root, FRONTEND_MASTER_PLAN.md, docs/ui/ with its mockups); SHA-256 identical at
the start, before the merge, after the merge and after the report commit (evidence/untracked_*.sha256);
0 collisions with the incoming tree; no incoming path under docs/ui/; the report path was free.

## 26. Merge and Tree Equality

`git merge --no-ff 7e19cbc` in E:\wasla on worktree-billing-google-auth → `3bc7c74ad8552c9849e0ea741b45740eb59737b1`,
parents `8752324` and `7e19cbc`; no conflict; `git diff --exit-code 7e19cbc 3bc7c74` exit 0; `alembic heads` 0126
(single); origin and local were 8752324 immediately before.

## 27. Gates on the Final Canonical Tree (E:\wasla-frm-final, detached at 3bc7c74)

| Gate | Result |
| --- | --- |
| ruff / black / mypy app tests / mypy scripts | clean (1,039 / 900 / 12) |
| alembic, fresh wasla_frr_final | 126 steps 18.4 s; check; 0/0/0; preflight ok; invariants 70 / 0; head→0120→head; base→head; doc truth 27 |
| MIGRATION-BUILT lane | 7,443 collected; 7,424 passed, 0 failed, 17 skipped (OpenAI ×11, tmpfs ×1, POSIX-only signal ×5), 2 deselected; 54:15; 23:38–00:32 Cairo; tracked tree clean; 0 Redis keys |
| kept-data sweep (ci.yml selection, identical to Part B's) | 304 passed, 0 skipped (7:25) |
| race suites (two follow-up workers, stream, capacity) + doc truth | 43 passed |
| Linux (wasla-frm-linux, /w/final) | signal 5 / 5, stream 14 / 14, capacity 1 / 1, response 2 / 2 |

## 28. Push

`git push origin worktree-billing-google-auth` (no force) after this report commit, only with origin still at 8752324; result recorded in the next commit.

## 29. First GitHub Actions Run

Recorded in the next commit.

## 30. Findings

| ID | Final status | Finding |
| --- | --- | --- |
| FRM-01 | ACCEPTED RESIDUAL (informational) | the stage report commit's one-row contract change; consistent with the code |
| FRM-02 | CLOSED (b7d4ccf; lands with this merge) | harness race in test_campaign_messaging_limit |
| FRM-03 | CLOSED locally (Linux, docker, nginx); GitHub run in the next commit | streams never told `closed` under the entrypoint; fixed by app/serve.py (ee0be25); real-signal test; M-R01..06 killed; docker stop through nginx proven |
| FRM-04 | CLOSED | hour-bound test fixed test-only (8f91ec9); green inside / outside quiet hours and shifted; M-Q01 killed |
| FRM-05 | ACCEPTED RESIDUAL | two unreproduced SIGINT-5 failures during A4 under host load (3.61 s first `closed`; one exit > 15 s); 0 recurrences in every later run (≥ 40 runs of the signal file incl. CPU burn, two workers, Part A / B / final gates) |
| FRM-06 | CLOSED (7e19cbc) | realtime suites skipped without WASLA_TEST_REDIS_URL (ci.yml sets none): would fail GitHub's skip gate |
| FRM-07 | ACCEPTED RESIDUAL | pre-existing, unchanged-since-origin timing / race tests failed only under load or a Windows socket timeout (day pair: test_ten_overlapping_failing_turns_charge_nothing, test_the_audit_amplifying_pdf…, test_an_agent_created_without_a_ceiling…; void clock-window runs also showed test_pdf_extraction_bounds 1.9997 s and test_whatsapp_inbound_concurrency WinError 121); each passes alone; the required night pair (dev stack stopped) 0 failed. test_pdf_extraction_bounds' `2.0 <=` bound measured with perf_counter is a test-side clock-resolution edge worth a follow-up |
| deviation | recorded | CI_ENV lanes sent the AI / RAG harnesses to `localhost:6379/15` = the developer's Redis DB 15 (day model lane 12:30–13:54 UTC, night attempts 1–2); DB 0 never used; option retired |
| clock | recorded | the machine clock was set +3 h 36 m 37 s from 15:22:18 to 17:38:02 UTC (real); every run in that window voided and re-run |
| observation | for review | a follow-up step whose send fails before the transport (credential key not configured) leaves a `pending` outbound message row per attempt (seen only with my wrong-key hold) — nothing sent twice |

## 31. Decisions Ledger

| Item | Status |
| --- | --- |
| SD-01..SD-07 | CLOSED (applied as section 4) |
| HB-01..HB-06 | CLOSED (applied as section 4) |
| CI job `quality` | CLOSED (run locally: ruff, black, mypy) |
| CI job `tests` | CLOSED (model-built lane with CI's selection and skip gate; Linux-only signal tests run in wasla-frm-linux); GitHub run: section 29 |
| CI job `migration-parity` | CLOSED (migration-built lanes incl. parity; kept-data sweep 3 / 3 + final) |
| CI job `monitoring` | CLOSED (promtool config, 81 rules, rule tests, amtool) |
| CI job `docker` | CLOSED (CI-exact liveness attempt 8; ready; uid 1001; launcher command line; hook line) |
| nightly CI at a Cairo night hour; injectable clock; install from the lock; timeout-minutes on every job; sequence suites in the kept-data selection (their non-vacuity checks need a rule of their own); trim the event stream relative to Redis's id; the FRM-02 gather pattern in five tests; stop_grace_period | DEFERRED PRODUCT DECISION (recommendations, evidence in this report) |

## 32. Infrastructure Created or Kept by This Stage

Kept (the verdict decides cleanup, section 41 of the prompt): wasla-frm-pg / -pg2 / -pg3 / -pg4 (pg4 re-created),
wasla-frm-redis..redis4, wasla-frm-minio, wasla-frm-linux (new), wasla-frm-net, images wasla-frm:b7d4ccf,
:ee0be25, :7e19cbc; worktrees E:\wasla-frm-fix (fix branch), -verify, -lane, -mut, -origin, -final (new, detached);
E:\wasla-frm-notes\rerun (draft, logs, evidence, proof, copies). Verification scripts copied to
E:\wasla-verification-scripts\followups-realtime\ (section 41).

## 33. Remaining External Actions

- Restart the development stack when convenient (`docker compose -f E:/wasla/docker-compose.yml start`); it
  picks up the new entrypoint only when its owner rebuilds the image. The developer Redis DB 15 may hold keys my
  CI_ENV lanes wrote (test data only).
- Deploy 0121–0126 by the RUNBOOK rule (stop the old processes, `alembic upgrade head`, then start the workers);
  every workspace has reminders ON from then (WhatsApp, one 5-minute reminder). On the real host confirm the first
  API log line `events.shutdown_hook active=true`; keep the orchestrator's stop timeout above about 2 s (the stop
  now takes ~1.3 s; the 25 s bound stays as the backstop).
- The nginx stream location on the real proxy; stream limits sized to real traffic.
- Real-provider lane, Messenger / Instagram reminders, the frontend contract, CMQ-05 subscriptions, open product
  decisions (CMQ-07, CMQ-08, CMQ-10), as in the merge prompt's section 42.
- Recommendations: section 31; plus test_pdf_extraction_bounds' 2.0 s lower bound (FRM-07).
- Infrastructure cleanup after review: wasla-fur-*, wasla-cpf-*, and this line's wasla-frm-* if kept; the local
  branches of this line (stage, FRM-02 fix, this fix, report branches) — never pushed on their own.
- Whether and when worktree-billing-google-auth is merged into main.

## 34. Final Verdict

**FOLLOW-UPS, HEADER MEDIA & REALTIME MERGED — push and the first GitHub Actions run are recorded in the commit that follows this report**
