# gmail-automator

[![CI](https://github.com/instax-dutta/gmail-automator/actions/workflows/ci.yml/badge.svg)](https://github.com/instax-dutta/gmail-automator/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)

**Let your AI agent send email as you - without it ever getting your account locked.**

gmail-automator is a small service you run yourself. Your agent talks to it over MCP (or a REST
API); it holds the Google credential so your agent never does, and it accounts for every send
against Gmail's real sending limit *before* contacting Google.

> **Alpha.** Pre-1.0: the API can still change. Pin a version if you depend on it. Nothing here is
> load-bearing for your mail until you point an agent at it.

- **MIT, no vendor lock-in, no paid tier.** SQLite by default, PostgreSQL optional. Nothing to sign
  up for.
- **The agent never sees your refresh token.** It is AES-256-GCM encrypted at rest and bound to the
  account address, so it cannot be moved between accounts and never appears in a log.
- **Send-only by default.** Connect with `gmail.send` and the read, reply, and label tools refuse
  with `scope_missing` rather than quietly doing nothing. You widen the grant, or you don't.
- **A restart is not a lost email.** The queue is a table with leases; the next worker resumes.

**Docs:** [Google Cloud setup](docs/google-cloud-setup.md) · [Operations runbook](docs/operations.md) ·
[Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md)

---

## The failure it prevents

Give an agent a Gmail credential and you have also given it a live sending quota. Agents retry. A
retry loop looks like a rate limit, then like a lockout, and Gmail's recovery is measured in hours or
days. From inside the agent it just looks like the tool is broken.

The usual ways out are worse than the disease: hand the agent full mailbox read scope "just in case",
or route mail through someone else's infrastructure. So gmail-automator takes the narrow credential,
keeps it, and makes the dangerous send impossible rather than merely discouraged.

---

## What it is, and what it is not

**Not** a mail client, and **not** a hosted service. It is a quota-aware send proxy with a durable
queue in front of the Gmail API. Every path into it - MCP tool or REST route - goes through the same
validation, the same quota check, and the same errors, so there is no side door around the policy.

---

## Why not just let the agent call Gmail directly?

For a one-off script, do that. This exists for the case where the agent is autonomous enough that
"the agent overshot the quota" is a real possibility:

| | direct Gmail API | gmail-automator |
|---|---|---|
| Quota | the agent discovers it by hitting it | refused before the call, with the numbers |
| OAuth token | held by the agent | held by the gateway, encrypted |
| Process restart | in-flight sends are lost | a queued job is a row; the next worker sends it |
| Blast radius | all the scopes you granted | one scope by default, per-key on top |
| Agent-facing errors | Google's error strings | stable codes like `quota_exceeded` and the budget left |

---

## Quickstart

Five steps, and step 5 is a real email in a real inbox. Budget about ten minutes; most of it is
Google's consent screen.

### 1. Run it

```bash
git clone https://github.com/instax-dutta/gmail-automator
cd gmail-automator
cp .env.example .env
docker compose up -d
```

It listens on `127.0.0.1:8000` and nothing else. No Redis, no broker, no database to operate.

<details>
<summary>Prefer a checkout, or a remote host?</summary>

```bash
uv sync --extra dev
uv run gmail-automator gen-key        # -> GMAIL_AUTOMATOR_TOKEN_ENCRYPTION_KEY
cp .env.example .env                  # fill in the key and your Google OAuth client
uv run gmail-automator migrate
uv run gmail-automator serve          # or: gmail-automator mcp-stdio
```

</details>

### 2. Create a Google OAuth client

You need a Cloud project with the Gmail API enabled, then one OAuth client of type **Web
application**. Register this redirect URI exactly:

```
http://127.0.0.1:8000/v1/oauth/google/callback
```

Put the client id and secret in `.env`. Step-by-step, including the part where Google says the app is
unverified and you click through it: [`docs/google-cloud-setup.md`](docs/google-cloud-setup.md).

### 3. Connect a mailbox

```bash
docker compose exec gmail-automator gmail-automator accounts connect
```

It prints a consent URL. Open it, approve the scope, and the tokens are stored encrypted. To connect a
second mailbox, or to change the scopes later, run it again - Google adds the new permission to the
existing grant rather than resetting it.

### 4. Mint a key for your agent

```bash
KEY=$(docker compose exec -T gmail-automator gmail-automator keys create my-agent --scopes send,read | tail -1)
```

Shown once. The secret goes to stdout and the warning to stderr, so the pipe above is safe. Give each
consumer its own key and only the scopes it needs; a key with `send` alone is refused by
`/v1/quota` with `403`, which is the point.

### 5. Send a real message

```bash
docker compose exec gmail-automator gmail-automator send-test you@gmail.com
```

That is a genuine send through the whole path: quota check, queue, worker, Gmail. If it arrives, the
hard part is done.

---

## Point your agent at it

This is the part that makes it useful. MCP (the Model Context Protocol) is how agent tools are
exposed over HTTP, so any MCP client can drive this.

**Streamable HTTP** - same process as the API, guarded by the same key:

```json
{
  "mcpServers": {
    "gmail": {
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer fmg_YOUR_KEY_HERE" }
    }
  }
}
```

Drop that into the MCP config your client reads (Claude Desktop, Cursor, Windsurf, OpenCode, and most
others use this shape). Two things that will otherwise cost you an hour:

- Use `/mcp` **without** a trailing slash. `/mcp/` answers `307`, which MCP clients do not follow for
  POST, so it fails with an opaque error.
- If your client runs on another machine, change the host to wherever the gateway is bound.

**stdio** - for clients that spawn a subprocess instead:

```json
{
  "mcpServers": {
    "gmail": { "command": "gmail-automator", "args": ["mcp-stdio"] }
  }
}
```

The subprocess inherits the environment, so `GMAIL_AUTOMATOR_DATABASE_URL` and
`GMAIL_AUTOMATOR_TOKEN_ENCRYPTION_KEY` must be visible to it. If your client does not pass the
environment through, wrap it:
`"command": "sh", "args": ["-c", "GMAIL_AUTOMATOR_DATABASE_URL=... gmail-automator mcp-stdio"]`.

---

## What your agent can do

Fourteen tools. Each states its own preconditions, and a refusal comes back as a readable result
rather than a stack trace - so a model can react instead of retrying blindly.

**Send**

| Tool | Does |
|---|---|
| `send_email` | Send or queue one message; returns a Gmail message id or a job id |
| `send_batch` | Up to 50 messages, all-or-nothing |
| `create_draft` | Leave it in Gmail for a human to review instead of sending |

**Read and organise** - need a scope you have to grant

| Tool | Does |
|---|---|
| `list_messages` | Search with Gmail's own syntax: `from:`, `newer_than:7d`, `is:unread` |
| `read_message` | Decoded headers, body, and the threading headers. Reads HTML-only mail too |
| `reply` | Replies so it threads in Gmail; `draft: true` to review first |
| `modify_message` | Read/unread, star, archive, trash, custom labels |
| `list_labels` | Every label with counts, so nothing gets filed under a guess |

**Operate**

| Tool | Does |
|---|---|
| `get_quota_status` | Budget left, when it frees up, queue depth |
| `list_accounts` | Connected accounts with the scopes each actually holds |
| `get_send_status` | One job in detail, message id or error code |
| `get_send_history` | Recent sends, newest first |
| `start_account_connect` | Google consent URL for a new mailbox |
| `disconnect_account` | Revoke and delete that mailbox's tokens |

The agent does not need to know your limits in advance. Every send response carries
`messages_remaining`, every refusal arrives with the numbers, and `get_quota_status` is there for
planning. Limits live in the gateway, so the numbers reach the model from one source and cannot go
stale.

There is no permanent delete. `modify_message` can trash, and that is all.

---

## REST API

Base path `/v1`. Everything requires `Authorization: Bearer <key>` except `/health`, `/status`,
`/metrics`, and the Google OAuth callback - the last because a browser redirect cannot carry a header.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness, version, account count, queue depth |
| `GET` | `/status` | Server-rendered operator dashboard |
| `GET` | `/metrics` | Prometheus |
| `GET` | `/v1/accounts` | Connected accounts, status, granted scopes |
| `GET` | `/v1/accounts/{account}` | One account |
| `DELETE` | `/v1/accounts/{account}` | Disconnect and delete its tokens |
| `GET` | `/v1/oauth/google/start` | Google consent URL |
| `GET` | `/v1/oauth/google/callback` | OAuth redirect target |
| `DELETE` | `/v1/oauth/google/{account}` | Disconnect |
| `GET` | `/v1/quota` | Remaining 24h capacity, all accounts |
| `GET` | `/v1/quota/{account}` | Remaining 24h capacity, one account |
| `POST` | `/v1/send` | Send one email, or queue it |
| `POST` | `/v1/send/batch` | Queue up to 50 emails |
| `GET` | `/v1/send/limits` | The limits this gateway will actually enforce |
| `GET` | `/v1/jobs/{id}` | Status of one send |
| `GET` | `/v1/history` | Recent sends, newest first |
| `POST` | `/v1/drafts` | Create a draft instead of sending |
| `GET` | `/v1/mailbox/messages` | Search the mailbox |
| `GET` | `/v1/mailbox/messages/{id}` | Read one message |
| `GET` | `/v1/mailbox/labels` | Every label, system and custom |
| `POST` | `/v1/mailbox/messages/{id}/labels` | Change a message's labels |
| `POST` | `/v1/reply` | Reply, or draft the reply, threaded correctly |

Interactive docs at `/docs`.

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
  "account": "you@gmail.com",
  "messages_remaining": 424
}
```

`wait: true` (the default) blocks until the worker has sent it, so you get the Gmail message id
directly. `wait: false` returns a job id immediately - use it for bulk work, then poll
`/v1/jobs/{id}` or ask the agent for `get_send_status`.

### Batch

```bash
curl -s http://localhost:8000/v1/send/batch \
  -H "authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"emails": [{"to": ["a@example.com"], "subject": "1", "body": "..."},
                  {"to": ["b@example.com"], "subject": "2", "body": "..."}],
       "wait": false}'
```

The whole batch is validated and quota-checked **before** anything is queued, so a refusal leaves the
queue untouched. Pacing then spreads the sends
`GMAIL_AUTOMATOR_DEFAULT_SEND_INTERVAL_SECONDS` apart.

### Errors

Every error, on every endpoint, has the same shape - which is what lets an agent parse it:

```json
{"error": {"code": "quota_exceeded",
           "message": "daily message soft limit reached for you@gmail.com; 425 of 425 used",
           "details": {"account": "you@gmail.com", "resource": "messages",
                       "used": 425, "soft_limit": 425, "reset_at": "..."}}}
```

`invalid_request`, `unauthorized`, `forbidden`, `account_not_found`, `scope_missing`,
`quota_exceeded`, `queue_full`, `duplicate_request`, `send_failed`,
`daily_send_quota_exceeded`, `attachment_too_large`, `attachment_path_not_allowed`, `crypto_error`,
`internal_error`.

---

## How the limit actually works

Gmail allows roughly 500 messages a day on a personal account, 2,000 on Workspace, counted over a
rolling 24 hours. Those are Google's numbers, not constants here.

1. **It stops at 85%, not 100%.** `500 x 0.85 = 425`. The remaining 15% is headroom for sends you make
   by hand, and for the traffic a human generates without the gateway noticing.
2. **The window rolls.** Counted from the job table over the last 24 hours - completed sends plus
   jobs already queued. There is no counter table to drift.
3. **The refusal is free.** A send that would cross the line is rejected *before* Google is called,
   with the numbers in the error. Your agent gets a decision to make, not a lockout to recover from.
4. **Pacing is scheduled, not slept.** Jobs carry a not-before time 2s apart per account, so a worker
   thread never blocks and the schedule survives a restart.
5. **Backoff, and knowing when to stop.** `rateLimitExceeded` and 5xx retry with
   `2^(attempt-1) + jitter`, capped, honouring `Retry-After`, up to
   `GMAIL_AUTOMATOR_MAX_ATTEMPTS`. A *daily* rejection is terminal on purpose: Google documents that
   it can stay in force for hours, so retrying would only spend more of what is left.

Every one of these is configuration, and
[`docs/google-cloud-setup.md`](docs/google-cloud-setup.md) records the published figures the defaults
came from.

---

## Not for you if

Being explicit here saves you an afternoon:

- **You only want to send.** That is the recommended setup, and the read, reply, and label tools will
  refuse. Widen `GMAIL_AUTOMATOR_OAUTH_SCOPES` only if you actually want an agent inside your mailbox.
- **You want someone else's infrastructure.** There is no hosted version. That is the trade: the
  credential never leaves your host.
- **You are sending bulk or unsolicited mail.** This stays well inside Gmail's limits as a safety
  margin. It is not permission.
- **You need multi-tenant isolation.** The auth model assumes one operator issuing keys to their own
  agents. It is not a public SaaS backend.
- **You need guaranteed delivery or a receipt.** A queued job is retried with backoff, and the
  confirmation is Gmail's own message id. Nothing more.

---

## Configuration

Every setting is an environment variable prefixed `GMAIL_AUTOMATOR_`;
[`.env.example`](.env.example) is the annotated list. These are the ones that matter first:

| Variable | Default | Meaning |
|---|---|---|
| `GMAIL_AUTOMATOR_TOKEN_ENCRYPTION_KEY` | *required* | 32 bytes, base64. Lose it and stored tokens are unreadable |
| `GMAIL_AUTOMATOR_DATABASE_URL` | `sqlite:///./data/…` | SQLite, or `postgresql+psycopg://…` |
| `GMAIL_AUTOMATOR_AUTH_MODE` | `api_key` | `none` for loopback-only local use |
| `GMAIL_AUTOMATOR_SOOGLE_OAUTH_SCOPES` | `gmail.send,openid,email` | Widen to `gmail.modify` for read/reply/organise |
| `GMAIL_AUTOMATOR_SOFT_LIMIT_RATIO` | `0.85` | Fraction of the hard limit the gateway will use |
| `GMAIL_AUTOMATOR_DEFAULT_SEND_INTERVAL_SECONDS` | `2.0` | Per-account pacing interval |
| `GMAIL_AUTOMATOR_HOST` / `GMAIL_AUTOMATOR_PORT` | `127.0.0.1` / `8000` | Bind address |
| `GMAIL_AUTOMATOR_WORKER_ENABLED` | `true` | Run the send worker in this process |
| `GMAIL_AUTOMATOR_ATTACHMENTS_ENABLED` | `false` | Allow attachments |
| `GMAIL_AUTOMATOR_ATTACHMENT_ALLOWED_DIRS` | *empty* | Directories path attachments may be read from |

---

## CLI

```
gmail-automator serve                    # HTTP gateway: REST + MCP
gmail-automator mcp-stdio                # MCP over stdio
gmail-automator migrate                  # apply database migrations
gmail-automator gen-key                  # generate a token encryption key
gmail-automator status [--json]          # accounts, remaining capacity, queue depth
gmail-automator send-test <recipient>    # send a real message end to end
gmail-automator rotate-keys [--dry-run]  # re-encrypt stored tokens under a new key
gmail-automator accounts list|connect|disconnect
gmail-automator keys create|list|revoke
gmail-automator purge-history [--days N]
```

`status --json` and `keys list` write JSON to **stdout**; logs and human messages go to **stderr**, so
both are safe to pipe.

`rotate-keys` moves stored tokens onto a new encryption key in three steps - set the new key plus
`GMAIL_AUTOMATOR_TOKEN_ENCRYPTION_KEY_OLD`, run the command, drop the old key. The runbook has the
order and the failure modes: [`docs/operations.md`](docs/operations.md).

---

## Operator status page

`GET /status` renders what `gmail-automator status` prints: accounts, quota used against the soft
limit, queue depth, and the next send time. Server-rendered, no JavaScript, no external assets, so it
works on a host with no internet access.

---

## Workspace: unattended sending

With a Workspace admin's domain-wide delegation grant, it can send as a Workspace mailbox with no
interactive consent - and no refresh token stored at all:

```dotenv
GMAIL_AUTOMATOR_SERVICE_ACCOUNT_KEY_FILE=/run/secrets/sa.json
GMAIL_AUTOMATOR_SERVICE_ACCOUNT_SUBJECT=agent@acme.co
```

The admin-side steps are in
[`docs/google-cloud-setup.md`](docs/google-cloud-setup.md#service-accounts-workspace-only). Limits,
pacing, and encryption work exactly as they do for an OAuth account.

---

## Security

- OAuth tokens are AES-256-GCM encrypted at rest, bound to the account address as associated data, so
  a ciphertext cannot be moved to another account and still decrypt. Never returned by the API,
  never logged.
- **Scopes are least-privilege and yours to widen.** The default is `gmail.send` plus `openid` and
  `email`: an account connected that way cannot be read, and the read, reply, and label tools refuse
  with `scope_missing` rather than degrading. Reading and replying need `gmail.modify` (or
  `gmail.readonly` for reading alone) added to `GMAIL_AUTOMATOR_OAUTH_SCOPES`, then a reconnect.
  `gmail.modify` also grants reading message bodies - widening it is a real change in what the
  gateway can reach, not a formality.
- API keys are stored as a prefix plus a SHA-256 hash, compared with `hmac.compare_digest`, and
  shown exactly once.
- Email bodies live only while a job is in flight and are wiped on a terminal state.
- The log pipeline redacts secret-looking keys recursively, and a test fails the build if a
  credential-shaped literal ever reaches the repository.
- The container runs as a non-root uid with a single writable volume, and there is no permanent
  delete on messages.

---

## Your responsibilities

You remain responsible for the Gmail Terms of Service, recipient consent, and anti-spam rules. This
does not hide or bypass Gmail's limits; it stays well inside them so your account is not at risk. Do
not use it for bulk or unsolicited mail.

---

## Development

```bash
uv sync --extra dev
uv run pytest -q                    # unit + integration, no sockets
uv run pytest -m e2e                # end-to-end over real localhost sockets
uv run ruff check . && uv run ruff format --check . && uv run mypy src
./scripts/docker-smoke.sh           # build the image and assert the deployed behaviour
```

The architecture is enforced by tests rather than convention. `tests/unit/test_import_boundaries.py`
fails the build if anything outside `gmail_automator/gmail/client.py` imports `googleapiclient`, if
anything outside `gmail_automator/db.py` creates an engine, or if a service calls `datetime.now()`
instead of taking an injected `Clock`. Google is never contacted in the default test run:
`tests/support/fake_gmail_app.py` is an in-process fake of the Gmail and OAuth endpoints.

---

## Contributing

Bug reports and pull requests are welcome. Start with [`CONTRIBUTING.md`](CONTRIBUTING.md); the short
version is `make check` and `uv run pytest -m e2e` must pass, and commits follow
[Conventional Commits](https://www.conventionalcommits.org/). Please read
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) first.

## License

MIT - see [`LICENSE`](LICENSE).
