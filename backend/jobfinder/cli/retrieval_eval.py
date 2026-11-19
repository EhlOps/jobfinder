"""`jobfinder ops eval-retrieval` (#126): each retrieval engine's top N side by side, read-only."""
import json
from typing import Annotated

import typer

from jobfinder.cli._common import run, transaction
from jobfinder.services.matching import evaluation


def eval_retrieval(
    top: Annotated[int, typer.Option(min=1, max=500, help="How many of each engine's best to compare.")] = evaluation.TOP,
    as_json: Annotated[bool, typer.Option("--json", help="Print the full report as JSON.")] = False,
    labels_version: Annotated[int | None, typer.Option(
        help="Judge by the matches scored at this profile version, not the current one.")] = None,
) -> None:
    """Compare each engine's top N (prefilter, vector, hybrid) against the jobs Claude has scored, for every
    user with scored matches and query vectors (EMBED_MODEL). Matched jobs count as labelled, so they are not
    excluded here. Writes nothing."""
    run(_eval_retrieval(top, as_json, labels_version))


def _num(v, fmt: str = "{}") -> str:
    return "-" if v is None else fmt.format(v)


async def _eval_retrieval(top: int, as_json: bool, labels_version: int | None) -> None:
    async with transaction() as db:
        report = await evaluation.evaluate_retrieval(db, top=top, labels_version=labels_version)
    if as_json:
        typer.echo(json.dumps(report, indent=2))
        return
    typer.echo(f"model {report['model']}, top {top}: scored = how many of the top {top} Claude has scored, "
               f"good = share of those rated good or strong, overlap = shared with the prefilter's top {top}")
    typer.echo(f"{'user':<6} {'ver':<4} {'labels':<7} {'engine':<10} {'cands':<6} {'scored':<7} {'mean':<6} "
               f"{'good':<5} {'overlap':<8} ranks of the {evaluation.BEST} best-scored")
    for u in report["users"]:
        for i, engine in enumerate(evaluation.ENGINES):
            e = u[engine]
            lead = f"{u['user_id']:<6} {u['labels_version']:<4} {u['labelled']:<7}" if i == 0 else " " * 19
            overlap = str(e["overlap"]) if i else ""
            ranks = " ".join(_num(r) for r in e["best_ranks"])
            typer.echo(f"{lead} {engine:<10} {e['candidates']:<6} {e['scored']:<7} {_num(e['mean_llm'], '{:.1f}'):<6} "
                       f"{_num(e['good_share'], '{:.2f}'):<5} {overlap:<8} {ranks}")
    typer.echo(f"\n{len(report['users'])} users compared")
    if report["skipped"]:
        typer.echo(f"skipped (no profile, or no query vectors for its current version): "
                   f"{', '.join(map(str, report['skipped']))}")
