"""`jobfinder boards add|list|set`: the companies table is the only list of boards; these edit it."""
from enum import Enum
from typing import Annotated

import typer

from jobfinder.cli._common import fail, run, transaction
from jobfinder.ingest.validate import ORIGINS, add_board
from jobfinder.integrations.ats.registry import ATS_FETCHERS
from jobfinder.services import boards
from jobfinder.services.ingestion import VALID_SIZES


def choices(name: str, values) -> type[Enum]:
    """A str Enum Typer checks the option against (a wrong value is a usage error); `.value` is the string."""
    return Enum(name, {v: v for v in values}, type=str)


BoardAts = choices("BoardAts", sorted({*ATS_FETCHERS, "recipe"}))  # what a companies row can hold
Source = choices("Source", [*(a.value for a in BoardAts), "jobspy", "usajobs"])  # plus non-board sources
Origin = choices("Origin", ORIGINS)
SEED = Origin("seed")
Size = choices("Size", sorted(VALID_SIZES))
YES, NO = ("yes", "true", "1"), ("no", "false", "0")  # what the old argparse CLI accepted, in any case


def value(choice: Enum | None) -> str | None:
    return choice.value if choice is not None else None

app = typer.Typer(help="Company boards: add one (validated with a live fetch), list them, edit one.",
                  no_args_is_help=True)


def yes_no(text: str | None) -> bool | None:
    if text is None:
        return None
    if text.lower() in YES + NO:
        return text.lower() in YES
    raise typer.BadParameter(f"{text!r} is not yes or no")




@app.command()
def add(
    ats: Annotated[BoardAts, typer.Option(help="The board's ATS.")],
    slug: Annotated[str, typer.Option(help="The board's slug on that ATS.")],
    name: Annotated[str, typer.Option(help="The company's display name.")],
    origin: Annotated[Origin, typer.Option(help="Where it came from.")] = SEED,
) -> None:
    """Add a board after a live fetch returns at least one posting. A duplicate is not an error."""
    run(_add(ats.value, slug, name, origin.value))


async def _add(ats: str, slug: str, name: str, origin: str) -> int:
    async with transaction() as db:
        company, reason = await add_board(db, ats, slug, name, origin=origin)
    if company:
        typer.echo(f"Added {ats}/{slug}: {reason}")
        return 0
    if reason.startswith("duplicate"):  # already there: not a failure
        typer.echo(f"Not added: {reason}")
        return 0
    fail(f"Not added: {reason}")
    return 1


@app.command("list")
def list_(
    ats: Annotated[Source | None, typer.Option(help="Only this ATS.")] = None,
    origin: Annotated[Origin | None, typer.Option(help="Only this origin.")] = None,
) -> None:
    """Every board: id, ATS, slug, name, prestige, size, industry, origin, on/off."""
    run(_list(value(ats), value(origin)))


async def _list(ats: str | None, origin: str | None) -> None:
    async with transaction() as db:
        rows = await boards.listing(db, ats=ats, origin=origin)
    for c in rows:
        state = "on" if c.enabled else "off"
        typer.echo(f"{c.id:<6} {c.ats:<16} {c.slug:<32} {c.name[:30]:<30} p={c.prestige_tier} {c.size:<7} "
                   f"{c.industry[:20]:<20} {c.origin:<10} {state}")
    typer.echo(f"\n{len(rows)} boards")


@app.command("set")
def set_(
    id: Annotated[int, typer.Option("--id", help="The board's id (see `jobfinder boards list`).")],
    name: Annotated[str | None, typer.Option(help="Company display name.")] = None,
    prestige: Annotated[int | None, typer.Option(min=1, max=5, help="Prestige tier 1-5.")] = None,
    size: Annotated[Size | None, typer.Option(help="Company size.")] = None,
    industry: Annotated[str | None, typer.Option(help="Industry.")] = None,
    # str for Typer (a bool would be a flag); the yes_no callback hands the command a bool
    sponsors_visas: Annotated[str | None, typer.Option(metavar="yes|no", callback=yes_no,
                                                       help="Whether it sponsors visas.")] = None,
    enabled: Annotated[str | None, typer.Option(metavar="yes|no", callback=yes_no,
                                                help="Fetch it or not (yes also resets its failures).")] = None,
) -> None:
    """Edit a board. --enabled yes re-enables it the way Admin › Sources does."""
    if all(v is None for v in (name, prestige, size, industry, sponsors_visas, enabled)):
        raise typer.BadParameter("give at least one of --name, --prestige, --size, --industry, "
                                 "--sponsors-visas, --enabled (nothing to change)", param_hint="options")
    run(_set(id, name=name, prestige=prestige, size=value(size), industry=industry,
             sponsors_visas=sponsors_visas, enabled=enabled))


async def _set(board_id: int, **fields) -> None:
    async with transaction() as db:
        company, changes = await boards.update(db, board_id, **fields)
        where = f"{company.ats}/{company.slug}"
    typer.echo(f"Updated {where}: " + ", ".join(f"{k}={v}" for k, v in changes.items()))
