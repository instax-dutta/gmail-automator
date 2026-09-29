# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). gmail-automator is pre-1.0, so a minor bump may
still contain breaking changes; the "Unreleased" section is the authoritative list of what is
already merged.

## [Unreleased]

### Added

- Mailbox read, reply, and organisation, so an agent can manage a mailbox rather than only fire
  off messages. Six new tools on both surfaces: `list_messages` (Gmail search syntax passed
  through), `read_message` (decoded headers, `text/plain` preferred over HTML, RFC 2047 unpacked),
  `reply`, `draft` reply, `modify_message` (read/unread, star, archive, trash, custom labels), and
  `list_labels`. REST parity: `/v1/mailbox/*` and `/v1/reply`.
- Replying gets the threading right: the recipient, subject, `In-Reply-To`, the whole `References`
  chain, and Gmail's `threadId` are all derived from the original, so a caller supplies only a
  body. A reply that merely *sent* while threading wrongly would have been invisible, so the
  headers are asserted directly.
- `JobStatusResponse` and history rows now carry `message_id` and `thread_id`, so an agent can find
  a reply target without a second lookup.
- Read and organise are gated on scope the operator grants deliberately. The default stays
  `gmail.send`; reading needs `gmail.readonly` or `gmail.modify`, and labels need `gmail.modify`.
  Every tool refuses with `scope_missing` before any API call rather than degrading.

### Fixed

- A folded `References` header could inject a line break into the headers of the message being
  sent. Python's email parser hands back the raw folded value, so the CRLF was carried straight
  into `In-Reply-To`/`References`. Header values are now unfolded on the way in.


- `tests/unit/test_no_hardcoded_credentials.py` fails the build on a credential-shaped literal in
  shipped code, or on a literal in this project's own key format in a test. The REST suite had been
  hard-coding `fmg_REDACTED_IN_HISTORY`, which the parser accepts as a working key: a valid
  credential published in a public repository, and one an operator could paste into a production
  env file. Test keys are now generated per run, and a test pins that the old literal is dead. The
  MCP allowlist test also used a real deployment's address as its fixture; it now uses RFC 5737
  TEST-NET-1, since a test has no business publishing someone's infrastructure.
- MCP tool descriptions now state their preconditions, including the exact OAuth scope strings an
  account must hold. `create_draft` previously said only "needs a compose scope", which a model
  could not check against the scopes `list_accounts` reports, so a draft against a send-only account
  cost a wasted turn. The scope text is interpolated from the same constants the enforcement uses,
  so a description cannot drift from the check it documents, and tests pin that no tool hard-codes a
  configurable limit.
- The OAuth connect flow could not complete in `auth_mode=api_key`: `GET /v1/oauth/google/callback`
  was behind the API-key gate, but Google redirects the operator's browser there and a browser
  navigation cannot set an `Authorization` header, so it answered `401 missing Authorization
  header`. The callback is now unauthenticated, which is inherent to the OAuth flow; the
  single-use, expiring `state` is the CSRF protection a bearer token would have provided. Starting
  a consent flow and disconnecting an account still require a key. The whole REST suite ran in
  `auth_mode=none`, which bypasses the auth gate, so nothing exercised the callback the way a real
  deployment does; new tests run it under `auth_mode=api_key`.

- The MCP endpoint answered `421 Invalid Host header` to every client except one on loopback. The
  MCP SDK auto-enables DNS-rebinding protection with a hard-coded loopback allowlist whenever it is
  not told the real bind address, and the gateway was not passing it, so a gateway on a Tailscale
  address, published on a port, or bound to `0.0.0.0` behind a reverse proxy was unreachable over
  MCP from anywhere but its own host. The allowlist now derives from the configured bind host plus
  loopback plus the new `GMAIL_AUTOMATOR_MCP_ALLOWED_HOSTS`, and the protection stays on rather than
  being switched off. A bare hostname is accepted, so operators need not learn the wildcard syntax.
  The existing suite missed this because every MCP test used `127.0.0.1`.

- `gmail-automator keys create` printed its "this is the only time the key is shown" warning to stdout
  instead of stderr, so `gmail-automator keys create agent | tail -1` returned the warning text rather than
  the key - the exact pattern the README recommends. The secret now goes to stdout and the warning
  to stderr, matching `gen-key` and the documented contract. A test pins both commands.

## [0.1.0] - 2026-09-26

First public release. Alpha: expect breaking changes, and read the security notes before exposing the
gateway to a network.

### Added

**Core**

- Self-hosted Gmail gateway exposing one service through two interfaces: MCP tools (stdio and
  Streamable HTTP at `/mcp`) and a REST API under `/v1`, sharing identical validation, quota
  enforcement, and error codes.
- SQLite by default, PostgreSQL optional (`pip install gmail_automator[postgres]`), chosen with
  `GMAIL_AUTOMATOR_DATABASE_URL`.
- Durable send queue in the database with leases, a persisted pacing cursor, idempotency
  fingerprints, and retry with exponential backoff honouring `Retry-After`. A job survives a gateway
  restart and is picked up by whichever process starts next.
- Rolling 24-hour quota accounting derived from the job table, with configurable per-account daily
  message and recipient limits, a soft-limit ratio, per-account pacing, and a refusal *before* Google
  is called.
- Worker maintenance loop: lease recovery, payload wiping on terminal states, and history retention.

**Google integration**

- OAuth 2.0 connect flow with CSRF state, PKCE, and OIDC userinfo; refresh-token lifecycle with a
  proactive refresh leeway and rotation-on-use.
- Gmail API transport isolated in a single module, with Google's error taxonomy translated into
  stable error codes.
- Workspace **service accounts with domain-wide delegation**, for unattended sending with no consent
  browser step and no stored refresh token.
- AES-256-GCM token encryption at rest, bound to the account address as associated data, plus staged
  key rotation (`gmail-automator rotate-keys`) that never renders a stored token unreadable in a single step.
- HTML alternative bodies, threading headers, `Bcc` handling, and header-injection rejection in the
  MIME builder.
- Attachments: inline base64 or path, off by default, with path reads confined to
  `GMAIL_AUTOMATOR_ATTACHMENT_ALLOWED_DIRS` by resolved-path containment (traversal and symlink escapes are
  refused).
- Draft mode over `POST /v1/drafts` and the `create_draft` tool, with a `scope_missing` error instead
  of an opaque provider 403 when the compose scope has not been granted.

**Operations**

- Per-API-key scopes and a per-key rate limiter; auth mode `api_key` by default, `none` only for
  loopback use.
- Prometheus metrics at `/metrics` on a private registry, with worker and HTTP instrumentation.
- Structured JSON logging with recursive, key-based redaction of secret-looking values.
- Server-rendered operator status page at `/status` - no JavaScript, no external assets, every value
  HTML-escaped.
- CLI: `serve`, `mcp-stdio`, `migrate`, `gen-key`, `status`, `send-test`, `rotate-keys`, `accounts`,
  `keys`, `purge-history`.
- Multi-file image that runs as a non-root user with a single writable volume, a compose file, and a
  smoke test asserting the auth gate, the error envelope, the non-root uid, and the absence of any
  plaintext Google token in the image or database.

### Notes

- Scope requests are `gmail.send`, `openid`, and `email`. gmail-automator never asks for mailbox read access.
- The MCP endpoint is `/mcp` **without** a trailing slash; `/mcp/` answers 307, which MCP clients do
  not follow for POST.
- The per-key rate limiter is in-process. Running N gateway processes allows N times the configured
  limit per key; use a shared rate limiter at the edge if that matters to you.
