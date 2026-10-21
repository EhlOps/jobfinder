"""The admin CLI: `jobfinder <group> <verb>` (`uv run jobfinder --help`; in the containers
`docker compose exec api jobfinder ...` or `kubectl exec deploy/api -- jobfinder ...`).

Each group is a module (users, boards, ops); each verb is one transaction over the same services the
routers use (_common). Exit codes: 0 ok, 1 when the command failed (an AppError's message goes to
stderr), 2 for a usage error. `python -m jobfinder.cli` runs the same app.
"""
import typer

from jobfinder.cli import boards, ops, users

app = typer.Typer(
    name="jobfinder",
    help="JobFinder admin commands.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,  # a traceback must not print settings or tokens
)
app.add_typer(users.app, name="users")
app.add_typer(boards.app, name="boards")
app.add_typer(ops.ingest, name="ingest")
app.add_typer(ops.digest_app, name="digest")
app.add_typer(ops.questions_app, name="questions")
app.add_typer(ops.email, name="email")
app.add_typer(ops.ai, name="ai")
app.add_typer(ops.tasks, name="tasks")

