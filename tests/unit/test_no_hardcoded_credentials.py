"""No credential-shaped literal may live in this repository.

A key or secret committed to a public repository is compromised the moment it is pushed, and it
stays compromised after removal because it remains in history. This test fails the build instead, at
the point where a credential is about to be written down.

Scope and why:

- `src/`, `migrations/`, `scripts/`, and the packaging files are scanned with no exemption. Shipped
  code reads credentials from the environment and must never contain one.
- `tests/` is scanned for credentials in *this project's own key format* only. A test that fakes a
  provider's key shape (`GOCSPX-test-secret`) is doing useful work, but a literal in our own
  `fmg_<prefix>_<secret>` format is a credential someone could paste into a production env file, so
  those are generated at runtime instead.
- The check is intentionally pattern-based and deliberately noisy on false positives: a reviewer
  reads the report and confirms, rather than a scanner deciding what is dangerous.

`tests/AGENTS.md` records the rule this enforces.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: Shipped code: any credential shape at all is a failure.
SHIPPED_TREES = ("src", "migrations", "scripts")
SHIPPED_FILES = (
    "pyproject.toml",
    "alembic.ini",
    "Makefile",
    "Dockerfile",
    "docker-compose.yml",
    ".env.example",
    ".dockerignore",
    "uv.lock",
)

SHIPPED_PATTERNS: dict[str, re.Pattern[str]] = {
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "Google API key": re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    "Google OAuth client secret": re.compile(r"GOCSPX-[0-9A-Za-z_-]{10,}"),
    "AWS access key id": re.compile(r"AKIA[0-9A-Z]{16}"),
    "GitHub token": re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[0-9A-Za-z]{30,}"),
    "GitHub fine-grained PAT": re.compile(r"github_pat_[0-9A-Za-z_]{20,}"),
    "Slack token": re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}"),
    "Stripe live key": re.compile(r"[sr]k_live_[0-9A-Za-z]{10,}"),
    "Anthropic API key": re.compile(r"sk-ant-[0-9A-Za-z-]{10,}"),
    "JWT": re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "credentials in a URL": re.compile(
        r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s:@/]+:[^\s@/\s]+@"
    ),
    "a gateway API key": re.compile(r"fmg_[0-9a-f]{8}_[A-Za-z0-9_-]{43}"),
    "a hard-coded encryption key": re.compile(
        r"(?i:TOKEN_ENCRYPTION_KEY\s*[=:]\s*[\"']?[A-Za-z0-9+/=_-]{20,})"
    ),
}

#: Test code: only this project's own key format, because only it is pasteable into a real deploy.
TEST_PATTERNS: dict[str, re.Pattern[str]] = {
    "a literal gateway API key": re.compile(r"fmg_[0-9a-f]{8}_[A-Za-z0-9_-]{43}"),
    "a literal token encryption key": re.compile(
        r"(?i:TOKEN_ENCRYPTION_KEY[\"']?\s*[:=]\s*[\"'][A-Za-z0-9+/=_-]{40,}[\"'])"
    ),
}

_SKIP_DIRS = {"__pycache__", ".venv", ".git", "dist", "build", ".pytest_cache", ".ruff_cache"}
_SKIP_SUFFIX = {".pyc", ".pyo", ".png", ".jpg", ".gz", ".whl", ".zip", ".sqlite", ".db"}


def _walk(relative: str) -> list[Path]:
    base = ROOT / relative
    if not base.is_dir():
        return []
    return sorted(
        p
        for p in base.rglob("*")
        if p.is_file()
        and p.suffix not in _SKIP_SUFFIX
        and not (_SKIP_DIRS & set(p.relative_to(ROOT).parts))
    )


def _candidates() -> list[tuple[Path, dict[str, re.Pattern[str]]]]:
    out: list[tuple[Path, dict[str, re.Pattern[str]]]] = []
    for tree in SHIPPED_TREES:
        out.extend((p, SHIPPED_PATTERNS) for p in _walk(tree))
    for name in SHIPPED_FILES:
        if (ROOT / name).is_file():
            out.append((ROOT / name, SHIPPED_PATTERNS))
    out.extend((p, TEST_PATTERNS) for p in _walk("tests"))
    return out


_CANDIDATES = _candidates()


def test_the_scan_actually_covers_the_repository() -> None:
    """Guard the guard: an empty file list means the scan silently checks nothing."""
    covered = {p.relative_to(ROOT).as_posix() for p, _ in _CANDIDATES}
    assert "pyproject.toml" in covered
    assert "src/gmail_automator/api_keys.py" in covered or "src/fmaiily/api_keys.py" in covered
    assert any(name.startswith("tests/") for name in covered)
    assert len(covered) > 100, f"only {len(covered)} files are being scanned"


@pytest.mark.parametrize(
    ("path", "patterns"),
    _CANDIDATES,
    ids=[p.relative_to(ROOT).as_posix() for p, _ in _CANDIDATES],
)
def test_no_credential_is_written_down(path: Path, patterns: dict[str, re.Pattern[str]]) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):  # pragma: no cover - binary or unreadable
        pytest.skip(f"{path.name} is not readable as text")
    for label, pattern in patterns.items():
        for match in pattern.finditer(text):
            line = text[: match.start()].count("\n") + 1
            value = match.group(0)
            pytest.fail(
                f"{path.relative_to(ROOT)}:{line} contains {label}: {value!r}\n\n"
                "Remove it and read the value from the environment. If this is a test fixture, "
                "generate the value at runtime instead of writing it down. A credential committed "
                "here is public the moment it is pushed and stays in git history afterwards."
            )
