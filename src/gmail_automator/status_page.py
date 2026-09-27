"""Operator status page (Phase 3, P5).

`gmail-automator status` suits a terminal and is useless in a browser tab. This page
renders the same numbers server-side: no JavaScript, no build step, no CDN, and nothing fetched from
the network. It is the same code path as the CLI, so the two cannot disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape

from gmail_automator.container import Container, require


@dataclass(frozen=True)
class AccountStatus:
    email: str
    account_type: str
    status: str
    messages_used: int
    message_soft_limit: int
    messages_remaining: int
    recipients_remaining: int
    pending_jobs: int
    reset_at: datetime | None
    next_send_at: datetime | None
    last_error: str | None


@dataclass(frozen=True)
class StatusSnapshot:
    version: str
    environment: str
    database: str
    auth_mode: str
    worker_enabled: bool
    queue_depth: int
    queue_max_depth: int
    send_interval_seconds: float
    accounts: tuple[AccountStatus, ...]


def collect(container: Container, *, now: datetime | None = None) -> StatusSnapshot:
    from gmail_automator import __version__

    settings = container.settings
    now = now or container.clock.now()
    quota = require(container, "quota")
    accounts_service = require(container, "accounts")
    queue = require(container, "queue")

    rows: list[AccountStatus] = []
    for account in accounts_service.list_all():
        snapshot = quota.snapshot(account, now=now)
        rows.append(
            AccountStatus(
                email=account.email,
                account_type=account.account_type,
                status=account.status,
                messages_used=snapshot.messages_sent,
                message_soft_limit=snapshot.message_soft_limit,
                messages_remaining=snapshot.messages_remaining,
                recipients_remaining=snapshot.recipients_remaining,
                pending_jobs=snapshot.pending_jobs,
                reset_at=snapshot.reset_at,
                next_send_at=account.next_send_at,
                last_error=account.last_refresh_error,
            )
        )
    return StatusSnapshot(
        version=__version__,
        environment=settings.environment,
        database=settings.database_url.split("://", 1)[0],
        auth_mode=settings.auth_mode,
        worker_enabled=settings.worker_enabled,
        queue_depth=queue.depth(),
        queue_max_depth=settings.queue_max_depth,
        send_interval_seconds=settings.default_send_interval_seconds,
        accounts=tuple(rows),
    )


def render(snapshot: StatusSnapshot) -> str:
    """Server-rendered HTML. Every interpolated value is escaped."""
    accounts_html = "".join(_render_account(row) for row in snapshot.accounts)
    if not snapshot.accounts:
        accounts_html = (
            '<p class="empty">No Gmail account is connected. '
            "Run <code>gmail-automator accounts connect</code> and open the printed URL.</p>"
        )
    worker = "running" if snapshot.worker_enabled else "disabled"
    auth = (
        "disabled (loopback only)"
        if snapshot.auth_mode == "none"
        else f"{snapshot.auth_mode} (bearer token)"
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>gmail-automator status</title>
<style>{_CSS}</style>
</head>
<body>
<main>
  <header>
    <h1>gmail-automator</h1>
    <p class="sub">{escape(snapshot.version)} &middot; {escape(snapshot.environment)}
      &middot; {escape(snapshot.database)} &middot; worker {worker} &middot; auth {escape(auth)}</p>
  </header>

  <section class="tiles">
    {
        _tile(
            "Queue depth",
            f"{snapshot.queue_depth} / {snapshot.queue_max_depth}",
            "messages waiting to be sent",
        )
    }
    {_tile("Accounts", str(len(snapshot.accounts)), "connected Gmail accounts")}
    {_tile("Pacing", f"{snapshot.send_interval_seconds:g}s", "between sends, per account")}
    {_tile("Window", "24h", "rolling quota window")}
  </section>

  <h2>Accounts</h2>
  {accounts_html}

  <footer>
    <p>Limits are enforced by the gateway before Gmail is called. See
      <code>docs/operations.md</code> for the runbook.</p>
  </footer>
</main>
</body>
</html>
"""


def _render_account(row: AccountStatus) -> str:
    used_pct = _percent(row.messages_used, row.message_soft_limit)
    badge = f'<span class="badge {escape(row.status)}">{escape(row.status)}</span>'
    detail = ""
    if row.reset_at:
        detail += f"<div>capacity frees at {_stamp(row.reset_at)}</div>"
    if row.next_send_at:
        detail += f"<div>next send from {_stamp(row.next_send_at)}</div>"
    if row.last_error:
        detail += f'<div class="error">last error: {escape(row.last_error)}</div>'
    return f"""<article class="account">
  <div class="head"><span class="email">{escape(row.email)}</span> {badge}
    <span class="type">{escape(row.account_type)}</span></div>
  <div class="bar"><div class="fill" style="width:{used_pct}%"></div></div>
  <div class="numbers">
    <span>{row.messages_used} / {row.message_soft_limit} messages used</span>
    <span>{row.messages_remaining} messages left</span>
    <span>{row.recipients_remaining} recipients left</span>
    <span>{row.pending_jobs} queued</span>
  </div>
  {detail}
</article>"""


def _tile(label: str, value: str, hint: str) -> str:
    return (
        f'<div class="tile"><div class="label">{escape(label)}</div>'
        f'<div class="value">{escape(value)}</div>'
        f'<div class="hint">{escape(hint)}</div></div>'
    )


def _percent(used: int, limit: int) -> int:
    if limit <= 0:
        return 0
    return max(0, min(100, round(used * 100 / limit)))


def _stamp(value: datetime) -> str:
    return escape(value.astimezone().strftime("%Y-%m-%d %H:%M %Z"))


_CSS = """
:root { color-scheme: light dark; --bg:#0f1115; --fg:#e8eaed; --muted:#9aa0a6;
  --line:#2a2f38; --ok:#3ddc84; --warn:#f5a623; --err:#ff6b6b; --accent:#7aa2f7; }
@media (prefers-color-scheme: light) {
  :root { --bg:#f7f8fa; --fg:#1b1d21; --muted:#5f6670; --line:#e2e5ea; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
  font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 62rem; margin: 0 auto; padding: 2.5rem 1.25rem 4rem; }
h1 { margin:0; font-size: 1.6rem; letter-spacing: -0.02em; }
h2 { margin: 2.5rem 0 1rem; font-size: 1.05rem; text-transform: uppercase;
  letter-spacing: 0.08em; color: var(--muted); }
.sub { margin: .35rem 0 0; color: var(--muted); }
.tiles { display: grid; gap: .75rem; margin-top: 1.75rem;
  grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr)); }
.tile { border:1px solid var(--line); border-radius: .6rem; padding: .9rem 1rem; }
.tile .label { color: var(--muted); font-size: .78rem; text-transform: uppercase;
  letter-spacing: .07em; }
.tile .value { font-size: 1.5rem; font-variant-numeric: tabular-nums; margin-top: .2rem; }
.tile .hint { color: var(--muted); font-size: .78rem; margin-top: .15rem; }
.account { border:1px solid var(--line); border-radius: .6rem; padding: 1rem 1.1rem;
  margin-bottom: .75rem; }
.account .head { display:flex; align-items:center; gap:.6rem; flex-wrap:wrap; }
.email { font-weight:600; }
.type { color: var(--muted); font-size:.8rem; }
.badge { font-size:.72rem; text-transform:uppercase; letter-spacing:.06em;
  padding:.12rem .45rem; border-radius:999px; border:1px solid var(--line); color:var(--muted); }
.badge.active { color: var(--ok); border-color: color-mix(in srgb, var(--ok) 45%, transparent); }
.badge.error { color: var(--err); border-color: color-mix(in srgb, var(--err) 45%, transparent); }
.bar { height:6px; background:var(--line); border-radius:999px; margin:.75rem 0 .5rem;
  overflow:hidden; }
.fill { height:100%; background: var(--accent); }
.numbers { display:flex; flex-wrap:wrap; gap:.25rem 1.25rem; color:var(--muted);
  font-size:.85rem; font-variant-numeric: tabular-nums; }
.error { color: var(--err); font-size:.85rem; margin-top:.4rem; }
.empty { color: var(--muted); border:1px dashed var(--line); border-radius:.6rem;
  padding:1.25rem; }
code { background: var(--line); padding:.1rem .3rem; border-radius:.25rem; font-size:.85em; }
footer { margin-top: 3rem; padding-top: 1rem; border-top:1px solid var(--line);
  color: var(--muted); font-size:.85rem; }
"""


__all__ = ["AccountStatus", "StatusSnapshot", "collect", "render"]
