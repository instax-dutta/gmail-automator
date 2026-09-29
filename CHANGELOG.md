# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). gmail-automator is pre-1.0, so a minor bump may
still contain breaking changes; the "Unreleased" section is the authoritative list of what is
already merged.

## [Unreleased]

### Fixed

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
