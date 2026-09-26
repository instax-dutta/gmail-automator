# Contributing to Fmaiily

Thanks for considering a contribution. This document is the short version of what the project
expects; the code is the rest of the specification.

## The one rule

Fmaiily is a gateway that spends a real email-sending quota and holds real OAuth credentials. Every
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

You do not need a Google account or real credentials to work on Fmaiily. `tests/support/` contains
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
docker run -d --name fmaiily-pg -p 5432:5432 \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=fmaiily postgres:17-alpine
FMAIILY_TEST_POSTGRES_URL=postgresql+psycopg://postgres:postgres@localhost:5432/fmaiily \
  uv run pytest -m postgres
```

## Where things live

| Path | What belongs there |
|---|---|
| `src/fmaiily/` | Services, transports, settings |
| `src/fmaiily/gmail/client.py` | The only module allowed to import `googleapiclient` |
| `src/fmaiily/db.py` | The only module allowed to create a database engine |
| `src/fmaiily/rest/`, `src/fmaiily/mcp_server/` | Adapters. They call services; services never call them |
| `src/fmaiily/migrations/` | Alembic revisions |
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
- **Errors are typed.** Add to the taxonomy in `src/fmaiily/errors.py` rather than raising
  `ValueError` from deep inside a service, so the agent receives a stable error code.
- **Limits are configuration.** If you introduce a new bound, it gets an environment variable, a
  safe default, and a line in `.env.example`.
- **Secrets go to the logger as keyword arguments**, never interpolated into the event string.
  Redaction is key-based, so a new secret needs a new key in the denylist.

## Adding an MCP tool or a route

Both surfaces are thin adapters over the same services, and a new capability should be reachable
from both. Add the service first, then the route, then the tool, then the tests. A tool that exists
only over REST is an inconsistency a reviewer will catch.

## Reporting bugs

Use the issue template. The most useful details are the Fmaiily version, whether you ran it in Docker
or from a checkout, the exact command or request, and the log output. **Redact tokens, API keys, and
the encryption key before pasting** - `FMAIILY_TOKEN_ENCRYPTION_KEY` in particular will unlock every
stored credential.

If you think you have found a security issue, do not open a public issue. See
[`SECURITY.md`](SECURITY.md).

## Licence

Contributions are accepted under the [MIT licence](LICENSE). By opening a pull request you confirm
that you have the right to license the code you contribute.
