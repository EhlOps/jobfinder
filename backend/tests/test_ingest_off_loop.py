"""Board parsing runs off the event loop (#48): a slow parse must not stall the worker's other
coroutines (the task heartbeat, the broker's stream read)."""
import asyncio
import time

import httpx
import pytest

from jobfinder.integrations import http_safety
from jobfinder.integrations.ats import greenhouse
from jobfinder.integrations.recipes import html as html_recipe
from jobfinder.integrations.recipes import json_api, parse_recipe, run_recipe
from tests.fakes import fake_resolve_public, serve

SLOW = 0.5
TICK = 0.05


def slow_parse(*_args, **_kwargs):
    time.sleep(SLOW)  # stands in for html_to_text etc. over a big board
    return []


async def ticks_during(coro) -> int:
    """Run `coro` while a ticker counts 50 ms beats; the beats it managed while `coro` ran."""
    beats = 0
    done = asyncio.Event()

    async def ticker():
        nonlocal beats
        while not done.is_set():
            await asyncio.sleep(TICK)
            beats += 1

    task = asyncio.create_task(ticker())
    try:
        assert await coro == []
    finally:
        done.set()
        await task
    return beats


async def test_greenhouse_parse_does_not_block_the_loop(web, monkeypatch):
    monkeypatch.setattr(greenhouse, "parse_greenhouse", slow_parse)
    serve(web, "https://boards-api.greenhouse.io/v1/boards/acme/jobs", json={"jobs": []})
    async with httpx.AsyncClient() as c:
        beats = await ticks_during(greenhouse.fetch_greenhouse(c, "acme", "Acme"))
    assert beats >= SLOW / TICK / 2  # a blocked loop gets ~0-1


@pytest.mark.parametrize("module, name, kind, response", [
    (json_api, "_map_page", "json_api", {"json": {"jobs": []}}),
    (html_recipe, "map_items", "html", {"html": "<ul></ul>"}),
])
async def test_recipe_parse_does_not_block_the_loop(web, monkeypatch, module, name, kind, response):
    monkeypatch.setattr(http_safety, "resolve_public", fake_resolve_public())
    monkeypatch.setattr(module, name, slow_parse)
    fields = {"external_id": "$.id", "title": "$.title", "url": "https://careers.acme.com/jobs/{external_id}"}
    if kind == "html":
        fields = {"external_id": "a@href", "title": "a", "url": "a@href"}
    recipe = parse_recipe({"kind": kind, "list": {"url": "https://careers.acme.com/jobs"},
                           "items": "$.jobs[*]" if kind == "json_api" else "li", "fields": fields})
    serve(web, **response)
    async with httpx.AsyncClient() as c:
        beats = await ticks_during(run_recipe(c, "careers.acme.com", "Acme", recipe))
    assert beats >= SLOW / TICK / 2
