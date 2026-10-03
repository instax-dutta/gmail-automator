# Contributing to gmail-automator

Thanks for considering a contribution. This document is the short version of what the project
expects; the code is the rest of the specification.

## The one rule

gmail-automator is a gateway that spends a real email-sending quota and holds real OAuth credentials. Every
change is judged by one question: **does this make it harder to misuse, or easier to operate
correctly?** A change that adds capability without a test, without a limit, or without an honest
error message is not finished.

## Getting set up

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/instax-dutta/gmail-automator
cd gmail-automator
uv sync --extra dev
```

You do not need a Google account or real credentials to work on gmail-automator. `tests/support/` contains
an in-process fake Google, and the default suite never opens a socket.

## Before you open a pull request

```bash
make check                      # lint, format, types, unit + integration tests
uv run pytest -m e2e            # real localhost sockets, real migrations
./scripts/docker-smoke.sh       # if you touched the image, compose, or startup path
```

`make check` must be clean. If you touched persistence, also run the Postgres suite, which CI runs
on every pull request:

```bash
docker run -d --name gmail_automator-pg -p 5432:5432 \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=gmail_automator postgres:17-alpine
GMAIL_AUTOMATOR_TEST_POSTGRES_URL=postgresql+psycopg://postgres:postgres@localhost:5432/gmail_automator \
  uv run pytest -m postgres
```

## Where things live

| Path | What belongs there |
|---|---|
| `src/gmail_automator/` | Services, transports, settings |
| `src/gmail_automator/gmail/client.py` | The only module allowed to import `googleapiclient` |
| `src/gmail_automator/db.py` | The only module allowed to create a database engine |
| `src/gmail_automator/rest/`, `src/gmail_automator/mcp_server/` | Adapters. They call services; services never call them |
| `src/gmail_automator/migrations/` | Alembic revisions |
| `tests/unit/` | One module, no I/O, no network |
| `tests/integration/` | Services against temporary SQLite plus injected fakes |
| `tests/e2e/` | The running system over real sockets |
| `docs/` | Operator-facing docs only |

## Conventions

- **Commits** follow [Conventional Commits](https://www.conventionalcommits.org/): `feat:`,
  `fix:`, `docs:`, `test:`, `refactor:`, `chore:`. One logical change per commit.
- **Tests** are part of the change, not a follow-up. Name them after the behaviour:
  `test_a_revoked_account_is_refused`, not `test_service_account_2`.
- **Time and randomness are injected.** A service that calls `datetime.now()` or `random.random()`
  instead of taking them as arguments is wrong, and
  `tests/unit/test_import_boundaries.py` will fail the build if you do.
- **Errors are typed.** Add to the taxonomy in `src/gmail_automator/errors.py` rather than raising
  `ValueError` from deep inside a service, so the agent receives a stable error code.
- **Limits are configuration.** If you introduce a new bound, it gets an environment variable, a
  safe default, and a line in `.env.example`.
- **Secrets go to the logger as keyword arguments**, never interpolated into the event string.
  Redaction is key-based, so a new secret needs a new key in the denylist.
- **No credential is ever written down.** `tests/unit/test_no_hardcoded_credentials.py` fails the
  build on a credential-shaped literal anywhere in `src/`, `migrations/`, `scripts/`, or the
  packaging files, and on a literal in this project's own `fmg_<prefix>_<secret>` key format
  anywhere in `tests/`. A test that needs a key generates one at runtime. A faked *provider* key
  shape such as `GOCSPX-test-secret` is fine and useful; a real one is not.

## Adding an MCP tool or a route

Both surfaces are thin adapters over the same services, and a new capability should be reachable
from both. Add the service first, then the route, then the tool, then the tests. A tool that exists
only over REST is an inconsistency a reviewer will catch.

## Reporting bugs

Use the issue template. The most useful details are the gmail-automator version, whether you ran it in Docker
or from a checkout, the exact command or request, and the log output. **Redact tokens, API keys, and
the encryption key before pasting** - `GMAIL_AUTOMATOR_TOKEN_ENCRYPTION_KEY` in particular will unlock every
stored credential.

If you think you have found a security issue, do not open a public issue for it. Use the private
**Security -> Report a vulnerability** form on the repository, or see
[`SECURITY.md`](SECURITY.md).

## Licence

Contributions are accepted under the [MIT licence](LICENSE). By opening a pull request you confirm
that you have the right to license the code you contribute.
