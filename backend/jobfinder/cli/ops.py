"""Operations: `ingest run`, `digest send`, `questions collect`, `email test`, `ai check`, and the task
ledger's `tasks list|retry`. `ingest run` can take a while; on k3s run it as a one-off Job."""
from typing import Annotated, Literal

import typer

from jobfinder.cli._common import fail, run, sessions, transaction
from jobfinder.cli.boards import Source
from jobfinder.context import default_context
from jobfinder.core.clock import utcnow
from jobfinder.ingest.runner import run_ingest
from jobfinder.integrations.claude.claude_code import AIError
from jobfinder.integrations.smtp import EmailError, send_email
from jobfinder.notify import daily
from jobfinder.services import accounts, digest, questions, task_admin

ingest = typer.Typer(help="Fetch jobs from the boards.", no_args_is_help=True)
digest_app = typer.Typer(help="The daily email.", no_args_is_help=True)
questions_app = typer.Typer(help="Clarifying questions.", no_args_is_help=True)
email = typer.Typer(help="Outbound email.", no_args_is_help=True)
ai = typer.Typer(help="The Claude connection.", no_args_is_help=True)
tasks = typer.Typer(help="The task ledger: what ran, what failed, run one again.", no_args_is_help=True)

Email = Annotated[str | None, typer.Option(help="Only this user.")]


# ── ingest ────────────────────────────────────────────────────────────────
@ingest.command("run")
def ingest_run(
    ats: Annotated[Source | None, typer.Option(help="Only this ATS.")] = None,
    company: Annotated[str | None, typer.Option(help="Only this board slug.")] = None,
    jobspy: Annotated[bool, typer.Option(help="Also scrape LinkedIn/Indeed (needs the jobspy extra).")] = False,
) -> None:
    """Fetch every enabled board now (a full run, not only the due ones). Exits 1 when every board failed."""
    run(_ingest(ats.value if ats else None, company, jobspy))


async def _ingest(ats: str | None, company: str | None, jobspy: bool) -> int:
    # run_ingest commits per board (each board is its own boundary), so it takes the session factory.
    out = await run_ingest(sessions(), ats=ats, slug=company, jobspy=True if jobspy else None, all_boards=True)
    for r in out["detail"]:
        status = f"ERROR: {r['error']}" if r["error"] else "ok"
        typer.echo(f"{r['company']:<22} {r['source']:<10} fetched={r['fetched']:<4} new={r['new']:<4} "
                   f"updated={r['updated']:<4} deactivated={r['deactivated']:<3} {status}")
    typer.echo(f"\nTotal: {out['companies']} boards, {out['fetched']} jobs fetched, {out['new']} new, "
               f"{out['updated']} updated, {out['deactivated']} deactivated, {len(out['errors'])} errors")
    return 1 if out["errors"] and out["fetched"] == 0 else 0


# ── digest ────────────────────────────────────────────────────────────────
@digest_app.command("send")
def digest_send(
    email: Email = None,
    preview: Annotated[bool, typer.Option(help="With --email: send without marking anything as sent.")] = False,
) -> None:
    """Send the daily email to every user whose hour has come, or with --email to that user now."""
    if preview and not email:
        raise typer.BadParameter("needs --email", param_hint="--preview")
    run(_digest(email, preview))


async def _digest(email: str | None, preview: bool) -> int:
    async with transaction() as db:
        if not email:
            out = await digest.send_due(db, default_context().claude, utcnow())
            typer.echo(out)
            return 1 if out["errors"] else 0
        user = await accounts.by_email(db, email)
        try:
            result = await daily.send_daily(db, user, preview=preview)
        except EmailError as e:  # send_daily already gave the claimed matches back
            fail(f"FAILED: {e}")
            return 1
    typer.echo(result or "Nothing to send (no new matches or open questions).")
    return 0


# ── questions ─────────────────────────────────────────────────────────────
@questions_app.command("collect")
def questions_collect(email: Email = None) -> None:
    """Turn new matches' unknowns into clarifying questions, for every active user or one."""
    run(_collect(email))


async def _collect(email: str | None) -> None:
    ai_tasks = default_context().claude
    async with transaction() as db:
        for user in await accounts.active_users(db, email):
            try:
                out = await questions.collect_questions(db, ai_tasks, user.id)
                await db.commit()  # each user is the boundary: a later failure keeps this one's questions
                typer.echo(f"{user.email} {out}")
            except AIError as e:  # raised before anything is staged: nothing to roll back
                fail(f"{user.email} FAILED: {e}")


# ── email ─────────────────────────────────────────────────────────────────
@email.command("test")
def email_test(to: Annotated[str, typer.Option(help="The recipient.")]) -> None:
    """Send a test email through the configured SMTP server."""
    run(_test_email(to))


async def _test_email(to: str) -> int:
    try:
        await send_email(to, "JobFinder test email", "If you can read this, outbound email works.",
                         "<p>If you can read this, <b>outbound email works</b>.</p>")
    except EmailError as e:
        fail(f"FAILED: {e}")
        return 1
    typer.echo(f"Sent a test email to {to}")
    return 0


# ── ai ────────────────────────────────────────────────────────────────────
@ai.command("check")
def ai_check() -> None:
    """Ping Claude with the stored token. Exits 1 when it fails."""
    run(_ai_check())


async def _ai_check() -> int:
    try:
        await default_context().claude.ping()
    except AIError as e:
        fail(f"AI check FAILED: {e}")
        if e.kind == "auth":
            fail("Sign in as the admin and paste your `claude setup-token` token on the AI setup page.")
        return 1
    typer.echo("AI check ok")
    return 0


# ── tasks ─────────────────────────────────────────────────────────────────
Status = Literal["queued", "running", "done", "failed"]


@tasks.command("list")
def tasks_list(
    status: Annotated[Status | None, typer.Option(help="Only this status.")] = None,
    kind: Annotated[str | None, typer.Option(help="Only this kind, e.g. ingest_board.")] = None,
    limit: Annotated[int, typer.Option(min=1, max=1000, help="How many, newest first.")] = 50,
) -> None:
    """The newest tasks: id, kind, queue, status, attempts, user, created, finished."""
    run(_tasks_list(status, kind, limit))


async def _tasks_list(status: str | None, kind: str | None, limit: int) -> None:
    async with transaction() as db:
        rows = await task_admin.recent(db, status=status, kind=kind, limit=limit)
    typer.echo(f"{'id':<8} {'kind':<24} {'queue':<7} {'status':<8} {'att':<4} {'user':<6} "
               f"{'created':<17} finished")
    for t in rows:
        finished = f"{t.finished_at:%Y-%m-%d %H:%M}" if t.finished_at else "-"
        typer.echo(f"{t.id:<8} {t.kind:<24} {t.queue:<7} {t.status:<8} {t.attempts:<4} "
                   f"{t.user_id or '-':<6} {t.created_at:%Y-%m-%d %H:%M} {finished}")
    typer.echo(f"\n{len(rows)} tasks")


@tasks.command("retry")
def tasks_retry(task_id: Annotated[int, typer.Argument(help="The task's id (see `jobfinder tasks list`).")]) -> None:
    """Run a finished or failed task again from scratch. Exits 1 while it is still queued or running."""
    run(_tasks_retry(task_id))


async def _tasks_retry(task_id: int) -> None:
    async with transaction() as db:
        task = await task_admin.retry(db, task_id)
        kind, queue = task.kind, task.queue
    typer.echo(f"Requeued task {task_id} ({kind}) on the {queue} queue")
