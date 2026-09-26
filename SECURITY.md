# Security Policy

Fmaiily holds a live Gmail sending quota and a refresh token for an account you care about. Please
report security issues privately rather than in a public issue.

## Reporting a vulnerability

Use **GitHub's private reporting** on this repository:
**Security -> Report a vulnerability** (or the "Report a vulnerability" link on the
[Security tab](https://github.com/instax-dutta/gmail-automator/security)).

If private reporting is unavailable, open a regular issue that says only "security report available
on request" with no technical detail, and a maintainer will arrange a private channel.

Please include, if you can:

- what an attacker can achieve, and what they need in order to try it
- the Fmaiily version, and whether it runs in Docker or from a checkout
- the relevant configuration, with secrets redacted
- any reproduction steps or request/response pairs

**Expect an acknowledgement within 7 days.** I will confirm the report, tell you the severity I
assess, and keep you updated while a fix is prepared. I will credit you in the advisory unless you
ask not to be named, and I will not pursue a public disclosure timeline that would leave you
exposed before a fix exists.

## What is in scope

Anything that would let someone:

- read, decrypt, or exfiltrate a stored OAuth token or the token encryption key
- authenticate to the gateway without a valid API key, or recover one
- send mail as the configured account without being authorized to
- read another account's data, history, or attachment files through the API or MCP surface
- escape the attachment allow-list (`FMAIILY_ATTACHMENT_ALLOWED_DIRS`) with traversal or a symlink
- inject content that escapes the log redaction layer and leaks a secret into logs
- bypass quota, pacing, or the rate limiter to burn an account's sending budget
- run code in the container as a privilege the image is not supposed to grant

## What is out of scope

- **Misconfiguration.** Running with `FMAIILY_AUTH_MODE=none` on a non-loopback address, or with a
  weak `FMAIILY_TOKEN_ENCRYPTION_KEY`, is a deployment error. Fmaiily warns about the first and
  cannot prevent the second.
- **Gmail-side abuse.** Fmaiily enforces and documents its limits, but using it for bulk or
  unsolicited mail violates Google's Terms of Service. That is an operational and legal problem, not
  a vulnerability in the code.
- **Running an outdated version.** Alpha software. Unreleased fixes are the main risk; see below.
- **The absence of features.** Missing hardening that has not been claimed, such as certificate
  pinning or multi-tenant isolation for a gateway designed to be single-operator.
- Denial of service through volume from an already-authorized key. The per-key rate limiter is
  deliberately in-process; see [`docs/operations.md`](docs/operations.md) before running multiple
  processes.

## Supported versions

Fmaiily is pre-1.0. Only the latest published release receives fixes.

| Version | Supported |
|---|---|
| 0.1.x (latest) | yes |
| anything older, or a commit without a release | no |

## Security posture

What the project already does, so you can judge where your own responsibility begins:

- OAuth tokens are encrypted at rest with AES-256-GCM and bound to the account address as
  associated data, so a ciphertext cannot be moved to another account and still decrypt.
- Tokens are never returned by the API and never written to a log. The log pipeline redacts
  secret-looking keys recursively, and a test pins that the redaction list does not swallow ordinary
  diagnostic fields.
- API keys are stored as a lookup prefix plus a SHA-256 hash, compared in constant time, and shown
  exactly once at creation.
- Access control is scoped per key, not just per key, and every `/v1` route and the MCP endpoint
  require it.
- Email bodies are held only while a job is in flight and are wiped on a terminal state.
- Path attachments are confined to the allow-list by resolved-path containment, and a symlink or
  `..` traversal out of it is refused.
- The container runs as a non-root user with a single writable volume, and the smoke test asserts
  that no plaintext Google token exists in the image or the database.

## What you are responsible for

- Keep `FMAIILY_TOKEN_ENCRYPTION_KEY` secret. Anyone holding it can decrypt every stored token.
  Losing it means reconnecting every account; leaking it means the same, urgently.
- Bind the gateway to loopback, or put it behind a TLS-terminating reverse proxy with an
  appropriate firewall. It speaks plain HTTP.
- Rotate the encryption key on a schedule and when someone with access leaves:
  `fmaiily rotate-keys`.
- Revoke API keys you no longer use, and keep scopes per key as narrow as the consumer allows.
- Back up the database and the key together. A backup without the key is unreadable; a key without a
  backup is pointless.
- Obtain consent from recipients and follow Google's Terms of Service. Fmaiily staying inside the
  published limits is a safety margin, not permission to send unwanted mail.
