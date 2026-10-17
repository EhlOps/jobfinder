import pytest

from jobfinder.ingest import validate
from jobfinder.ingest.validate import ValidationResult
from jobfinder.models import Company
from tests.cli_runner import invoke
from tests.rollback import count

pytestmark = pytest.mark.real_commit


async def test_set_board_edits_and_reenables(maker):
    async with maker() as s:
        c = Company(name="Acme", ats="greenhouse", slug="acme", enabled=False, consecutive_failures=3,
                    disabled_reason="3 consecutive failures")
        s.add(c)
        await s.commit()
    assert (await invoke("boards", "set", "--id", str(c.id))).exit_code == 2  # nothing to change
    assert (await invoke("boards", "set")).exit_code == 2  # no --id
    assert (await invoke("boards", "set", "--id", str(c.id), "--prestige", "9")).exit_code == 2
    r = await invoke("boards", "set", "--id", str(c.id + 1), "--prestige", "4")
    assert r.exit_code == 1 and f"No board {c.id + 1}" in r.stderr
    r = await invoke("boards", "set", "--id", str(c.id), "--name", "  ")
    assert r.exit_code == 1 and "invalid name" in r.stderr
    r = await invoke("boards", "set", "--id", str(c.id), "--prestige", "4", "--industry", "fintech",
                     "--enabled", "yes")
    assert r.exit_code == 0 and "Updated greenhouse/acme" in r.stdout
    async with maker() as s:
        row = await s.get(Company, c.id)
    assert (row.prestige_tier, row.industry, row.enabled) == (4, "fintech", True)
    assert row.consecutive_failures == 0 and row.disabled_reason is None and row.name == "Acme"
    r = await invoke("boards", "set", "--id", str(c.id), "--enabled", "no", "--sponsors-visas", "TRUE")
    assert r.exit_code == 0, r.output  # the old CLI's true/false/1/0 still work, in any case
    async with maker() as s:
        row = await s.get(Company, c.id)
    assert (row.enabled, row.sponsors_visas) == (False, True)
    assert (await invoke("boards", "set", "--id", str(c.id), "--enabled", "maybe")).exit_code == 2
    r = await invoke("boards", "list", "--ats", "greenhouse")
    assert r.exit_code == 0 and "acme" in r.stdout and "1 boards" in r.stdout
    assert "0 boards" in (await invoke("boards", "list", "--ats", "lever")).stdout


async def test_add_board_commits(maker, monkeypatch):
    async def ok(ats, slug, client=None, list_only=False):
        return ValidationResult(True, "", 3)

    monkeypatch.setattr(validate, "validate_board", ok)
    r = await invoke("boards", "add", "--ats", "greenhouse", "--slug", "acme", "--name", "Acme")
    assert r.exit_code == 0 and "Added greenhouse/acme" in r.stdout
    assert await count(maker, Company, slug="acme", origin="seed") == 1
    r = await invoke("boards", "add", "--ats", "greenhouse", "--slug", "acme", "--name", "Acme")
    assert r.exit_code == 0 and "duplicate" in r.stdout  # a duplicate is not a failure
    assert (await invoke("boards", "add", "--ats", "jobspy", "--slug", "x", "--name", "X")).exit_code == 2


async def test_add_board_that_fails_validation_exits_1(maker, monkeypatch):
    async def bad(ats, slug, client=None, list_only=False):
        return ValidationResult(False, "board has no postings")

    monkeypatch.setattr(validate, "validate_board", bad)
    r = await invoke("boards", "add", "--ats", "lever", "--slug", "empty", "--name", "Empty")
    assert r.exit_code == 1 and "Not added: board has no postings" in r.stderr
    assert await count(maker, Company, slug="empty") == 0
