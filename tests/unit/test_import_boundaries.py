"""Architecture guard rails (master plan R10 layer rule).

These tests fail the build the moment a module reaches across a sealed seam, which is the only
mechanism that keeps the Gmail surface fakeable and the SQLite/Postgres choice swappable.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "fmaiily"

#: third-party module -> the only fmaiily modules allowed to import it
IMPORT_ALLOWLIST: dict[str, set[str]] = {
    "googleapiclient": {"gmail/client.py"},
    "google.auth": {"gmail/client.py", "tokens.py"},
    "google.oauth2": {"gmail/client.py", "tokens.py"},
    "httplib2": {"gmail/client.py"},
    "alembic": {"db.py"},
}

#: only these modules may create a database engine
ENGINE_CREATORS = {"db.py"}


def _python_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _imported_roots(path: Path) -> set[str]:
    """Top-level package of every absolute import in the file."""
    return {name.split(".")[0] for name in _imported_modules(path)}


def _imported_modules(path: Path) -> set[str]:
    """Every absolute module path imported by the file, `from a.b import c` kept as `a.b`."""
    tree = ast.parse(path.read_text(), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            modules.add(node.module)
    return modules


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_third_party_imports_respect_the_allowlist(path: Path) -> None:
    rel = path.relative_to(SRC).as_posix()
    for root in _imported_roots(path):
        if root not in IMPORT_ALLOWLIST:
            continue
        allowed = IMPORT_ALLOWLIST[root]
        assert rel in allowed or any(rel.startswith(prefix) for prefix in allowed), (
            f"{rel} may not import {root}; only {sorted(allowed)} may"
        )


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_only_db_py_creates_engines(path: Path) -> None:
    rel = path.relative_to(SRC).as_posix()
    source = path.read_text()
    if rel in ENGINE_CREATORS:
        return
    for symbol in ("create_engine(", "create_async_engine(", "make_engine("):
        assert symbol not in source, f"{rel} must not call {symbol}; only db.py may"


def test_googleapiclient_is_confined_to_the_gmail_client() -> None:
    offenders = [
        path.relative_to(SRC).as_posix()
        for path in _python_files()
        if "googleapiclient" in _imported_roots(path)
        and path.relative_to(SRC).as_posix() != "gmail/client.py"
    ]
    assert offenders == []


def test_no_module_calls_datetime_now_directly_outside_models_and_clock() -> None:
    """Services take `now` from the injected Clock (master plan R9)."""
    allowed = {"db.py", "models.py", "clock.py", "logging_setup.py"}
    offenders = []
    for path in _python_files():
        rel = path.relative_to(SRC).as_posix()
        if rel in allowed or rel.endswith("cli.py"):
            continue
        if "datetime.now(" in path.read_text():
            offenders.append(rel)
    assert offenders == []


def test_no_service_imports_a_second_http_client() -> None:
    """All outbound HTTP goes through httpx, or through httplib2 for googleapiclient.

    `urllib.parse` is fine: building a query string is not making a request.
    """
    banned = {"requests", "urllib3", "urllib.request", "urllib.error", "http.client"}
    offenders = [
        path.relative_to(SRC).as_posix()
        for path in _python_files()
        if path.relative_to(SRC).as_posix() != "gmail/client.py"
        and _imported_modules(path) & banned
    ]
    assert offenders == []
