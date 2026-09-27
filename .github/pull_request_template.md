## What this changes

<!-- One or two sentences. What is different after this merges? -->

## Why

<!-- The problem being solved, or a link to the issue. -->

## How it was verified

- [ ] `make check` passes (lint, format, types, unit + integration tests)
- [ ] `uv run pytest -m e2e` passes, if the change affects a wired surface
- [ ] `GMAIL_AUTOMATOR_TEST_POSTGRES_URL=... uv run pytest -m postgres` passes, if the change touches SQL
- [ ] `./scripts/docker-smoke.sh` passes, if the change touches the image, compose, or startup path
- [ ] A new test fails without this change and passes with it

## Checklist

- [ ] New behaviour is reachable from **both** REST and MCP, or the asymmetry is intentional and noted
- [ ] Any new bound is an environment variable with a safe default and a line in `.env.example`
- [ ] No secret is interpolated into a log event string; new secrets are added to the redaction keys
- [ ] Errors come from the taxonomy in `src/gmail_automator/errors.py`, so agents get a stable error code
- [ ] User-facing changes are reflected in `README.md` / `docs/`
- [ ] Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)

<!--
A security issue does not belong in a pull request. Contact the maintainer privately instead.
-->
