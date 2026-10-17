"""Admin commands: `python -m jobfinder.cli ai-check`, `add-user`, `reset-link`, `list-users`, `add-board`,
`list-boards`, `set-board`. The `companies` table is the only list of boards; these edit it."""
import argparse
import asyncio

from jobfinder.integrations.claude.claude_code import AIError


async def ai_check() -> int:
    from jobfinder.context import default_context

    try:
        await default_context().claude.ping()
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

    out = await run_ingest(
        SessionLocal, ats=args.ats, slug=args.company, jobspy=True if args.jobspy else None, all_boards=True
    )
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

    from jobfinder.context import default_context
    from jobfinder.db import SessionLocal
    from jobfinder.integrations.smtp import EmailError
    from jobfinder.models import User
    from jobfinder.notify import daily as daily_mod
    from jobfinder.scheduling.handlers import daily_emails

    async with SessionLocal() as db:
        if not args.email:
            from jobfinder.models import Task

            out = await daily_emails(db, default_context().claude, Task(kind="daily_emails", payload={}))
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

    from jobfinder.context import default_context
    from jobfinder.db import SessionLocal
    from jobfinder.models import User
    from jobfinder.services import questions as service

    ai = default_context().claude
    async with SessionLocal() as db:
        q = sa.select(User).where(User.password_hash.is_not(None))
        if args.email:
            q = q.where(User.email == args.email.lower())
        for user in (await db.scalars(q)).all():
            try:
                out = await service.collect_questions(db, ai, user.id)
                await db.commit()  # each user is the boundary: a later failure keeps this one's questions
                print(user.email, out)
            except AIError as e:  # raised before anything is staged: nothing to roll back
                print(user.email, f"FAILED: {e}")
    return 0


async def test_email(args: argparse.Namespace) -> int:
    from jobfinder.integrations.smtp import EmailError, send_email

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
    from jobfinder.db import SessionLocal
    from jobfinder.services import accounts

    if not args.email:
        print("Usage: add-user --email x@y.com")
        return 2
    email = args.email.strip().lower()
    async with SessionLocal() as db:
        if not await accounts.add_user(db, email):
            print(f"{email} already exists")
            return 1
        await db.commit()
    print(f"Added {email} (pending). They can request a link on the sign-in page, or run: reset-link --email {email}")
    return 0


async def reset_link_cmd(args: argparse.Namespace) -> int:
    from jobfinder.core.errors import NotFound
    from jobfinder.db import SessionLocal
    from jobfinder.services import accounts

    if not args.email:
        print("Usage: reset-link --email x@y.com")
        return 2
    async with SessionLocal() as db:
        try:
            print(await accounts.link_for(db, args.email))
        except NotFound:
            print(f"No user {args.email}")
            return 1
    return 0


async def set_admin(args: argparse.Namespace, value: bool) -> int:
    from jobfinder.core.errors import NotFound
    from jobfinder.db import SessionLocal
    from jobfinder.services import accounts

    verb = "grant-admin" if value else "revoke-admin"
    if not args.email:
        print(f"Usage: {verb} --email x@y.com")
        return 2
    email = args.email.strip().lower()
    async with SessionLocal() as db:
        try:
            remaining = await accounts.set_admin(db, email, value)
        except NotFound as e:
            print(e.detail)
            return 1
        if remaining is None:
            print(f"{email} is already {'' if value else 'not '}an admin")
            return 0
        await db.commit()
    print(f"{'Granted' if value else 'Revoked'} admin for {email} (takes effect on their next page load)")
    if not remaining:
        print("Warning: there are no admins left. Re-grant with: grant-admin --email ...")
    return 0


async def list_users() -> int:
    import sqlalchemy as sa

    from jobfinder.db import SessionLocal
    from jobfinder.models import User

    async with SessionLocal() as db:
        for u in (await db.scalars(sa.select(User).order_by(User.id))).all():
            state = "active" if u.password_hash else "pending"
            print(f"{u.id:<4} {u.email:<40} {state:<8} {'admin' if u.is_admin else '':<6} "
                  f"created={u.created_at:%Y-%m-%d} activated={u.activated_at:%Y-%m-%d}" if u.activated_at else
                  f"{u.id:<4} {u.email:<40} {state:<8} {'admin' if u.is_admin else '':<6} created={u.created_at:%Y-%m-%d}")
    return 0


async def add_board_cmd(args: argparse.Namespace) -> int:
    from jobfinder.db import SessionLocal
    from jobfinder.ingest.validate import add_board

    if not (args.ats and args.slug and args.name):
        print("Usage: add-board --ats greenhouse --slug acme --name 'Acme Inc' [--origin seed|discovered]")
        return 2
    if args.ats in ("jobspy", "usajobs"):
        print(f"{args.ats} is not a board ATS")
        return 2
    async with SessionLocal() as db:
        company, reason = await add_board(db, args.ats, args.slug, args.name, origin=args.origin or "seed")
        await db.commit()  # the command is the boundary
    if company:
        print(f"Added {args.ats}/{args.slug}: {reason}")
        return 0
    print(f"Not added: {reason}")
    return 0 if reason.startswith("duplicate") else 1


async def list_boards(args: argparse.Namespace) -> int:
    import sqlalchemy as sa

    from jobfinder.db import SessionLocal
    from jobfinder.models import Company

    q = sa.select(Company).order_by(Company.ats, Company.slug)
    if args.ats:
        q = q.where(Company.ats == args.ats)
    if args.origin:
        q = q.where(Company.origin == args.origin)
    async with SessionLocal() as db:
        boards = (await db.scalars(q)).all()
    for c in boards:
        state = "on" if c.enabled else "off"
        print(f"{c.id:<6} {c.ats:<16} {c.slug:<32} {c.name[:30]:<30} p={c.prestige_tier} {c.size:<7} "
              f"{c.industry[:20]:<20} {c.origin:<10} {state}")
    print(f"\n{len(boards)} boards")
    return 0


async def set_board(args: argparse.Namespace) -> int:
    from jobfinder.db import SessionLocal
    from jobfinder.models import Company

    name = args.name.strip() if args.name is not None else None
    if name is not None and not 1 <= len(name) <= 200:
        print("invalid name (must be 1-200 characters)")
        return 2
    if args.industry is not None and len(args.industry) > 100:
        print("invalid industry (at most 100 characters)")
        return 2
    changes = {k: v for k, v in (
        ("name", name), ("prestige_tier", args.prestige), ("size", args.size),
        ("industry", args.industry), ("sponsors_visas", args.sponsors_visas),
    ) if v is not None}
    if not args.id or not (changes or args.enabled is not None):
        print("Usage: set-board --id N [--name X] [--prestige 1-5] [--size startup|mid|large] "
              "[--industry X] [--sponsors-visas yes|no] [--enabled yes|no]")
        return 2
    async with SessionLocal() as db:
        company = await db.get(Company, args.id)
        if not company:
            print(f"No board {args.id}")
            return 1
        for k, v in changes.items():
            setattr(company, k, v)
        if args.enabled is False:
            company.enabled = False
        elif args.enabled:
            from jobfinder.services.admin import enable_source

            await enable_source(db, company.id)  # the admin page's re-enable rule
        if args.enabled is not None:
            changes["enabled"] = args.enabled
        await db.commit()
    print(f"Updated {company.ats}/{company.slug}: " + ", ".join(f"{k}={v}" for k, v in changes.items()))
    return 0


def _yes_no(v: str) -> bool:
    if v.lower() in ("yes", "true", "1"):
        return True
    if v.lower() in ("no", "false", "0"):
        return False
    raise argparse.ArgumentTypeError("expected yes or no")


def main() -> None:
    from jobfinder.integrations.ats.registry import ATS_FETCHERS

    parser = argparse.ArgumentParser(prog="jobfinder.cli")
    parser.add_argument("command", choices=["ai-check", "ingest", "daily", "questions", "test-email", "add-user", "reset-link", "list-users", "add-board", "list-boards", "set-board", "grant-admin", "revoke-admin"])
    parser.add_argument("--ats", choices=[*sorted({*ATS_FETCHERS, "recipe"}), "jobspy", "usajobs"], help="ingest/add-board/list-boards: ATS")
    parser.add_argument("--company", help="ingest: only this company slug")
    parser.add_argument("--jobspy", action="store_true", help="ingest: also scrape LinkedIn/Indeed (needs the jobspy extra)")
    parser.add_argument("--email", help="daily/questions: only this user; add-user/reset-link/grant-admin/revoke-admin: the user")
    parser.add_argument("--preview", action="store_true", help="daily --email: send without marking anything as sent")
    parser.add_argument("--to", help="test-email: recipient")
    parser.add_argument("--slug", help="add-board: board slug")
    parser.add_argument("--name", help="add-board/set-board: company display name")
    parser.add_argument("--origin", choices=["seed", "discovered", "user"],
                        help="add-board: origin (default seed); list-boards: only this origin")
    parser.add_argument("--id", type=int, help="set-board: board id (see list-boards)")
    parser.add_argument("--prestige", type=int, choices=range(1, 6), help="set-board: prestige tier 1-5")
    parser.add_argument("--size", choices=["startup", "mid", "large"], help="set-board: company size")
    parser.add_argument("--industry", help="set-board: industry")
    parser.add_argument("--sponsors-visas", type=_yes_no, help="set-board: yes|no")
    parser.add_argument("--enabled", type=_yes_no, help="set-board: yes|no")
    args = parser.parse_args()
    handlers = {
        "ai-check": lambda: ai_check(), "ingest": lambda: ingest(args), "daily": lambda: daily(args),
        "questions": lambda: collect(args), "test-email": lambda: test_email(args),
        "add-user": lambda: add_user(args), "reset-link": lambda: reset_link_cmd(args), "list-users": lambda: list_users(), "add-board": lambda: add_board_cmd(args),
        "list-boards": lambda: list_boards(args), "set-board": lambda: set_board(args),
        "grant-admin": lambda: set_admin(args, True), "revoke-admin": lambda: set_admin(args, False),
    }
    raise SystemExit(asyncio.run(handlers[args.command]()))


if __name__ == "__main__":
    main()
