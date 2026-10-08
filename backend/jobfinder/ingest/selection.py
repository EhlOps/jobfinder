"""Pick which company boards one ingest run fetches (REQ-26 d).

Seed boards always run. Discovered boards (origin 'discovered') are capped per run: most of the
cap goes to boards relevant to active users' targets, the rest to whichever boards have gone
longest without a fetch, so every board is eventually visited.
"""
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from jobfinder.matching.prefilter import _tokens

_EPOCH = datetime.min.replace(tzinfo=UTC)


class BoardLike(Protocol):
    id: int
    name: str
    industry: str
    size: str
    prestige_tier: int
    origin: str
    enabled: bool
    last_fetched_at: datetime | None


@dataclass
class Target:
    roles: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)
    sizes: list[str] = field(default_factory=list)

    @classmethod
    def from_status(cls, status: dict | None) -> "Target":
        s = status or {}
        return cls(
            roles=[r for r in s.get("target_roles") or [] if r],
            industries=[i.lower() for i in s.get("industries") or [] if i],
            sizes=list(s.get("company_sizes") or []),
        )


def _one_score(board: BoardLike, t: Target) -> float:
    industry = _tokens(board.industry or "")
    score = 0.0
    if industry and any(industry & _tokens(i) for i in t.industries):
        score += 2.0
    if board.size and board.size in t.sizes:
        score += 1.0
    if t.roles:
        words = _tokens(f"{board.name} {board.industry or ''}")
        if words and any(_tokens(r) & words for r in t.roles):
            score += 1.0
    return score


def board_score(board: BoardLike, targets: list[Target]) -> float:
    """Relevance of a board to the users' targets: best single-user overlap, plus a small
    bonus per additional user it fits, plus a tiebreak for prestige. 0 means no overlap."""
    scores = sorted((_one_score(board, t) for t in targets), reverse=True)
    scores = [s for s in scores if s > 0]
    if not scores:
        return 0.0
    return scores[0] + 0.25 * sum(scores[1:]) + 0.01 * (board.prestige_tier or 0)


def _when(b: BoardLike) -> datetime:
    t = b.last_fetched_at
    if t is None:
        return _EPOCH
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _stalest(boards: list[BoardLike], scores: dict[int, float]) -> list[BoardLike]:
    # never fetched first, then oldest fetch; better-ranked first within a tie
    return sorted(boards, key=lambda b: (_when(b), -scores[b.id], b.id))


def select_boards(boards: list[BoardLike], targets: list[Target], cap: int) -> list[BoardLike]:
    """Enabled seed boards plus at most `cap` discovered ones. cap <= 0 means no cap."""
    live = [b for b in boards if b.enabled]
    seeds = [b for b in live if b.origin != "discovered"]
    disc = [b for b in live if b.origin == "discovered"]
    if cap <= 0 or len(disc) <= cap:
        return sorted(seeds + disc, key=lambda b: b.id)

    scores = {b.id: board_score(b, targets) for b in disc}
    reserve = max(1, cap // 4)  # slots for the stalest boards: they rotate through everything else
    relevant = sorted((b for b in disc if scores[b.id] > 0), key=lambda b: (-scores[b.id], b.id))
    chosen = relevant[: cap - reserve]
    taken = {b.id for b in chosen}
    chosen += _stalest([b for b in disc if b.id not in taken], scores)[: cap - len(chosen)]
    return sorted(seeds + chosen, key=lambda b: b.id)
