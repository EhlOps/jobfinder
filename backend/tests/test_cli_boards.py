import argparse

from jobfinder import cli
from jobfinder.models import Company


def ns(**kw):
    keys = ("id", "name", "prestige", "size", "industry", "sponsors_visas", "enabled", "ats", "origin")
    return argparse.Namespace(**{**dict.fromkeys(keys), **kw})


async def test_set_board_edits_and_reenables(maker, capsys):
    async with maker() as s:
        c = Company(name="Acme", ats="greenhouse", slug="acme", enabled=False, consecutive_failures=3,
                    disabled_reason="3 consecutive failures")
        s.add(c)
        await s.commit()
    assert await cli.set_board(ns(id=c.id)) == 2
    assert await cli.set_board(ns(id=c.id + 1, prestige=4)) == 1
    assert await cli.set_board(ns(id=c.id, name="  ")) == 2
    assert await cli.set_board(ns(id=c.id, prestige=4, industry="fintech", enabled=True)) == 0
    async with maker() as s:
        row = await s.get(Company, c.id)
    assert (row.prestige_tier, row.industry, row.enabled) == (4, "fintech", True)
    assert row.consecutive_failures == 0 and row.disabled_reason is None and row.name == "Acme"
    assert await cli.list_boards(ns(ats="greenhouse")) == 0
    assert "acme" in capsys.readouterr().out
