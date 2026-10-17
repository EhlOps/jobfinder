"""`jobfinder users add|list|reset-link` and `jobfinder users admin grant|revoke` (services.accounts)."""
from typing import Annotated

import typer

from jobfinder.cli._common import run, transaction
from jobfinder.core.errors import Conflict
from jobfinder.services import accounts

app = typer.Typer(help="Accounts: invite a user, list them, print a password link, grant admin.",
                  no_args_is_help=True)
admin = typer.Typer(help="Grant or revoke admin rights.", no_args_is_help=True)
app.add_typer(admin, name="admin")

Email = Annotated[str, typer.Option(help="The user's email address.")]


@app.command()
def add(email: Email) -> None:
    """Add a pending user (no password yet). They set one from a link they request on the sign-in page."""
    run(_add(email))


async def _add(email: str) -> None:
    async with transaction() as db:
        user = await accounts.add_user(db, email)
        if not user:
            raise Conflict(f"{accounts.normal_email(email)} already exists")
    typer.echo(f"Added {user.email} (pending). They can request a link on the sign-in page, "
               f"or run: jobfinder users reset-link --email {user.email}")


@app.command("list")
def list_() -> None:
    """Every user: id, email, active or pending, admin, created and activated dates."""
    run(_list())


async def _list() -> None:
    async with transaction() as db:
        users = await accounts.list_users(db)
    for u in users:
        state = "active" if u.password_hash else "pending"
        line = (f"{u.id:<4} {u.email:<40} {state:<8} {'admin' if u.is_admin else '':<6} "
                f"created={u.created_at:%Y-%m-%d}")
        typer.echo(f"{line} activated={u.activated_at:%Y-%m-%d}" if u.activated_at else line)


@app.command("reset-link")
def reset_link(email: Email) -> None:
    """Print a one-time password link instead of emailing it (for when SMTP is down)."""
    run(_reset_link(email))


async def _reset_link(email: str) -> None:
    async with transaction() as db:
        typer.echo(await accounts.link_for(db, email))


@admin.command()
def grant(email: Email) -> None:
    """Make the user an admin (takes effect on their next page load)."""
    run(_set_admin(email, True))


@admin.command()
def revoke(email: Email) -> None:
    """Take admin rights away (takes effect on their next page load)."""
    run(_set_admin(email, False))


async def _set_admin(email: str, value: bool) -> None:
    email = accounts.normal_email(email)
    async with transaction() as db:
        remaining = await accounts.set_admin(db, email, value)
    if remaining is None:
        typer.echo(f"{email} is already {'' if value else 'not '}an admin")
        return
    typer.echo(f"{'Granted' if value else 'Revoked'} admin for {email} (takes effect on their next page load)")
    if not remaining:
        typer.echo("Warning: there are no admins left. Re-grant with: jobfinder users admin grant --email ...")
