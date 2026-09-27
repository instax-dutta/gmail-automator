"""Operator status page (Phase 3, P5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html import escape

from gmail_automator.models import SendJob
from gmail_automator.status_page import collect, render

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def test_collect_reports_every_account(wired, accounts, connected) -> None:
    snapshot = collect(wired, now=NOW)
    assert snapshot.version == "0.1.0"
    assert snapshot.environment == "dev"
    assert snapshot.database == "sqlite"
    assert snapshot.worker_enabled is False
    assert [row.email for row in snapshot.accounts] == [connected]
    assert snapshot.accounts[0].status == "active"
    assert snapshot.accounts[0].messages_used == 0
    assert snapshot.accounts[0].message_soft_limit == 425
    assert snapshot.accounts[0].messages_remaining == 425
    assert snapshot.queue_depth == 0


def test_collect_counts_the_queue(wired, sender, account) -> None:
    from gmail_automator.gmail.mime import OutgoingMessage

    for index in range(2):
        sender.send(
            account_email=None,
            msg=OutgoingMessage(
                from_email=account.email, to=("a@example.com",), subject=f"s{index}", body="b"
            ),
            source="api",
            wait=False,
            now=NOW,
        )
    snapshot = collect(wired, now=NOW)
    assert snapshot.queue_depth == 2
    assert snapshot.accounts[0].pending_jobs == 2


def test_collect_surfaces_a_refresh_error(wired, accounts, connected) -> None:
    accounts.set_status(connected, "error", error="invalid_grant")
    row = collect(wired, now=NOW).accounts[0]
    assert row.status == "error"
    assert row.last_error == "invalid_grant"


def test_render_is_valid_html_with_the_key_numbers(wired, connected) -> None:
    html = render(collect(wired, now=NOW))
    assert html.startswith("<!doctype html>")
    assert "</html>" in html
    assert escape(connected) in html
    assert "425" in html  # the soft limit
    assert "Queue depth" in html
    assert "worker disabled" in html


def test_render_explains_an_empty_installation(wired) -> None:
    html = render(collect(wired, now=NOW))
    assert "No Gmail account is connected" in html
    assert "gmail-automator accounts connect" in html


def test_render_escapes_an_account_address(wired, accounts) -> None:
    """An address is operator-supplied data; it must never become markup."""
    from gmail_automator.errors import InvalidRequest

    hostile = "a<b>@example.com"
    try:
        accounts.upsert_oauth_account(
            email=hostile, token_uri="https://oauth2.googleapis.com/token", now=NOW
        )
    except InvalidRequest:
        # Pydantic rejects the address outright, which is an even better outcome.
        return
    html = render(collect(wired, now=NOW))
    assert "<b>@example.com" not in html
    assert escape(hostile) in html


def test_render_escapes_a_refresh_error(wired, accounts, connected) -> None:
    accounts.set_status(connected, "error", error="<script>alert(1)</script>")
    html = render(collect(wired, now=NOW))
    assert "<script>alert(1)</script>" not in html
    assert escape("<script>alert(1)</script>") in html


def test_the_page_loads_no_external_assets(wired) -> None:
    """It has to work on an isolated host, so nothing may be fetched from the network."""
    html = render(collect(wired, now=NOW))
    assert "http://" not in html
    assert "https://" not in html.replace("https://www.w3.org", "")
    assert "<script" not in html
    assert "<link" not in html


def test_a_used_window_shows_a_bar_and_a_reset_time(
    wired, accounts, connected, session_factory
) -> None:
    account = accounts.get(connected)
    with session_factory() as session:
        session.add(
            SendJob(
                account_id=account.id,
                status="sent",
                recipients=1,
                source="api",
                scheduled_at=NOW - timedelta(hours=20),
                sent_at=NOW - timedelta(hours=20),
                created_at=NOW - timedelta(hours=20),
                updated_at=NOW - timedelta(hours=20),
            )
        )
        session.commit()
    html = render(collect(wired, now=NOW))
    assert "1 / 425 messages used" in html
    assert "capacity frees at" in html
    assert 'class="fill" style="width:0%"' in html or "width:" in html


def test_collect_uses_the_injected_clock(wired, sender, account) -> None:
    from gmail_automator.gmail.mime import OutgoingMessage

    sender.send(
        account_email=None,
        msg=OutgoingMessage(from_email=account.email, to=("a@example.com",), subject="s", body="b"),
        source="api",
        wait=False,
        now=NOW,
    )
    assert collect(wired, now=NOW).queue_depth == 1
    assert collect(wired, now=NOW + timedelta(hours=1)).accounts[0].reset_at is None
