from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from jobfinder.config import get_settings
from jobfinder.ingest import service
from jobfinder.ingest.selection import Target, board_score, select_boards
from jobfinder.models import Company, Profile, User
from tests.test_ingest_service import fake_fetcher, posting

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def board(i, origin="discovered", industry="", size="mid", name=None, enabled=True, fetched=None, tier=3):
    return SimpleNamespace(
        id=i, name=name or f"Co{i}", industry=industry, size=size, prestige_tier=tier,
        origin=origin, enabled=enabled, last_fetched_at=fetched,
    )


def ids(boards):
    return [b.id for b in boards]


def test_ranking_with_several_users():
    fintech = Target.from_status({"industries": ["Fintech"], "company_sizes": ["startup"]})
    ml = Target.from_status({"target_roles": ["machine learning engineer"], "industries": ["AI"]})
    both = board(1, industry="fintech ai", size="startup")
    f_only = board(2, industry="fintech", size="large")
    ml_only = board(3, industry="ai research")
    none = board(4, industry="retail", size="large")
    scores = {b.id: board_score(b, [fintech, ml]) for b in (both, f_only, ml_only, none)}
    assert scores[1] > scores[2] > 0 and scores[3] > 0 and scores[4] == 0
    assert board_score(f_only, []) == 0


def test_cap_honoured_and_relevant_first():
    t = [Target(industries=["fintech"])]
    rel = [board(i, industry="fintech") for i in range(1, 5)]
    irr = [board(i, industry="retail") for i in range(5, 15)]
    got = select_boards(rel + irr, t, cap=4)
    assert len(got) == 4
    assert {1, 2, 3}.issubset(ids(got))  # cap - reserve slots go to relevant boards


def test_seed_always_included_and_disabled_excluded():
    boards = [board(1, origin="seed"), board(2, origin="seed", enabled=False)]
    boards += [board(i, industry="x") for i in range(3, 10)]
    boards.append(board(10, enabled=False))
    got = ids(select_boards(boards, [], cap=2))
    assert 1 in got and 2 not in got and 10 not in got
    assert len([i for i in got if i >= 3]) == 2


def test_no_discovered_keeps_everything():
    boards = [board(i, origin="seed") for i in range(1, 6)]
    assert ids(select_boards(boards, [], cap=1)) == [1, 2, 3, 4, 5]
    assert ids(select_boards(boards, [], cap=0)) == [1, 2, 3, 4, 5]


@pytest.mark.parametrize("cap", [1, 2, 4])
def test_rotation_covers_all_boards(cap):
    t = [Target(industries=["fintech"])]
    boards = [board(i, industry="fintech" if i % 3 == 0 else "retail") for i in range(1, 13)]
    seen: set[int] = set()
    for run in range(12):
        got = select_boards(boards, t, cap=cap)
        assert len(got) == cap
        for b in got:
            b.last_fetched_at = T0 + timedelta(hours=run)
        seen |= set(ids(got))
    assert seen == set(range(1, 13))


def test_never_fetched_come_before_stale():
    old = board(1, fetched=T0)
    fresh = board(2, fetched=T0 + timedelta(days=1))
    new = board(3)
    assert ids(select_boards([old, fresh, new], [], cap=1)) == [3]
    assert ids(select_boards([old, fresh], [], cap=1)) == [1]


async def test_ingest_boards_uses_selection(engine, monkeypatch):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(get_settings(), "ingest_max_discovered_boards_per_run", 1)
    monkeypatch.setattr(service, "load_seed", list)
    monkeypatch.setitem(service.ATS_FETCHERS, "greenhouse", fake_fetcher([posting(1)]))
    async with maker() as db:
        u = User(email="a@x.io", activated_at=datetime.now(UTC))
        db.add(u)
        await db.flush()
        db.add(Profile(user_id=u.id, status={"industries": ["fintech"]}))
        db.add_all([
            Company(name="Seed", ats="greenhouse", slug="seed", origin="seed"),
            Company(name="Bank", ats="greenhouse", slug="bank", origin="discovered", industry="fintech"),
            Company(name="Shop", ats="greenhouse", slug="shop", origin="discovered", industry="retail"),
        ])
        await db.commit()
    results = await service.ingest_boards(maker)
    assert sorted(r.company for r in results) == ["Bank", "Seed"]
    # an explicitly requested board is never capped away
    results = await service.ingest_boards(maker, slug="shop")
    assert [r.company for r in results] == ["Shop"]
