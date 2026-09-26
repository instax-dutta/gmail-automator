# Operations

Runbook for running Fmaiily day to day. Setup and Google Cloud steps are in
[`google-cloud-setup.md`](google-cloud-setup.md); this file covers what to watch, what to do when
something goes wrong, and how the system behaves under load.

---

## Health and status

| Question | Command |
|---|---|
| Is the process up? | `GET /health` (unauthenticated) or `docker compose ps` |
| What can it still send? | `fmaiily status`, or `GET /v1/quota` |
| What is queued? | `fmaiily status --json` → `queue_depth` |
| What happened recently? | `GET /v1/history?limit=50` |
| What is the per-job story? | `GET /v1/jobs/{id}` |

`/health` is safe to expose: it returns a version, a dialect, a count of connected accounts, and a
queue depth - no addresses, no tokens.

---

## Metrics

`GET /metrics` serves Prometheus exposition from a registry owned by the process, so several
gateways on one host do not collide.

| Metric | Type | Labels | Read it for |
|---|---|---|---|
| `fmaiily_sends_total` | counter | `account`, `outcome`, `source` | throughput and failure ratio |
| `fmaiily_send_failures_total` | counter | `account`, `error_code` | which failure dominates |
| `fmaiily_send_duration_seconds` | histogram | `account` | Gmail latency, p50/p95 |
| `fmaiily_queue_depth` | gauge | - | backlog growth |
| `fmaiily_quota_remaining` | gauge | `account`, `resource` | how close to the soft limit |
| `fmaiily_tokens_refreshed_total` | counter | `account`, `result` | refresh health (`cached` dominates) |
| `fmaiily_worker_iterations_total` | counter | `action` | worker liveness |
| `fmaiily_http_requests_total` | counter | `method`, `path`, `status` | API traffic |

HTTP metrics are labelled by **route template** (`/v1/jobs/{job_id}`), never by a concrete path, so
a job id or an email address cannot become an unbounded label.

Alerts worth having:

```promql
# queue not draining
rate(fmaiily_queue_depth[15m]) > 0 and fmaiily_queue_depth > 50

# a failure mode is dominating
topk(3, rate(fmaiily_send_failures_total[10m]))

# auth is broken: a rising share of refreshes failing
rate(fmaiily_tokens_refreshed_total{result="error"}[15m]) > 0

# quota pressure
fmaiily_quota_remaining{resource="messages"} < 25
```

---

## Logs

Structured JSON on stdout (stderr for the CLI), one object per line, with a secret-redacting
processor that walks nested structures. Pass secrets as keyword arguments - `access_token=...` - not
interpolated into the event message, because redaction is key-based and a message string is
treated as data.

Events worth alerting on:

| Event | Meaning |
|---|---|
| `send_failed` | a job reached a terminal failure; `error_code` says why |
| `send_retry_scheduled` | a transient failure; `delay_seconds` and `attempt` show the backoff |
| `account_paused` | Google throttled this user; the account cursor moved out |
| `recovered_expired_leases` | a worker died mid-send and its jobs were requeued |
| `maintenance` | payloads swept or history purged |
| `auth_disabled_on_public_interface` | `FMAIILY_AUTH_MODE=none` on a non-loopback bind |

Set `FMAIILY_LOG_LEVEL=DEBUG` only while diagnosing: it is verbose and still redacted.

---

## Error codes and what to do

| Code | HTTP | Cause | Action |
|---|---|---|---|
| `quota_exceeded` | 429 | the send would cross a soft limit | wait for `reset_at`, or raise the ratio deliberately |
| `daily_send_quota_exceeded` | 502 | Google rejected the send for the whole day | stop sending; the job is terminal by design |
| `queue_full` | 503 | queue depth or schedule horizon reached | let the worker drain; check `queue_depth` |
| `gmail_rate_limited` | - | per-user throttle | automatic backoff; the account is paused |
| `auth_expired` | - | token rejected, refreshed once | check `last_refresh_error` if it repeats |
| `send_failed` (token refresh) | 502 | refresh token revoked or expired | reconnect the account |
| `forbidden` | 403 | account revoked, or the key lacks the scope | reconnect, or issue a wider key |
| `scope_missing` | 403 | the operation needs a scope the account lacks | reconnect with the extra scope |
| `duplicate_request` | 409 | idempotency key reused with a different body | use a new key |
| `attachment_too_large` | 413 | over `FMAIILY_ATTACHMENT_MAX_BYTES` | shrink the file |
| `crypto_error` | 500 | a stored value cannot be decrypted | `FMAIILY_TOKEN_ENCRYPTION_KEY` changed: reconnect accounts |

### An account is stuck in `error`

The refresh token is bad. The previous access token is preserved, so nothing is lost until it also
expires. Fix:

```bash
fmaiily accounts disconnect you@gmail.com
fmaiily accounts connect          # open the printed URL and grant access again
```

### An account is stuck in `revoked`

Expected after `DELETE /v1/accounts/{email}` or `fmaiily accounts disconnect`. Queued jobs for it
fail with `send_failed` and the tokens are already deleted.

### Quota is exhausted but you believe it should not be

`GET /v1/quota/{account}` reports `used`, `soft_limit`, and `reset_at`. The window is rolling 24
hours, not a calendar day, so a burst at 09:00 and another at 09:00 tomorrow both count. In-flight
jobs are counted as reserved, which is why `used` can exceed what has actually been delivered.

---

## Retention and disk

| What | Setting | Default | Wiped by |
|---|---|---|---|
| Queued MIME payload | `FMAIILY_PAYLOAD_RETENTION_HOURS` | 24 h | worker `maintenance` every 50 iterations |
| Sent payload | `FMAIILY_KEEP_SENT_PAYLOADS` | `false` | immediately on a terminal state |
| Send history | `FMAIILY_HISTORY_RETENTION_DAYS` | 30 d | worker `maintenance`; `fmaiily purge-history` |

With `KEEP_SENT_PAYLOADS=false` no email body outlives its send. The database therefore grows with
job *metadata*, not content. On a busy gateway, run `fmaiily purge-history` from cron if you care
about the file size:

```cron
17 4 * * * cd /opt/fmaiily && docker compose exec -T fmaiily fmaiily purge-history
```

If you need bodies for debugging, set `FMAIILY_KEEP_SENT_PAYLOADS=true` temporarily, understand that
you are now storing message content at rest, and turn it back off.

---

## Backups

Two things matter:

1. **The database** - holds encrypted tokens, queue state, and history.
2. **`FMAIILY_TOKEN_ENCRYPTION_KEY`** - without it the tokens in that backup are unreadable.

```bash
# SQLite - the runtime image has no sqlite3 CLI, but it does have Python's stdlib driver.
# `sqlite3.Connection.backup` is a consistent online copy, unlike `cp` on a live file.
docker compose exec -T fmaiily python -c \
  "import sqlite3; s=sqlite3.connect('/data/fmaiily.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d); d.close(); s.close()"
docker compose cp fmaiily:/data/backup.db "./fmaiily-$(date +%F).db"

# Postgres
docker compose exec -T fmaiily pg_dump -U "$PGUSER" fmaiily > "fmaiily-$(date +%F).sql"
```

Test a restore periodically: run the container against the restored file, connect an account, and
send one message.

---

## Scaling

### One process is enough for most deployments

A single worker comfortably handles hundreds of messages a day, and the *pacing* - not CPU - is the
limit: `FMAIILY_DEFAULT_SEND_INTERVAL_SECONDS=2.0` allows 1,800 messages per account per day while
staying far inside Gmail's cap.

### More than one process

The queue is designed for it. `claim_next` is a single `UPDATE ... WHERE id = (SELECT ... LIMIT 1)
... RETURNING id`, so two workers never take the same job, and `requeue_expired_leases` recovers a
job whose worker died. To run two:

- point both at the same Postgres database (SQLite works for two processes on one host with WAL, but
  Postgres is the supported answer for anything else);
- give each a distinct `FMAIILY_WORKER_ID` so a stuck job is attributable;
- scrape `/metrics` from both - each process has its own registry.

**Not shared across processes:** the per-key request rate limiter is in-memory, so with N processes a
key gets N times its configured limit. Put a shared limiter at the ingress if that matters.

### Vertical instead of horizontal

For very high volume, raise `FMAIILY_MAX_ATTEMPTS` and `FMAIILY_BACKOFF_MAX_SECONDS` so transient
failures wait longer instead of cycling, and lower `FMAIILY_WORKER_POLL_INTERVAL_SECONDS` so a
picked-up job is noticed sooner.

---

## Postgres

```bash
uv sync --extra postgres
export FMAIILY_DATABASE_URL="postgresql+psycopg://user:pass@host:5432/fmaiily"
fmaiily migrate
```

The suite covers the backend-specific behaviour:

```bash
docker run -d --name fmaiily-pg -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=fmaiily \
  -p 127.0.0.1:5432:5432 postgres:17-alpine
FMAIILY_TEST_POSTGRES_URL="postgresql+psycopg://postgres:pw@127.0.0.1:5432/fmaiily" \
  uv run pytest -m postgres
```

What differs from SQLite and is therefore tested: `RETURNING` claim semantics under real row locks,
concurrent claim and enqueue from several threads, and timestamp round-tripping.

---

## Migrations

```bash
fmaiily migrate            # upgrade to head
alembic downgrade -1       # one step back
alembic history            # what exists
```

Two rules:

- Take a backup first. `downgrade` for `0002` drops `send_jobs.request_hash`, which silently
  disables idempotent replay.
- Never reset the schema with `Base.metadata.drop_all` alone: it does not touch `alembic_version`,
  so the next `upgrade head` is a silent no-op and the tables stay missing. Drop the stamp too
  (the Postgres fixture shows the correct sequence).

---

## Security checklist

- [ ] `FMAIILY_TOKEN_ENCRYPTION_KEY` is 32 random bytes, stored in a secret manager, backed up.
- [ ] `FMAIILY_AUTH_MODE=api_key` (not `none`).
- [ ] `FMAIILY_BOOTSTRAP_ADMIN_KEY` removed once real keys exist.
- [ ] Every key has the narrowest scopes and account allow-list that works.
- [ ] The port is bound to loopback, or behind a TLS-terminating proxy.
- [ ] `/metrics` is not publicly reachable (it contains account addresses as labels).
- [ ] The container runs as uid 10001 with a single writable volume (`docker-smoke.sh` asserts it).
- [ ] Log shipping retains JSON, not rendered console output.
- [ ] `FMAIILY_KEEP_SENT_PAYLOADS=false` unless message bodies at rest are acceptable to you.

---

## Verifying a deployment

```bash
./scripts/docker-smoke.sh
```

Builds the image, starts it with a throwaway key, and asserts: health responds, every `/v1` route
and `/mcp` reject an unauthenticated request, the authenticated surface works, a malformed body
returns 400 and an unknown path 404, the container runs as uid 10001, and no plaintext Google token
appears in the database or the image.
