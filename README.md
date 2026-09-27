# gmail-automator

[![CI](https://github.com/instax-dutta/gmail-automator/actions/workflows/ci.yml/badge.svg)](https://github.com/instax-dutta/gmail-automator/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)

**Status: alpha.** The API may still change; pin a version if you depend on it.

Self-hosted Gmail gateway for AI agents. Your agent sends email **as you**, through your own Gmail
or Google Workspace account, over MCP or a REST API, while the gateway quietly enforces Gmail's
official sending limits so the account is never locked.

- **MIT licensed, no vendor lock-in, no paid dependency.** SQLite by default; Postgres optional.
- **Two interfaces, one policy.** MCP tools (stdio and Streamable HTTP) and REST `/v1` share the
  same services, so validation, quota, and errors are identical either way.
- **Limits first.** Rolling 24-hour counters with configurable soft limits, per-account pacing, and
  exponential backoff. A send that would exceed a limit is refused *before* Google is called.
- **Tokens encrypted at rest** (AES-256-GCM) and never returned over the API or written to a log.
- **Drains on your schedule.** A durable SQLite-backed queue with leases, so a restart mid-send
  resumes instead of losing the job.

**Documentation:** [Google Cloud setup](docs/google-cloud-setup.md) · [Operations runbook](docs/operations.md) ·
[Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md)

> The GitHub repository is `gmail-automator`; the Python distribution and import package are
> `gmail_automator`, and the CLI is `gmail_automator`. They are the same thing.

## Contents

- [The problem](#the-problem)
- [Requirements](#requirements)
- [Quickstart](#quickstart)
- [Give your agent a key](#give-your-agent-a-key)
- [REST API](#rest-api)
- [MCP](#mcp)
- [How it works](#how-it-works)
- [How limits are enforced](#how-limits-are-enforced)
- [Configuration](#configuration)
- [CLI](#cli)
- [Operator status page](#operator-status-page)
- [Workspace: unattended sending](#workspace-unattended-sending)
- [When not to use this](#when-not-to-use-this)
- [Development](#development)
- [Security](#security)
- [Your responsibilities](#your-responsibilities)
- [Contributing](#contributing)
- [License](#license)

---

## The problem

Handing an agent a Gmail credential means handing it a live sending quota. One careless retry loop
and the account is throttled or locked, and the failure looks like a bug in the agent rather than a
limit. Most existing options make this worse: they ask for full mailbox read access, they hide their
quota assumptions, or they run someone else's infrastructure over your mail.

gmail-automator sits in between. It is a small service you run yourself that holds one narrow credential
(`gmail.send`), accounts for every send before it happens, and refuses the risky one with a clear
error your agent can reason about.

---

## Requirements

| | |
|---|---|
| Python | 3.12 or newer (3.12 and 3.13 tested in CI) |
| Database | SQLite (bundled) or PostgreSQL 14+ |
| Google | A Cloud project with the Gmail API enabled and an OAuth client |
| Access | A Gmail account you own, or a Workspace mailbox with domain-wide delegation |

Nothing else. No Redis, no message broker, no external database to operate.

---

## Quickstart

### Docker (one command)

```bash
cp .env.example .env
$EDITOR .env          # set GMAIL_AUTOMATOR_TOKEN_ENCRYPTION_KEY and the Google OAuth client id/secret
docker compose up -d
```

The gateway listens on `127.0.0.1:8000`. Nothing is exposed to your network until you change the
published address in `docker-compose.yml`.

### From a checkout

```bash
uv sync --extra dev
uv run gmail-automator gen-key          # -> GMAIL_AUTOMATOR_TOKEN_ENCRYPTION_KEY
cp .env.example .env            # fill in the key + Google OAuth credentials
uv run gmail-automator migrate
uv run gmail-automator serve             # or: uv run gmail-automator mcp-stdio
```

### Connect an account and send

```bash
uv run gmail-automator accounts connect          # prints the Google consent URL; open it in a browser
uv run gmail-automator status                    # accounts, remaining capacity, queue depth
uv run gmail-automator keys create agent          # prints an API key, once
uv run gmail-automator send-test someone@example.com
```

---

## Give your agent a key

```bash
uv run gmail-automator keys create my-agent --scopes send,read
# fmg_1a2b3c4d_...            <- shown once
```

The secret goes to stdout and the warning to stderr, so this is safe:

```bash
KEY=$(gmail-automator keys create my-agent --scopes send,read | tail -1)
```

Give the key only the scopes its consumer needs - `send` to send, `read` for status and quota. A key
with `send` alone is refused by `/v1/quota` with `403`, which is the point.

Then either header works:

```bash
curl -s http://localhost:8000/v1/quota -H "authorization: Bearer fmg_..." | jq
```

---

## REST API

Base path `/v1`. Every route requires `Authorization: Bearer <key>` except `/health`.

| Method   | Path                        | Purpose                                            |
|----------|-----------------------------|----------------------------------------------------|
| `GET`    | `/health`                   | Liveness, version, account count, queue depth        |
| `GET`    | `/v1/accounts`              | Connected accounts with status and scopes            |
| `GET`    | `/v1/accounts/{email}`      | One account                                         |
| `DELETE` | `/v1/accounts/{email}`      | Disconnect and delete its tokens                     |
| `GET`    | `/v1/oauth/google/start`    | Google consent URL                                   |
| `GET`    | `/v1/oauth/google/callback` | OAuth redirect target                               |
| `DELETE` | `/v1/oauth/google/{email}`  | Disconnect                                          |
| `GET`    | `/v1/quota`                 | Remaining 24h capacity for every account             |
| `GET`    | `/v1/quota/{email}`         | Remaining 24h capacity for one account               |
| `POST`   | `/v1/send`                  | Send one email, or queue it                          |
| `POST`   | `/v1/send/batch`            | Queue up to 50 emails                                |
| `GET`    | `/v1/send/limits`           | The limits this gateway will actually enforce        |
| `GET`    | `/v1/jobs/{id}`             | Status of one send                                   |
| `GET`    | `/v1/history`               | Recent sends, newest first                           |
| `POST`   | `/v1/drafts`                | Create a draft instead of sending                    |

Interactive docs are at `/docs`.

### Send

```bash
curl -s http://localhost:8000/v1/send \
  -H "authorization: Bearer $KEY" \
  -H 'content-type: application/json' \
  -d '{
        "to": ["someone@example.com"],
        "cc": ["team@example.com"],
        "subject": "Build finished",
        "body": "All green. Logs attached.",
        "wait": true
      }'
```

```json
{
  "job_id": 42,
  "status": "sent",
  "message_id": "18f0a1b2c3d4e5f6",
  "account": "you@gmail.com"
}
```

`wait: true` (the default) blocks until the worker has sent it, so you get the Gmail message id
directly. `wait: false` returns immediately with a job id - use it for bulk work.

### Batch

```bash
curl -s http://localhost:8000/v1/send/batch \
  -H "authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"emails": [{"to": ["a@example.com"], "subject": "1", "body": "..."},
                  {"to": ["b@example.com"], "subject": "2", "body": "..."}],
       "wait": false}'
```

The whole batch is validated and quota-checked **before** anything is queued, so a refusal leaves
the queue untouched. Pacing then spreads the sends `GMAIL_AUTOMATOR_DEFAULT_SEND_INTERVAL_SECONDS` apart.

### Errors

Every error, on every endpoint, has the same shape:

```json
{"error": {"code": "quota_exceeded",
           "message": "daily message soft limit reached for you@gmail.com; 425 of 425 used",
           "details": {"account": "you@gmail.com", "resource": "messages",
                       "used": 425, "soft_limit": 425, "reset_at": "..."}}}
```

Stable codes: `invalid_request`, `unauthorized`, `forbidden`, `account_not_found`, `scope_missing`,
`quota_exceeded`, `queue_full`, `duplicate_request`, `send_failed`, `daily_send_quota_exceeded`,
`attachment_too_large`, `attachment_path_not_allowed`, `crypto_error`, `internal_error`.

---

## MCP

### Streamable HTTP

Served by the same process at `/mcp`, guarded by the same API key:

```json
{
  "mcpServers": {
    "gmail_automator": {
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer fmg_YOUR_KEY_HERE" }
    }
  }
}
```

Clients that read `mcpServers` from a JSON file (Claude Desktop, Cursor, Windsurf, and most others)
want the same shape.

Use `/mcp` **without** a trailing slash. `/mcp/` answers with a 307, which MCP clients do not follow
for POST, so the request fails with an opaque error. This is not a quirk of the URL you type - it is
why the server uses a custom mount instead of a conventional one, noted in
[docs/operations.md](docs/operations.md).

### stdio

For agent clients that spawn a subprocess:

```json
{
  "mcpServers": {
    "gmail_automator": { "command": "gmail_automator", "args": ["mcp-stdio"] }
  }
}
```

The subprocess inherits the environment, so `GMAIL_AUTOMATOR_DATABASE_URL` and
`GMAIL_AUTOMATOR_TOKEN_ENCRYPTION_KEY` must be visible to the client - see
[Configuration](#configuration). If the client does not pass the environment through, wrap the
command: `"command": "sh", "args": ["-c", "GMAIL_AUTOMATOR_DATABASE_URL=... gmail-automator mcp-stdio"]`.

| Tool                      | What it does                                                        |
|---------------------------|---------------------------------------------------------------------|
| `send_email`              | Send or queue one message; returns a message id or a job id        |
| `send_batch`              | Queue up to 50 messages, one result each                            |
| `get_quota_status`        | Messages and recipients left in the 24h window, queue depth         |
| `list_accounts`           | Connected accounts, status, granted scopes                          |
| `start_account_connect`   | Google consent URL for connecting an account                        |
| `disconnect_account`      | Disconnect an account and delete its tokens                         |
| `get_send_history`        | Recent sends with status and error codes                            |
| `get_send_status`         | One job in detail, including the Gmail message id                   |
| `create_draft`            | Create a draft instead of sending, for human review                 |

Tools return structured results. A refused operation comes back as a readable error result whose
text begins with the error code, e.g. `quota_exceeded: daily message soft limit reached...`, so a
model can react rather than seeing a stack trace.

---

## How it works

One send, end to end:

```
agent ──MCP tool / POST /v1/send──▶ SendService
                                     │  validate, build MIME, count recipients
                                     │  check quota: message + recipient budget, 24h window
                                     ▼
                                   QueueService          encrypted body, job id as AAD
                                     ▼
                                   Worker (separate process or thread)
                                     │  lease the job, refresh the token if needed
                                     │  pacing cursor says "not before 18:42:07"
                                     ▼
                                   GmailTransport ──▶ Gmail API
                                     │
                                     ▼
                                   event recorded ──▶ body wiped, history row written
```

The rules that matter:

- **The queue is the database.** A job is a row. Restart the gateway and the next worker picks up
  where the last one stopped, with a lease so two workers never send the same job.
- **Refuse before the network.** Quota, recipient count, and body size are all checked before
  Google is called, so a refusal costs nothing and cannot half-happen.
- **Pacing is scheduled, not slept.** Jobs carry a not-before time, so a worker thread never blocks
  and the schedule survives a restart.
- **One policy, two front doors.** The MCP tools and the REST routes are thin adapters over the same
  services. There is no way to reach a send that skips the quota check.
- **Failures are typed.** Every refusal carries a stable error code, so an agent can react
  (`quota_exceeded`, wait) instead of retrying blindly (`auth_expired`, do not).

---

## How limits are enforced

1. **Soft limits, not hard ones.** The gateway caps each account at
   `GMAIL_AUTOMATOR_DEFAULT_DAILY_MESSAGE_LIMIT x GMAIL_AUTOMATOR_SOFT_LIMIT_RATIO` (default `500 x 0.85 = 425`).
2. **Rolling window.** Counted from `send_jobs` over the last 24 hours - completed sends plus jobs
   already queued. No counter table to drift out of sync.
3. **Refuse before calling Google.** A send that would cross a soft limit is rejected with
   `quota_exceeded` and the numbers involved. Google is never asked.
4. **Pacing.** Jobs are *scheduled* `GMAIL_AUTOMATOR_DEFAULT_SEND_INTERVAL_SECONDS` apart per account, not
   slept on, so a worker thread never blocks and the pacing survives restarts.
5. **Backoff, and knowing when to stop.** `rateLimitExceeded` and 5xx retry with
   `2^(attempt-1) + jitter`, capped, honouring `Retry-After`, up to `GMAIL_AUTOMATOR_MAX_ATTEMPTS`. A
   *daily* quota rejection is terminal for the job: Google documents that it can stay in force for
   hours, so retrying would only spend more of the remaining budget.

Every limit is configuration, not a constant. Gmail changes its published numbers; this file and
`docs/google-cloud-setup.md` record the values the defaults were chosen from.

---

## When not to use this

- **You need to read mail.** gmail-automator requests `gmail.send` and nothing else. It will not become a
  mail client, and it deliberately cannot read your inbox.
- **You want someone else's infrastructure.** There is no hosted version. That is the trade: the
  credential never leaves your host.
- **You are sending bulk or unsolicited mail.** gmail-automator stays well inside Gmail's limits as a
  safety margin. It is not permission, and Gmail's Terms of Service still apply.
- **You need multi-tenant isolation.** The auth model assumes one operator issuing keys to their own
  agents. It is not a public SaaS backend.
- **You need guaranteed delivery.** A queued job is retried with backoff, but there is no delivery
  receipt beyond Gmail's own message id.

---

## Configuration

Every setting is an environment variable prefixed `GMAIL_AUTOMATOR_`; see
[`.env.example`](.env.example) for the annotated list. The ones that matter most:

| Variable                                | Default              | Meaning                                            |
|-----------------------------------------|----------------------|----------------------------------------------------|
| `GMAIL_AUTOMATOR_TOKEN_ENCRYPTION_KEY`          | *required*           | 32 bytes, base64. Losing it makes stored tokens unreadable |
| `GMAIL_AUTOMATOR_DATABASE_URL`                  | `sqlite:///./data/…` | SQLite or `postgresql+psycopg://…`                  |
| `GMAIL_AUTOMATOR_AUTH_MODE`                     | `api_key`            | `none` for loopback-only local use                  |
| `GMAIL_AUTOMATOR_SOFT_LIMIT_RATIO`              | `0.85`               | Fraction of the hard limit the gateway will use     |
| `GMAIL_AUTOMATOR_DEFAULT_SEND_INTERVAL_SECONDS` | `2.0`                | Per-account pacing interval                        |
| `GMAIL_AUTOMATOR_HOST` / `GMAIL_AUTOMATOR_PORT`         | `127.0.0.1` / `8000` | Bind address                                       |
| `GMAIL_AUTOMATOR_WORKER_ENABLED`                | `true`               | Run the send worker in this process                 |
| `GMAIL_AUTOMATOR_ATTACHMENTS_ENABLED`           | `false`              | Allow attachments                                  |
| `GMAIL_AUTOMATOR_ATTACHMENT_ALLOWED_DIRS`       | *empty*              | Directories path attachments may be read from       |

---

## CLI

```
gmail-automator serve                 # HTTP gateway: REST + MCP
gmail-automator mcp-stdio             # MCP over stdio
gmail-automator migrate               # apply database migrations
gmail-automator gen-key               # generate a token encryption key
gmail-automator status [--json]       # accounts, remaining capacity, queue depth
gmail-automator send-test <recipient> # send a real message end to end
gmail-automator rotate-keys [--dry-run]   # re-encrypt stored tokens under a new key
gmail-automator accounts list|connect|disconnect
gmail-automator keys create|list|revoke
gmail-automator purge-history [--days N]
```

`status --json` and `keys list` write JSON to **stdout**; logs and human messages go to **stderr**,
so both are safe to pipe.

---

## Operator status page

`GET /status` renders the same numbers as `gmail-automator status` for a browser: accounts, quota used
against the soft limit, queue depth, and the next send time. Server-rendered, no JavaScript, and no
external assets, so it works on a host with no internet access.

## Workspace: unattended sending

With a Workspace admin's domain-wide delegation grant, gmail-automator can send as a Workspace mailbox
unattended - no interactive consent, and no refresh token stored at all:

```dotenv
GMAIL_AUTOMATOR_SERVICE_ACCOUNT_KEY_FILE=/run/secrets/gmail_automator-sa.json
GMAIL_AUTOMATOR_SERVICE_ACCOUNT_SUBJECT=agent@acme.co
```

See [`docs/google-cloud-setup.md`](docs/google-cloud-setup.md#service-accounts-workspace-only) for
the admin-side steps. Everything else - least-privilege scopes, encrypted storage, quota
enforcement - is unchanged.

---

## Development

```bash
uv sync --extra dev
uv run pytest -q                    # unit + integration, no sockets
uv run pytest -m e2e                # end-to-end over real localhost sockets
uv run ruff check . && uv run ruff format --check . && uv run mypy src
./scripts/docker-smoke.sh           # build the image and assert the deployed behaviour
```

The architecture is enforced by tests rather than convention: `tests/unit/test_import_boundaries.py`
fails the build if anything outside `gmail_automator/gmail/client.py` imports `googleapiclient`, if anything
outside `gmail_automator/db.py` creates an engine, or if a service calls `datetime.now()` instead of taking
an injected `Clock`. Google is never contacted in the default test run - `tests/support/fake_gmail_app.py`
is an in-process fake, and `tests/support/sync_asgi.py` adapts it for both `httpx` and `httplib2`
without binding a port.

---

## Security

- OAuth tokens are AES-256-GCM encrypted at rest, bound to the account address as AAD. They are
  never returned by the API and never written to a log.
- Scopes are least-privilege: `gmail.send` plus `openid` and `email`. The gateway never asks for
  mailbox read access.
- Email bodies are held only while a job is in flight and are wiped on a terminal state.
- API keys are stored as a prefix plus a SHA-256 hash and compared with `hmac.compare_digest`.
- The log pipeline redacts secret-looking keys recursively, and the container runs as uid 10001
  with a single writable volume.

## Your responsibilities

You remain responsible for the Gmail Terms of Service, recipient consent, and anti-spam rules.
gmail-automator does not hide or bypass Gmail's limits; it stays well inside them so your account is not at
risk. Do not use it for bulk or unsolicited mail.

---

## Contributing

Bug reports and pull requests are welcome. Start with
[`CONTRIBUTING.md`](CONTRIBUTING.md); the short version is `make check` plus
`uv run pytest -m e2e` must pass, and commits follow
[Conventional Commits](https://www.conventionalcommits.org/).

Please read [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) before participating.

## License

MIT - see [`LICENSE`](LICENSE).
