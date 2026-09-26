"""Operator CLI.

Every command is a thin wrapper over the same services the HTTP surfaces use, so a CLI send and
an API send are governed by identical quota rules. Secrets are printed exactly once, at the moment
they are created, and never written to the database or to a log line.
"""

from __future__ import annotations

import base64
import json
import secrets
import sys
from typing import Annotated, Any

import typer

from fmaiily import __version__
from fmaiily.api_keys import ApiKeyService
from fmaiily.config import Settings
from fmaiily.container import Container, build_container
from fmaiily.db import run_migrations
from fmaiily.errors import GatewayError
from fmaiily.gmail.mime import OutgoingMessage
from fmaiily.logging_setup import configure_logging, get_logger
from fmaiily.worker import Worker

app = typer.Typer(
    name="fmaiily",
    help="Self-hosted Gmail gateway for AI agents.",
    no_args_is_help=True,
    add_completion=False,
)
keys_app = typer.Typer(help="Issue and revoke gateway API keys.", no_args_is_help=True)
accounts_app = typer.Typer(help="Inspect and disconnect Gmail accounts.", no_args_is_help=True)
app.add_typer(keys_app, name="keys")
app.add_typer(accounts_app, name="accounts")

_log = get_logger("fmaiily.cli")


def _quiet_alembic() -> None:
    """Alembic's INFO chatter would land on stderr next to real output; keep it terse."""
    import logging

    for name in ("alembic", "alembic.runtime.migration", "sqlalchemy.engine"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _settings() -> Settings:
    try:
        return Settings()
    except Exception as exc:
        typer.secho(f"configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc


def _container(settings: Settings | None = None) -> Container:
    resolved = settings or _settings()
    configure_logging(level=resolved.log_level, json_output=False, stream=sys.stderr)
    _quiet_alembic()
    run_migrations(resolved.database_url, resolved.validate_migrations())
    return build_container(resolved)


def _fail(exc: GatewayError) -> None:
    typer.secho(f"{exc.code}: {exc.message}", fg=typer.colors.RED, err=True)
    if exc.details:
        typer.secho(json.dumps(exc.details, indent=2, default=str), err=True)
    raise typer.Exit(code=1)


def _echo_json(payload: Any) -> None:
    typer.echo(json.dumps(payload, indent=2, default=str))


# ------------------------------------------------------------------ top level


@app.command()
def version() -> None:
    """Print the gateway version."""
    typer.echo(__version__)


@app.command()
def serve(
    host: Annotated[
        str | None, typer.Option(help="Bind address; defaults to the configured one")
    ] = None,
    port: Annotated[int | None, typer.Option(help="Bind port")] = None,
    reload: Annotated[bool, typer.Option(help="Reload on code changes (development only)")] = False,
) -> None:
    """Run the HTTP gateway: REST under /v1 plus the MCP endpoint."""
    import uvicorn

    settings = _settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json, stream=sys.stderr)
    bind_host = host or settings.host
    bind_port = port or settings.port
    _log.info(
        "cli_serve", host=bind_host, port=bind_port, auth_mode=settings.auth_mode, reload=reload
    )
    if reload:
        uvicorn.run(
            "fmaiily.rest.app:uvicorn_app",
            host=bind_host,
            port=bind_port,
            reload=True,
            factory=True,
        )
        return
    uvicorn.run(create_app_for_serve(settings), host=bind_host, port=bind_port)


def create_app_for_serve(settings: Settings) -> Any:
    from fmaiily.rest.app import create_app

    return create_app(settings=settings)


@app.command("mcp-stdio")
def mcp_stdio() -> None:
    """Run the MCP server on stdio, for agent clients that spawn a subprocess.

    stdout carries the protocol, so logging is pinned to stderr at WARNING.
    """
    settings = _settings()
    # stdout is the protocol channel for MCP; everything human goes to stderr
    configure_logging(level="WARNING", json_output=False, stream=sys.stderr)
    _quiet_alembic()
    run_migrations(settings.database_url, settings.validate_migrations())
    container = build_container(settings)

    from fmaiily.mcp_server.server import create_mcp_server

    mcp = create_mcp_server(lambda: container)
    _log.info("cli_mcp_stdio", version=__version__)
    mcp.run(transport="stdio")


@app.command()
def migrate() -> None:
    """Create or upgrade the database schema."""
    settings = _settings()
    run_migrations(settings.database_url, settings.validate_migrations())
    dialect = settings.database_url.split("://", 1)[0]
    typer.secho(f"migrations applied ({dialect})", fg=typer.colors.GREEN)


@app.command("gen-key")
def gen_key() -> None:
    """Generate a 32 byte token encryption key.

    Print it once and store it as FMAIILY_TOKEN_ENCRYPTION_KEY. Losing it makes every stored
    OAuth token unreadable, so keep a backup.
    """
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    typer.secho(key)
    typer.secho(
        "\nStore this now: it is shown once, and without it stored tokens cannot be decrypted.",
        fg=typer.colors.YELLOW,
        err=True,
    )


@app.command()
def status(
    as_json: Annotated[bool, typer.Option("--json", help="Machine readable output")] = False,
) -> None:
    """Show connected accounts, remaining capacity, and queue depth."""
    container = _container()
    accounts = container.accounts
    quota = container.quota
    assert accounts is not None and quota is not None
    now = container.clock.now()
    rows = []
    for account in accounts.list_all():
        snapshot = quota.snapshot(account, now=now)
        rows.append(
            {
                "account": account.email,
                "type": account.account_type,
                "status": account.status,
                "messages_used": snapshot.messages_sent,
                "message_soft_limit": snapshot.message_soft_limit,
                "messages_remaining": snapshot.messages_remaining,
                "recipients_remaining": snapshot.recipients_remaining,
                "pending_jobs": snapshot.pending_jobs,
                "reset_at": snapshot.reset_at.isoformat() if snapshot.reset_at else None,
                "next_send_at": (
                    account.next_send_at.isoformat() if account.next_send_at else None
                ),
            }
        )
    payload = {
        "version": __version__,
        "environment": container.settings.environment,
        "database": container.settings.database_url.split("://", 1)[0],
        "auth_mode": container.settings.auth_mode,
        "worker_enabled": container.settings.worker_enabled,
        "queue_depth": container.queue.depth() if container.queue else 0,
        "accounts": rows,
    }
    if as_json:
        _echo_json(payload)
        return
    typer.echo(f"fmaiily {payload['version']} ({payload['environment']}, {payload['database']})")
    typer.echo(f"auth mode: {payload['auth_mode']}   worker: {payload['worker_enabled']}")
    typer.echo(f"queue depth: {payload['queue_depth']}")
    if not rows:
        typer.secho(
            "no accounts connected - run `fmaiily accounts connect`", fg=typer.colors.YELLOW
        )
        return
    for row in rows:
        typer.echo(
            f"\n{row['account']} [{row['status']}, {row['type']}]\n"
            f"  messages   {row['messages_used']}/{row['message_soft_limit']} used, "
            f"{row['messages_remaining']} left\n"
            f"  recipients {row['recipients_remaining']} left\n"
            f"  queued     {row['pending_jobs']}"
            + (f"\n  resets at  {row['reset_at']}" if row["reset_at"] else "")
        )


@app.command("send-test")
def send_test(
    to: Annotated[str, typer.Argument(help="Recipient address")],
    account: Annotated[str | None, typer.Option(help="Account to send from")] = None,
    subject: Annotated[str, typer.Option("--subject")] = "Fmaiily test message",
    body: Annotated[str, typer.Option("--body")] = "Sent by fmaiily to verify the setup.",
    wait: Annotated[
        bool, typer.Option("--wait/--no-wait", help="Block for the Gmail message id")
    ] = True,
    run_worker: Annotated[
        bool, typer.Option("--run-worker/--no-run-worker", help="Drain the queue inline")
    ] = True,
) -> None:
    """Send a real message, so an operator can confirm the whole path works."""
    container = _container()
    sender = container.sender
    accounts = container.accounts
    assert sender is not None and accounts is not None
    try:
        resolved = accounts.resolve(account)
        outcome = sender.send(
            account_email=resolved.email,
            msg=OutgoingMessage(from_email=resolved.email, to=(to,), subject=subject, body=body),
            source="cli",
            wait=False,
        )
        typer.secho(
            f"queued job {outcome.job_id} from {outcome.account_email}", fg=typer.colors.GREEN
        )
        if not run_worker:
            typer.echo(
                "not sending now; run `fmaiily status` or let the gateway worker drain the queue"
            )
            return
        worker = Worker(container, worker_id="cli-send-test", poll_interval=0.0)
        result = worker.run_once()
        if result.action == "sent":
            typer.secho(f"sent: Gmail message id {result.message_id}", fg=typer.colors.GREEN)
            return
        if result.action in ("queued", "idle"):
            typer.secho(
                "the job is paced for a later slot; start the gateway and it will go out",
                fg=typer.colors.YELLOW,
            )
            return
        typer.secho(
            f"send failed: {result.error_code} (job {outcome.job_id})",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    except GatewayError as exc:
        _fail(exc)


# ------------------------------------------------------------------ api keys


@keys_app.command("create")
def keys_create(
    name: Annotated[str, typer.Argument(help="Human readable label for the key")],
    scopes: Annotated[
        str, typer.Option("--scopes", help="Comma separated: send,read,admin")
    ] = "send,read",
    accounts: Annotated[
        str | None, typer.Option("--accounts", help="Comma separated allow-list of addresses")
    ] = None,
    rate_limit: Annotated[
        int | None, typer.Option("--rate-limit", help="Requests per minute for this key")
    ] = None,
) -> None:
    """Issue an API key. The secret is printed once and never stored."""
    container = _container()
    parsed = tuple(s.strip() for s in scopes.split(",") if s.strip())
    allowed = tuple(a.strip() for a in accounts.split(",")) if accounts else None
    context, full_key = ApiKeyService(container=container).create(
        name=name, scopes=parsed, allowed_accounts=allowed, rate_limit_per_minute=rate_limit
    )
    _echo_json({"id": context.key_id, "name": context.name, "scopes": list(parsed)})
    typer.secho(f"\n{full_key}", fg=typer.colors.GREEN)
    typer.secho("This is the only time the key is shown. Store it now.", fg=typer.colors.YELLOW)


@keys_app.command("list")
def keys_list() -> None:
    """List API keys and their prefixes (never the secrets)."""
    container = _container()
    _echo_json(ApiKeyService(container=container).list_keys())


@keys_app.command("revoke")
def keys_revoke(
    prefix: Annotated[str, typer.Argument(help="Key prefix, e.g. fmg_1a2b3c4d")],
) -> None:
    """Revoke a key so it stops working immediately."""
    container = _container()
    try:
        ApiKeyService(container=container).revoke(prefix)
    except GatewayError as exc:
        _fail(exc)
    typer.secho(f"revoked {prefix}", fg=typer.colors.GREEN)


# ------------------------------------------------------------------ accounts


@accounts_app.command("list")
def accounts_list() -> None:
    """List connected Gmail accounts."""
    container = _container()
    accounts = container.accounts
    assert accounts is not None
    _echo_json([summary.model_dump(mode="json") for summary in accounts.summaries()])


@accounts_app.command("connect")
def accounts_connect(
    login_hint: Annotated[str | None, typer.Option(help="Pre-fill the Google account")] = None,
    show: Annotated[bool, typer.Option("--show", help="Print the URL only")] = True,
) -> None:
    """Print the Google consent URL for connecting a Gmail account.

    The gateway cannot complete this on its own: a human has to open the URL and grant consent.
    Google then redirects to the configured callback, which stores the tokens.
    """
    container = _container()
    oauth = container.oauth
    assert oauth is not None
    request = oauth.start(account_hint=login_hint)
    if not show:
        raise typer.Exit(code=0)
    typer.echo("Open this URL in a browser and grant access:\n")
    typer.secho(request.authorization_url, fg=typer.colors.CYAN)
    typer.echo(
        f"\nstate: {request.state}\nexpires: {request.expires_at.isoformat()}\n"
        f"redirect: {container.settings.oauth_redirect_uri}"
    )


@accounts_app.command("disconnect")
def accounts_disconnect(account: Annotated[str, typer.Argument(help="Account address")]) -> None:
    """Disconnect an account and delete its stored tokens."""
    container = _container()
    accounts = container.accounts
    assert accounts is not None
    try:
        accounts.revoke(account)
    except GatewayError as exc:
        _fail(exc)
    typer.secho(f"disconnected {account}", fg=typer.colors.GREEN)


@app.command("purge-history")
def purge_history(
    days: Annotated[
        int | None, typer.Option(help="Days to keep; defaults to the configured value")
    ] = None,
) -> None:
    """Delete send history older than the retention window."""
    container = _container()
    history = container.history
    assert history is not None
    keep = days if days is not None else container.settings.history_retention_days
    removed = history.purge_older_than(days=keep)
    typer.secho(f"removed {removed} history rows older than {keep} days", fg=typer.colors.GREEN)


def main() -> None:  # pragma: no cover - console script entry point
    app()


__all__ = ["accounts_app", "app", "keys_app", "main"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
