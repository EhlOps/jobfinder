"""The rollback-test helpers, and the rate events that must survive a failed request (side_session)."""
import pytest

from jobfinder.config import get_settings
from jobfinder.models import RateEvent
from jobfinder.services import ratelimit
from tests.rollback import Boom, count, raise_after


async def test_raise_after_keeps_the_real_call_and_nothing_persists(client, make_account, maker, monkeypatch):
    await make_account()
    raise_after(monkeypatch, ratelimit, "hit")  # the job-links hit, the endpoint's first write
    monkeypatch.setattr(get_settings(), "job_links_enabled", True)
    with pytest.raises(Boom):
        await client.post("/api/job-links", json={"url": "https://boards.greenhouse.io/acme/jobs/1"})
    assert await count(maker, RateEvent, kind="job_link") == 0


@pytest.mark.real_commit
async def test_failed_logins_count_although_the_401_rolls_back(client, make_account, maker):
    await make_account("known@x.com", "right-password-1", login=False)
    r = await client.post("/api/auth/login", json={"email": "known@x.com", "password": "bad-password-1"})
    assert r.status_code == 401
    assert await count(maker, RateEvent, kind="login_fail_email", key="known@x.com") == 1
    assert await count(maker, RateEvent, kind="login_fail_ip") == 1


@pytest.mark.real_commit
async def test_a_preview_that_has_nothing_to_send_still_counts(client, make_account, maker):
    await make_account()
    r = await client.post("/api/settings/digest/preview")
    assert r.status_code == 409
    assert await count(maker, RateEvent, kind="preview_email") == 1
