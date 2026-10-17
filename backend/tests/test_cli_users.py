import pytest

from jobfinder.models import User
from tests.cli_runner import invoke
from tests.rollback import count

pytestmark = pytest.mark.real_commit


async def test_add_list_and_reset_link(maker):
    r = await invoke("users", "add", "--email", "  New@X.com ")
    assert r.exit_code == 0 and "Added new@x.com (pending)" in r.stdout
    assert await count(maker, User, email="new@x.com") == 1
    r = await invoke("users", "add", "--email", "new@x.com")
    assert r.exit_code == 1 and "already exists" in r.stderr
    r = await invoke("users", "list")
    assert r.exit_code == 0 and "new@x.com" in r.stdout and "pending" in r.stdout
    r = await invoke("users", "reset-link", "--email", "NEW@x.com")
    assert r.exit_code == 0 and "/reset-password#token=" in r.stdout
    r = await invoke("users", "reset-link", "--email", "no@x.com")  # NotFound: an AppError
    assert r.exit_code == 1 and "No user no@x.com" in r.stderr


async def test_grant_and_revoke_admin(maker):
    assert (await invoke("users", "add", "--email", " Boss@X.com ")).exit_code == 0
    r = await invoke("users", "admin", "grant", "--email", " Boss@X.com ")
    assert r.exit_code == 0 and "Granted admin for boss@x.com" in r.stdout
    assert "admin" in (await invoke("users", "list")).stdout.split("pending")[1]
    r = await invoke("users", "admin", "grant", "--email", "boss@x.com")
    assert r.exit_code == 0 and "already an admin" in r.stdout
    r = await invoke("users", "admin", "revoke", "--email", "boss@x.com")
    assert r.exit_code == 0 and "Revoked" in r.stdout and "no admins left" in r.stdout
    assert (await invoke("users", "admin", "grant", "--email", "no@x.com")).exit_code == 1


async def test_missing_email_is_a_usage_error(maker):
    assert (await invoke("users", "add")).exit_code == 2
