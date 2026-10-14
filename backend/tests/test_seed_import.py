"""Migration 0021: the one-time import of the retired seed YAML into `companies`."""
import importlib.util
from pathlib import Path

import sqlalchemy as sa

from jobfinder.ingest.service import VALID_SIZES
from jobfinder.ingest.sources import ATS_FETCHERS
from jobfinder.ingest.validate import SLUG_RE
from jobfinder.models import Company

MIGRATION = Path(__file__).parents[1] / "alembic" / "versions" / "0021_import_seed_companies.py"
_spec = importlib.util.spec_from_file_location("migration_0021", MIGRATION)
m0021 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m0021)

# What the old load_seed() produced from companies.yaml + companies.d/*.yaml (887 rows, 247 dupes).
SEED_COUNT = 640


def test_data_file_is_valid_and_deduped():
    rows = m0021.load_rows()
    assert len(rows) == SEED_COUNT
    assert len({(r["ats"], r["slug"]) for r in rows}) == len(rows)
    for r in rows:
        assert set(r) == {"name", "ats", "slug", "prestige_tier", "size", "industry", "sponsors_visas"}, r
        assert r["name"] and r["ats"] in ATS_FETCHERS, r
        assert isinstance(r["slug"], str) and SLUG_RE.match(r["slug"]), r
        assert 1 <= r["prestige_tier"] <= 5 and r["size"] in VALID_SIZES, r
        assert isinstance(r["industry"], str) and r["sponsors_visas"] in (True, False, None), r


async def _import(engine, rows) -> int:
    async with engine.begin() as conn:
        return await conn.run_sync(lambda c: m0021.import_rows(c, rows))


async def _companies(engine) -> dict:
    async with engine.connect() as conn:
        res = await conn.execute(sa.select(Company.ats, Company.slug, Company.prestige_tier,
                                           Company.origin, Company.enabled))
        return {(r.ats, r.slug): r for r in res}


async def test_import_fills_empty_table_and_is_idempotent(engine):
    rows = m0021.load_rows()
    assert await _import(engine, rows) == SEED_COUNT
    got = await _companies(engine)
    assert len(got) == SEED_COUNT
    assert all(r.origin == "seed" and r.enabled for r in got.values())
    stripe = got[("greenhouse", "stripe")]
    assert stripe.prestige_tier == next(r["prestige_tier"] for r in rows if r["slug"] == "stripe")
    assert await _import(engine, rows) == 0
    assert len(await _companies(engine)) == SEED_COUNT


async def test_import_keeps_existing_rows_and_restores_missing(engine):
    rows = m0021.load_rows()
    first, second = rows[0], rows[1]
    async with engine.begin() as conn:
        # an existing deployment: the sync already wrote these; an admin re-tiered one and the
        # other was deleted; a user-added board sits alongside
        await conn.execute(sa.insert(Company).values(
            name="Edited", ats=first["ats"], slug=first["slug"], prestige_tier=1, origin="seed"))
        await conn.execute(sa.insert(Company).values(
            name="Mine", ats="greenhouse", slug="not-in-seed", origin="user"))
    assert await _import(engine, rows) == SEED_COUNT - 1
    got = await _companies(engine)
    assert len(got) == SEED_COUNT + 1
    assert got[(first["ats"], first["slug"])].prestige_tier == 1  # not clobbered
    assert (second["ats"], second["slug"]) in got
    assert got[("greenhouse", "not-in-seed")].origin == "user"
