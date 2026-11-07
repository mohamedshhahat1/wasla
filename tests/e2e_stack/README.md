# The e2e stack

The real Wasla API and worker, with deterministic fakes for every provider, so
the frontend's Playwright journeys (J1–J10 of `FRONTEND_PLAN.md`) - and anybody
who wants the real backend on a laptop - can run with **nothing leaving the
machine**. Built by FE-0 (ADR-191).

> **Test-only. Never deploy `docker-compose.e2e.yml` or its image.** Every
> secret in it is a public test value written into the file on purpose.

## Start, stop, reset

```bash
docker compose -f docker-compose.e2e.yml --profile e2e up -d --build
docker compose -f docker-compose.e2e.yml --profile e2e down -v
```

`down -v` is the reset: the database, media and mailbox volumes go with it. No
reset, seed or test-only route exists on the API.

| Service | Container | Published | What it is |
|---|---|---|---|
| `api` | `wasla-fe-e2e-api` | `127.0.0.1:${WASLA_E2E_API_PORT:-8000}` | the real API (`python -m app.serve`) through the launcher |
| `worker` | `wasla-fe-e2e-worker` | - | the real worker (`app.workers.runner`) through the launcher |
| `mailbox` | `wasla-fe-e2e-mailbox` | `127.0.0.1:${WASLA_E2E_MAILBOX_PORT:-8025}` | every email sent, kept after sending |
| `fakes` | `wasla-fe-e2e-fakes` | `127.0.0.1:${WASLA_E2E_FAKES_PORT:-8026}` | Meta, OpenAI, Paymob and Google stand-ins, and the two pages a browser opens |
| `migrate` | `wasla-fe-e2e-migrate` | - | `alembic upgrade head`, then `seed.py`, then exits |
| `pg`, `redis` | `wasla-fe-e2e-pg`, `-redis` | - | pgvector 16 and Redis 7 |

Every published port is bound to `127.0.0.1`. If the developer stack already
holds port 8000, set `WASLA_E2E_API_PORT`.

## Accounts

| Who | Email | Password | How |
|---|---|---|---|
| Platform owner (J8) | `staff.e2e@example.com` | `e2e-staff-password-NOT-A-SECRET` | `seed.py`, always |
| Demo owner | `demo.owner.e2e@example.com` | `e2e-demo-password-NOT-A-SECRET` | `seed.py --demo` |

The plans come from the migrations (`pro` costs 99 EGP, which J5 buys).
`--demo` builds a verified owner, workspace `e2e-demo`, a WhatsApp number, a
default agent and a knowledge base - for FE-3 / FE-4 development. Run it once
the stack is up:

```bash
docker compose -f docker-compose.e2e.yml --profile e2e exec api python -m tests.e2e_stack.seed --demo
```

## The mailbox

```text
GET    http://127.0.0.1:8025/messages?to=<address>    newest first
DELETE http://127.0.0.1:8025/messages                 empty it
```

Each message: `{to, subject, text, html, links, codes, sent_at}` - `codes` holds
the six-digit verification codes, `links` the invitation and reset links.

## What each fake answers

| Provider | Answer |
|---|---|
| WhatsApp ownership | `tests/fake_ownership.FakeOwnershipVerifier`: every phone number id starting `PNID-E2E` is owned |
| Meta Graph | a send answers `wamid.E2E<n>`; a media id starting `e2e-media` resolves to a 1x1 PNG |
| OpenAI | embeddings from `tests/fake_embeddings.py`; sentiment neutral; the agent replies in the customer's language, calls `request_human_handoff` on "agent" / "موظف" and `search_knowledge` on "price" / "سعر" |
| Paymob | intentions as the suites' double; `http://localhost:8026/paymob/?…` sends the signed callback to the API and redirects to `{APP_PUBLIC_URL}/billing/checkout/return` |
| Google | `http://localhost:8026/google/authorize` redirects to `GOOGLE_REDIRECT_URI` with a code (append `&email=` to choose who); the token is RS256-signed by a key generated at start-up; the binding cookie, PKCE, state and nonce checks are the API's real ones |
| Email | the repository's fake provider, plus the mailbox |

## How nothing leaves the machine

The launcher (`launcher.py`) installs, in the api and worker processes only: a
socket wall refusing every lookup and connect outside the stack's own service
names and private addresses, and a reroute of every request the application's
guarded HTTP transport sends to the fakes service; a provider host the
application resolves to judge a URL resolves to a placeholder address nothing
connects to. Every process also has a dead proxy, and `OPENAI_API_KEY` is a
public dummy (the worker will not run an agent without *a* key). Nothing under
`app/` changes for this stack.

## Signing a forward (FEB-01)

Requests from a frontend tier carry the browser's address, signed with the
stack's `SESSION_FORWARD_SECRET` (`e2e-not-a-secret-0123456789abcdef0123456789abcdef`):

```text
X-Wasla-Client-IP:   203.0.113.7
X-Wasla-Forward-Ts:  <unix seconds>
X-Wasla-Forward-Sig: hex(HMAC-SHA256(secret, "v1\n" + ts + "\n" + ip + "\n" + METHOD + "\n" + path))
```

`path` is the percent-decoded URL path, no query string. Test vectors:
`frontend/packages/api-client/fixtures/forward-signature.v1.json`.

## The smoke walk

```bash
python -m tests.e2e_stack.smoke
```

Walks J1–J10 from the API side with fresh accounts, prints one row per journey
and the signed-forward checks, and exits non-zero if a journey failed. J6 is
`EXPECTED UNTIL FE-2` (two refreshes with one token sign the account out - the
race FE-2's single refresh per browser avoids). J10 reads the media id from the
database because no message carries it (FEB-03).
