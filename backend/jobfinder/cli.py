"""Admin commands: `python -m jobfinder.cli ai-check`, `add-user`, `reset-link`, `list-users`."""
import argparse
import asyncio

from jobfinder.ai.claude_code import AIError, ClaudeCode


async def ai_check() -> int:
    from jobfinder.ai.tasks import AITasks

    try:
        await AITasks(ClaudeCode()).ping()
    except AIError as e:
        print(f"AI check FAILED: {e}")
        if e.kind == "auth":
            print("Sign in as the admin and paste your `claude setup-token` token on the AI setup page.")
        return 1
    print("AI check ok")
    return 0


async def ingest(args: argparse.Namespace) -> int:
    from jobfinder.db import SessionLocal
    from jobfinder.ingest.runner import run_ingest

    out = await run_ingest(SessionLocal, ats=args.ats, slug=args.company, jobspy=True if args.jobspy else None)
    for r in out["detail"]:
        status = f"ERROR: {r['error']}" if r["error"] else "ok"
        print(f"{r['company']:<22} {r['source']:<10} fetched={r['fetched']:<4} new={r['new']:<4} "
              f"updated={r['updated']:<4} deactivated={r['deactivated']:<3} {status}")
    print(f"\nTotal: {out['companies']} boards, {out['fetched']} jobs fetched, {out['new']} new, "
          f"{out['updated']} updated, {out['deactivated']} deactivated, {len(out['errors'])} errors")
    return 1 if out["errors"] and out["fetched"] == 0 else 0


async def daily(args: argparse.Namespace) -> int:
    """Send the daily email. With --email: that user now (add --preview to mark nothing as sent)."""
    import sqlalchemy as sa

    from jobfinder.ai.tasks import AITasks
    from jobfinder.db import SessionLocal
    from jobfinder.models import User
    from jobfinder.notify import daily as daily_mod
    from jobfinder.notify.email import EmailError
    from jobfinder.scheduling.handlers import daily_emails

    async with SessionLocal() as db:
        if not args.email:
            from jobfinder.models import Task

            out = await daily_emails(db, AITasks(ClaudeCode()), Task(kind="daily_emails", payload={}))
            print(out)
            return 1 if out["errors"] else 0
        user = (await db.scalars(sa.select(User).where(User.email == args.email.lower()))).first()
        if not user:
            print(f"No user {args.email}")
            return 1
        try:
            result = await daily_mod.send_daily(db, user, preview=args.preview)
        except EmailError as e:
            print(f"FAILED: {e}")
            return 1
        print(result or "Nothing to send (no new matches or open questions).")
    return 0


async def collect(args: argparse.Namespace) -> int:
    import sqlalchemy as sa

    from jobfinder.ai.tasks import AITasks
    from jobfinder.db import SessionLocal
    from jobfinder.models import User
    from jobfinder.questions import service

    ai = AITasks(ClaudeCode())
    async with SessionLocal() as db:
        q = sa.select(User).where(User.password_hash.is_not(None))
        if args.email:
            q = q.where(User.email == args.email.lower())
        for user in (await db.scalars(q)).all():
            try:
                print(user.email, await service.collect_questions(db, ai, user.id))
            except AIError as e:
                print(user.email, f"FAILED: {e}")
    return 0


async def test_email(args: argparse.Namespace) -> int:
    from jobfinder.notify.email import EmailError, send_email

    if not args.to:
        print("Usage: test-email --to you@example.com")
        return 2
    try:
        await send_email(args.to, "JobFinder test email", "If you can read this, outbound email works.", "<p>If you can read this, <b>outbound email works</b>.</p>")
    except EmailError as e:
        print(f"FAILED: {e}")
        return 1
    print(f"Sent a test email to {args.to}")
    return 0


async def add_user(args: argparse.Namespace) -> int:
    import sqlalchemy as sa

    from jobfinder.db import SessionLocal
    from jobfinder.models import User

    if not args.email:
        print("Usage: add-user --email x@y.com")
        return 2
    email = args.email.strip().lower()
    async with SessionLocal() as db:
        if (await db.scalars(sa.select(User).where(User.email == email))).first():
            print(f"{email} already exists")
            return 1
        db.add(User(email=email))
        await db.commit()
    print(f"Added {email} (pending). They can request a link on the sign-in page, or run: reset-link --email {email}")
    return 0


async def reset_link_cmd(args: argparse.Namespace) -> int:
    import sqlalchemy as sa

    from jobfinder.auth.routes import reset_link
    from jobfinder.db import SessionLocal
    from jobfinder.models import User

    if not args.email:
        print("Usage: reset-link --email x@y.com")
        return 2
    async with SessionLocal() as db:
        user = (await db.scalars(sa.select(User).where(User.email == args.email.strip().lower()))).first()
        if not user:
            print(f"No user {args.email}")
            return 1
        print(reset_link(user))
    return 0


async def list_users() -> int:
    import sqlalchemy as sa

    from jobfinder.auth.security import is_admin_email
    from jobfinder.db import SessionLocal
    from jobfinder.models import User

    async with SessionLocal() as db:
        for u in (await db.scalars(sa.select(User).order_by(User.id))).all():
            state = "active" if u.password_hash else "pending"
            print(f"{u.id:<4} {u.email:<40} {state:<8} {'admin' if is_admin_email(u.email) else '':<6} "
                  f"created={u.created_at:%Y-%m-%d} activated={u.activated_at:%Y-%m-%d}" if u.activated_at else
                  f"{u.id:<4} {u.email:<40} {state:<8} {'admin' if is_admin_email(u.email) else '':<6} created={u.created_at:%Y-%m-%d}")
    return 0


async def add_board_cmd(args: argparse.Namespace) -> int:
    from jobfinder.db import SessionLocal
    from jobfinder.ingest.validate import add_board

    if not (args.ats and args.slug and args.name):
        print("Usage: add-board --ats greenhouse --slug acme --name 'Acme Inc' [--origin seed|discovered]")
        return 2
    if args.ats == "jobspy":
        print("jobspy is not a board ATS")
        return 2
    async with SessionLocal() as db:
        company, reason = await add_board(db, args.ats, args.slug, args.name, origin=args.origin or "seed")
    if company:
        print(f"Added {args.ats}/{args.slug}: {reason}")
        return 0
    print(f"Not added: {reason}")
    return 0 if reason.startswith("duplicate") else 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="jobfinder.cli")
    parser.add_argument("command", choices=["ai-check", "ingest", "daily", "questions", "test-email", "add-user", "reset-link", "list-users", "add-board"])
    parser.add_argument("--ats", choices=["greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee", "bamboohr", "jobspy"], help="ingest/add-board: ATS")
    parser.add_argument("--company", help="ingest: only this company slug")
    parser.add_argument("--jobspy", action="store_true", help="ingest: also scrape LinkedIn/Indeed (needs the jobspy extra)")
    parser.add_argument("--email", help="daily/questions: only this user's email address")
    parser.add_argument("--preview", action="store_true", help="daily --email: send without marking anything as sent")
    parser.add_argument("--to", help="test-email: recipient")
    parser.add_argument("--slug", help="add-board: board slug")
    parser.add_argument("--name", help="add-board: company display name")
    parser.add_argument("--origin", choices=["seed", "discovered"], help="add-board: origin (default seed)")
    args = parser.parse_args()
    handlers = {
        "ai-check": lambda: ai_check(), "ingest": lambda: ingest(args), "daily": lambda: daily(args),
        "questions": lambda: collect(args), "test-email": lambda: test_email(args),
        "add-user": lambda: add_user(args), "reset-link": lambda: reset_link_cmd(args), "list-users": lambda: list_users(), "add-board": lambda: add_board_cmd(args),
    }
    raise SystemExit(asyncio.run(handlers[args.command]()))


if __name__ == "__main__":
    main()
