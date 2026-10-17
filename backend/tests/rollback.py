"""Helpers for per-endpoint rollback tests (#44-#46): make a request fail after its first write,
then show that nothing it wrote was kept.

    raise_after(monkeypatch, profile_service, "bump_version")   # the real call runs, then raises
    with pytest.raises(Boom):  # the test client re-raises what the app answered with a 500
        await client.post("/api/profile/facts", json=...)
    assert await count(maker, ProfileFact, user_id=uid) == 0
"""
from collections.abc import Callable
from functools import wraps
from inspect import iscoroutinefunction

import sqlalchemy as sa


class Boom(RuntimeError):
    """The failure raise_after injects."""


def raise_after(monkeypatch, target: object, attr: str) -> None:
    """Replace `target.attr` with a wrapper that runs the real function, then raises Boom."""
    real: Callable = getattr(target, attr)
    if iscoroutinefunction(real):
        @wraps(real)
        async def wrapper(*a, **kw):
            await real(*a, **kw)
            raise Boom(attr)
    else:
        @wraps(real)
        def wrapper(*a, **kw):
            real(*a, **kw)
            raise Boom(attr)
    monkeypatch.setattr(target, attr, wrapper)


async def count(maker, model, **filters) -> int:
    """Rows of `model` matching `filters`, read on a fresh session (what another request would see)."""
    async with maker() as db:
        q = sa.select(sa.func.count()).select_from(model).filter_by(**filters)
        return await db.scalar(q) or 0
