# Security Policy

## Reporting a vulnerability

**Do not open a public issue for a suspected vulnerability.** Use the private channel:

> **Security -> Report a vulnerability** on this repository
> (`https://github.com/instax-dutta/gmail-automator/security/advisories/new`)

That opens a private GitHub Security Advisory visible only to you and the maintainer. If private
reporting is unavailable to you for any reason, email the maintainer via the address on their
[GitHub profile](https://github.com/instax-dutta) and agree on a channel before anything is written
down publicly.

Please do not open a pull request containing an exploit, a leaked credential, or a working attack
against a live deployment.

### What to include

- What an attacker can achieve, and what access they need to start.
- Steps to reproduce, ideally against a local `docker compose up` deployment with throwaway
  credentials.
- The commit or version you tested.
- Whether any real credential is involved. **If a live token or key is exposed, say so in the first
  line** - that changes the urgency and the first thing to do is revoke it.

### What happens next

| Stage | What to expect |
|---|---|
| Acknowledgement | Within a few days |
| First assessment | Whether it is accepted, and roughly how severe |
| Fix | A patch and a release, or a mitigation you can apply immediately |
| Disclosure | Credit in the changelog unless you would rather stay anonymous |

There is no formal SLA and no paid support. This is alpha software maintained in spare time, and a
slow honest answer beats a fast wrong one.

## Supported versions

The project is pre-1.0, so only the latest release carries fixes. Report against `main` if you are
running a commit.

| Version | Supported |
|---|---|
| latest release | yes |
| anything older | no - upgrade |

## What counts as a vulnerability

This is a gateway that holds a live Google OAuth credential and spends a real Gmail sending quota,
so the interesting bugs are the ones that cross a boundary the operator did not grant.

**In scope**

- Reading another account's stored tokens, or decrypting a token bound to a different account.
- Any path that returns a token, a key secret, or a decrypted credential to an API caller or a log.
- An API key or a per-key scope being bypassed, so a `send`-only key reaches read, reply, or labels.
- A send that is not checked against the account's quota before Google is contacted.
- The scope gate being skipped on any MCP tool or REST route.
- Path or content attachment handling escaping `GMAIL_AUTOMATOR_ATTACHMENT_ALLOWED_DIRS`, including
  through a symlink.
- Injection that reaches an outbound header - a subject or recipient carrying a CR or LF.
- The migration, key rotation, or queue paths losing or duplicating a queued send.

**Out of scope**

- An agent choosing to send unwanted mail. That is the operator's grant and the quota policy working
  as designed.
- Anything needing an operator to hand over a credential they already control.
- Running without TLS in front of the gateway, or binding it to a public interface.
- Denial of service from a valid key with a large queue.
- Missing hardening headers on the operator status page, which is server-rendered for a trusted LAN.

## Threat model in one paragraph

The gateway sits between an untrusted AI agent and a trusted Google account, and it is designed on
the assumption that **the agent is the attacker**. The agent is expected to retry, to loop, to ask
for more scope than it was given, and to try to read what it was not told it could read. The
credential must therefore never be reachable by the agent, a scope must never widen without an
operator, and a send that would exceed Gmail's limit must be refused before Google sees it. Most
reported issues will be an instance of one of those three failing.

Out of scope of the gateway itself: the host it runs on. A compromised host with read access to the
database and the encryption key has the tokens, and no amount of code in this repository changes
that. Encrypt the volume, keep the key out of the image, and run as a non-root user.

## Security properties, and where they are asserted

Not claims - each is a test that fails the build if it stops being true.

| Property | Enforced by |
|---|---|
| Only `gmail/client.py` may import `googleapiclient` | `tests/unit/test_import_boundaries.py` |
| Only `db.py` may create an engine | `tests/unit/test_import_boundaries.py` |
| Services take time from an injected `Clock`, never `datetime.now()` | `tests/unit/test_import_boundaries.py` |
| No credential-shaped literal anywhere in the suite | `tests/unit/test_no_hardcoded_credentials.py` |
| A CR or LF in a subject or address is rejected before the MIME is built | `tests/unit/test_mime.py` |
| Attachment paths stay inside the allow-list after symlink resolution | `tests/unit/test_attachments.py` |
| A send-only account is refused read access before any API call | `tests/integration/test_mailbox.py` |
| A read-only key cannot send, and a send-only key cannot read accounts or disconnect one | `tests/rest/test_limits.py` |
| Every send and every refusal reports the remaining budget | `tests/rest/test_api.py` |

GitHub's secret scanning with push protection, Dependabot, and CodeQL are enabled on this
repository, so a leaked credential is blocked at push rather than discovered later.

## Encryption and key handling

- OAuth refresh tokens are AES-256-GCM encrypted at rest, with the account address as associated
  data, so a ciphertext cannot be moved to another account and still decrypt.
- API keys are stored as a prefix plus a SHA-256 hash and compared with `hmac.compare_digest`.
- `GMAIL_AUTOMATOR_TOKEN_ENCRYPTION_KEY` is 32 bytes, base64. **It is the whole system.** Lose it
  and the stored tokens are unreadable; leak it and the tokens are readable. `gmail-automator
  gen-key` generates one.
- Rotate with `gmail-automator rotate-keys`, in the order the
  [operations runbook](docs/operations.md) sets out.
