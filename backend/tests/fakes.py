"""Test doubles shared across the suite: one home instead of a copy per test file.

- `FakeClaude` stands in for the `ClaudeCode` client that `AITasks` wraps, so the real prompt
  building and output checks run.
- The `Fake*AI` classes stand in for `AITasks` itself, one per capability, for tests of the
  services and task handlers that call it. They are kept separate on purpose: a fake that only
  has the methods a test expects fails loudly when code under test calls something else.
- `LocalGate` is the Claude gate in memory (one process's semaphore); conftest.py gives every test
  one, so only tests/test_claude_gate.py talks to valkey.
- `FakeClock`, `fake_resolve_public` and `FakeRenderer` replace the clock, DNS and the renderer
  container.
- `run_task` delivers one ledger row in-process, the way its queue's worker would.
- `MockWeb` and its helpers (`serve`, `redirects`, `refuse`, `handle`) replace the
  network, through respx. The `web` fixture in conftest.py hands a test one that is already active.
"""
import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from itertools import pairwise

import httpx
import respx
from respx.models import AllMockedAssertionError
from respx.patterns import M
from sqlalchemy import select

from jobfinder.core.clock import utcnow
from jobfinder.integrations.claude.schemas import (
    AuditQuestion,
    Background,
    Consolidated,
    ConsolidatedQuestion,
    Dossier,
    Experience,
    FollowupQuestion,
    MatchScore,
    ProfileAudit,
    Unknown,
)
from jobfinder.integrations.http_safety import FetchError
from jobfinder.integrations.render import KINDS as RENDER_KINDS
from jobfinder.models import Task
from jobfinder.tasks import ledger

_UNSET = object()


class FakeClaude:
    """Returns (or raises) each reply in turn and records every call.

    Once the queue is empty it keeps returning `default`; with no default the next call fails
    with IndexError, so a test that expects no AI call notices one.
    """

    def __init__(self, *replies, default=_UNSET):
        self.replies, self.default, self.calls = list(replies), default, []

    async def run(self, prompt, *, system, model="sonnet", timeout_s=180, task="", output=None):
        self.calls.append(
            {"prompt": prompt, "system": system, "model": model, "task": task, "output": output}
        )
        if self.replies or self.default is _UNSET:
            reply = self.replies.pop(0)
        else:
            reply = self.default
        if isinstance(reply, Exception):
            raise reply
        return reply

    @property
    def prompts(self) -> list[str]:
        return [c["prompt"] for c in self.calls]

    @property
    def systems(self) -> list[str]:
        return [c["system"] for c in self.calls]

    @property
    def outputs(self) -> list:
        return [c["output"] for c in self.calls]


class FakePingAI:
    """ping: succeeds, or raises the given error."""

    def __init__(self, error: Exception | None = None):
        self.error = error

    async def ping(self):
        if self.error:
            raise self.error
        return True


class FakeLetterAI:
    """cover_letter: returns canned text and records the keyword arguments."""

    def __init__(self, text: str):
        self.text, self.calls = text, []

    async def cover_letter(self, **kw):
        self.calls.append(kw)
        return self.text


class FakeTailorAI:
    """tailor_resume: returns a canned resume (or raises) and records the keyword arguments."""

    def __init__(self, resume=None, error: Exception | None = None):
        self.resume, self.error, self.calls = resume, error, []

    async def tailor_resume(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return self.resume


class FakeScoringAI:
    """score_match returns a canned score by job title; titles can be told to fail."""

    def __init__(self, scores=None, errors=None):
        self.scores, self.errors, self.calls = scores or {}, errors or {}, []

    async def score_match(self, status, background, facts, job, dossier=None, stage_info=None,
                          sponsorship=None):
        self.calls.append(job["title"])
        if (err := self.errors.get(job["title"])) is not None:
            raise err
        return MatchScore(
            score=self.scores.get(job["title"], 80), confidence=0.9, reasons=["fits"], gaps=["k8s"],
            unknowns=[Unknown(question="Used Kafka?", why="job needs it")],
        )


class FakeQuestionsAI:
    """consolidate_questions: by default every raw question becomes its own question."""

    def __init__(self, result: Consolidated | None = None, error: Exception | None = None):
        self.result, self.error, self.calls = result, error, []

    async def consolidate_questions(self, facts, asked, raw, max_questions=6):
        self.calls.append({"facts": facts, "asked": asked, "raw": raw, "max": max_questions})
        if self.error:
            raise self.error
        return self.result or Consolidated(
            questions=[ConsolidatedQuestion(question=q, why=w, sources=[i])
                       for i, (q, w) in enumerate(raw)]
        )


class FakeOnboardingAI:
    """extract_profile and followup_questions: canned answers; remembers what they were given."""

    def __init__(self):
        self.extract_sources = None
        self.fail_with: Exception | None = None
        self.seen = None

    async def extract_profile(self, sources):
        if self.fail_with:
            raise self.fail_with
        self.extract_sources = sources
        return Background(summary="SWE",
                          experience=[Experience(company="Acme", title="Intern", kind="internship")])

    async def followup_questions(self, status, background, facts):
        self.seen = (status, background, facts)
        return [FollowupQuestion(question="Have you used Kubernetes?", why="Many roles ask for it")]


class FakeAuditAI:
    """audit_profile: a canned audit; `questions` overrides the questions it asks."""

    def __init__(self, questions=None, during=None):
        self.seen = None
        self.questions = questions
        self.during = during  # awaited inside the "AI call", to simulate the user acting meanwhile

    async def audit_profile(self, status, background, facts, demand, skipped, queued=None):
        self.seen = {"facts": facts, "demand": demand, "skipped": skipped, "queued": queued}
        if self.during:
            await self.during()
        if self.questions is not None:
            return ProfileAudit(
                dossier=Dossier(headline="Backend engineer", hire_view="x"), readiness=62,
                dimensions={"skills": 70},
                questions=[AuditQuestion(question=q, why="w", dimension="skills")
                           for q in self.questions],
            )
        return ProfileAudit(
            dossier=Dossier(headline="Backend engineer",
                            hire_view="I could hire this person for backend roles."),
            readiness=62, dimensions={"skills": 70, "impact": 40},
            questions=[AuditQuestion(question="What did you own at Acme?", why="scope",
                                     dimension="scope"),
                       AuditQuestion(question="Notice period?", why="logistics",
                                     dimension="logistics")],
        )


class FakeClock:
    """A settable `now()`: monkeypatch it in for a module's clock, then move `.now` by hand."""

    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


PUBLIC_IP = "93.184.216.34"
_PRIVATE_PREFIXES = ("127.", "10.", "192.168.", "169.254.", "::1")


def fake_resolve_public(*, refuse_internal: bool = False, refuse_private: bool = False):
    """A stand-in for `http_safety.resolve_public`: no real DNS in unit tests.

    Every host resolves to PUBLIC_IP. `refuse_internal` refuses hosts containing 'internal', and
    `refuse_private` refuses localhost and a few common private-range prefixes. That is a rough
    stand-in for the tests that used it, not the real check: SSRF tests should use the real one.
    """

    async def resolve(url):
        host = httpx.URL(url).host
        private = host in ("localhost", "0.0.0.0") or host.startswith(_PRIVATE_PREFIXES)
        if (refuse_internal and "internal" in host) or (refuse_private and private):
            raise FetchError("URL points to a non-public address")
        return host, PUBLIC_IP

    return resolve


class FakeRenderer:
    """Plays the renderer container: completes render_* tasks with canned results and records them.

    `fail`, when set, fails every task with that error (no retry) instead of completing it.
    """

    def __init__(self, maker, results, *, fail: str | None = None):
        self.maker, self.results, self.fail, self.seen = maker, results, fail, []

    async def loop(self):
        while True:
            async with self.maker() as db:
                due = await db.scalar(
                    select(Task.id)
                    .where(Task.kind.in_(RENDER_KINDS), Task.status == "queued", Task.run_after <= utcnow())
                    .order_by(Task.id).limit(1)
                )
                t = await ledger.claim(db, due) if due else None
                if t:
                    self.seen.append((t.kind, t.payload))
                    if self.fail:
                        await ledger.fail(db, t, self.fail, retry=False)
                    else:
                        await ledger.finish(db, t, self.results[t.kind])
                await db.commit()
                if t:
                    continue
            await asyncio.sleep(0.01)

    async def __aenter__(self):
        self.task = asyncio.create_task(self.loop())
        return self

    async def __aexit__(self, *exc):
        # Wait for the cancel to land, so the loop can't still hold a connection mid-claim when the test
        # ends and the engine is disposed.
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task


async def run_task(task_id: int, ai=None, session_factory=None) -> None:
    """Deliver ledger row `task_id` to its kind's handler through tasks.runtime.execute, as worker-ai or
    worker-io would: claim, run, finish or fail, and commit. `ai` is handed to the handler as is."""
    import jobfinder.tasks.ai  # registers the handlers
    import jobfinder.tasks.io  # noqa: F401
    from jobfinder.core.context import default_context
    from jobfinder.tasks import runtime

    sessions = session_factory or default_context().sessions
    async with sessions() as db:
        kind = await db.scalar(select(Task.kind).where(Task.id == task_id))
    assert kind is not None, f"no task {task_id}"
    await runtime.execute(kind, runtime.REGISTRY[kind], task_id, payload_model=runtime.PAYLOADS[kind],
                          wants_ai=ai is not None, session_factory=sessions, ai=ai)


class MockWeb(respx.MockRouter):
    """A respx router standing in for the network while it is active (`with MockWeb() as web:`).

    It patches httpx below the client, so plain `httpx.AsyncClient()`s, and those the code under
    test builds itself, all go through it. A route that is never hit is fine. A request no route
    matches raises respx's AllMockedAssertionError and lands in `stray`: code under test may catch
    that error as a failed fetch, so the `web` fixture fails the test on any stray request after
    it. Leaving the block forgets the routes and the calls.
    """

    def __init__(self):
        super().__init__(assert_all_called=False)
        self.stray: list[httpx.Request] = []

    def resolve(self, request):
        try:
            return super().resolve(request)
        except AllMockedAssertionError:
            self.stray.append(request)
            raise

    async def aresolve(self, request):
        try:
            return await super().aresolve(request)
        except AllMockedAssertionError:
            self.stray.append(request)
            raise


def _named(url: str):
    """A respx pattern for `url`, matched on the Host header and the path (and the exact query, if
    `url` has one).

    Matching on the Host header rather than the URL's host is what lets one route serve both a plain
    fetch and an IP-pinned one: safe_fetch and the renderer connect to the validated IP and send the
    name in the Host header (and SNI). The scheme is not checked.
    """
    u = httpx.URL(url)
    pattern = M(headers={"host": u.netloc.decode("ascii")}, path=u.path)
    return pattern & M(params__eq=u.query.decode()) if u.query else pattern


def handle(web: respx.MockRouter, handler) -> respx.Route:
    """Send every request to `handler`, a function of the request returning a Response (or
    raising), as with `httpx.MockTransport`.

    Routes for a specific URL added before this one still win. respx replaces a route with an
    identical pattern, so a later catch-all (this, or serve/refuse with no url) replaces this one.
    """
    return web.route().mock(side_effect=handler)


def serve(web: respx.MockRouter, url: str | None = None, status: int = 200, **response) -> respx.Route:
    """Answer requests for `url` (every request, when None) with a fresh Response each time.

    `response` takes httpx.Response's keywords: json=, html=, text=, content=, headers=.
    """
    route = web.route(_named(url)) if url else web.route()
    return route.respond(status, **response)


def redirects(web: respx.MockRouter, *urls: str, status: int = 302) -> list[respx.Route]:
    """Chain redirects: each of `urls` redirects to the next. Serve the last one separately."""
    return [serve(web, a, status, headers={"location": b}) for a, b in pairwise(urls)]


def refuse(web: respx.MockRouter, url: str | None = None) -> respx.Route:
    """Refuse connections to `url` (every request, when None) with httpx.ConnectError."""
    route = web.route(_named(url)) if url else web.route()
    return route.mock(side_effect=httpx.ConnectError)  # a fresh error, tied to its request



class LocalGate:
    """The Claude gate (integrations/claude/gate.py) without valkey: `limit` permits in this process."""

    def __init__(self, limit: int = 2):
        self._sem = asyncio.Semaphore(limit)
        self.leases: list[float] = []

    @asynccontextmanager
    async def slot(self, lease_s: float):
        self.leases.append(lease_s)
        async with self._sem:
            yield
