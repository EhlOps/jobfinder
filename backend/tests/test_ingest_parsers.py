import json
from pathlib import Path

import pytest

from jobfinder.ingest.base import (
    dedupe_hash,
    html_to_text,
    infer_seniority,
    infer_workplace,
    parse_salary_range,
)
from jobfinder.ingest.sources.ashby import parse_ashby
from jobfinder.ingest.sources.greenhouse import parse_greenhouse
from jobfinder.ingest.sources.lever import parse_lever

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


# ── real-payload fixtures ────────────────────────────────────────────────
def test_greenhouse_fixture():
    jobs = parse_greenhouse(load("greenhouse_discord.json"), "Discord")
    assert len(jobs) == 2 and all(j.source == "greenhouse" and j.company_name == "Discord" for j in jobs)
    j = jobs[0]
    assert j.external_id == "8806482002" and j.title == "Commercial Policy Lead"
    assert j.url.startswith("https://job-boards.greenhouse.io/discord/jobs/") and j.location == "San Francisco Bay Area"
    assert j.posted_at and j.posted_at.year == 2026
    # content arrives HTML-escaped; it must come out as clean text
    assert "&lt;" not in j.description_text and "<div" not in j.description_text and len(j.description_text) > 500


def test_lever_fixture():
    jobs = parse_lever(load("lever_spotify.json"), "Spotify")
    assert len(jobs) == 3
    android = next(j for j in jobs if j.title.startswith("Android Engineer"))
    assert android.location == "London; Stockholm" and android.workplace_type == "hybrid"
    assert android.employment_type == "Permanent" and android.posted_at.year == 2026
    assert next(j for j in jobs if "Manager" in j.title).seniority == "manager"
    assert all(j.url.startswith("https://jobs.lever.co/spotify/") for j in jobs)


def test_ashby_fixture_salary_locations_and_workplace():
    jobs = parse_ashby(load("ashby_ramp.json"), "Ramp")
    sec = next(j for j in jobs if j.title == "Security Engineer, Cloud")
    assert (sec.salary_min, sec.salary_max, sec.salary_currency) == (211400, 290600, "USD")
    assert sec.workplace_type == "hybrid" and "New York, NY (HQ)" in sec.location and "Remote (Canada)" in sec.location
    assert sec.url.startswith("https://jobs.ashbyhq.com/ramp/")


def test_ashby_skips_unlisted_and_untitled():
    payload = {"jobs": [{"id": "1", "title": "Hidden", "isListed": False}, {"id": "2", "title": " "}, {"id": "3", "title": "Real"}]}
    assert [j.title for j in parse_ashby(payload, "X")] == ["Real"]


def test_greenhouse_pay_ranges_and_description_salary():
    payload = {"jobs": [
        {"id": 1, "title": "A", "absolute_url": "u", "location": {"name": "NYC"}, "content": "",
         "pay_input_ranges": [{"min_cents": 15000000, "max_cents": 20000000, "currency_type": "USD"}]},
        {"id": 2, "title": "B", "absolute_url": "u", "location": {"name": "Remote"},
         "content": "&lt;p&gt;Pay range: $130,000 - $170,000 per year&lt;/p&gt;"},
    ]}
    a, b = parse_greenhouse(payload, "X")
    assert (a.salary_min, a.salary_max) == (150000, 200000)
    assert (b.salary_min, b.salary_max) == (130000, 170000) and b.workplace_type == "remote"


def test_lever_salary_range_field():
    payload = [{"id": "x", "text": "Engineer", "hostedUrl": "u", "categories": {"location": "SF"},
                "salaryRange": {"min": 120000, "max": 160000, "currency": "USD", "interval": "per-year-salary"}}]
    j = parse_lever(payload, "X")[0]
    assert (j.salary_min, j.salary_max, j.salary_currency) == (120000, 160000, "USD")


def test_parsers_tolerate_empty_payloads():
    assert parse_greenhouse({}, "X") == [] and parse_lever([], "X") == [] and parse_ashby({"jobs": []}, "X") == []


# ── helpers ──────────────────────────────────────────────────────────────
def test_html_to_text_structure_and_cleanup():
    html = "<div><h2>About</h2><p>Hello&nbsp;<b>world</b> &amp; more</p><ul><li>One</li><li>Two</li></ul><script>evil()</script></div>"
    assert html_to_text(html) == "About\n\nHello world & more\n\n- One\n\n- Two" or html_to_text(html).splitlines()[0] == "About"
    out = html_to_text(html)
    assert "evil" not in out and "- One" in out and "- Two" in out and "Hello world & more" in out


@pytest.mark.parametrize(("text", "expected"), [
    ("Pay: $120,000 - $150,000", (120000, 150000, "USD")),
    ("range $120K–$150K a year", (120000, 150000, "USD")),
    ("$95,000 to $125,000", (95000, 125000, "USD")),
    ("$120 - $150K", (120000, 150000, "USD")),
    ("$50 - $80 per hour", None),                 # hourly: outside the annual band
    ("$150,000 - $120,000", None),                # reversed
    ("equity worth $5,000,000 - $9,000,000", None),
    ("no pay info here", None),
])
def test_parse_salary_range(text, expected):
    assert parse_salary_range(text) == expected


@pytest.mark.parametrize(("title", "level"), [
    ("Software Engineering Intern", "intern"), ("Software Engineer, New Grad", "new_grad"),
    ("University Graduate - Backend", "new_grad"), ("Senior Software Engineer", "senior"),
    ("Sr. Data Engineer", "senior"), ("Staff Engineer", "staff"), ("Principal Engineer", "staff"),
    ("Engineering Manager", "manager"), ("Director of Engineering", "manager"),
    ("Junior Developer", "junior"), ("Software Engineer", None), ("Product Designer", None),
    ("Software Engineer I", "junior"), ("Software Engineer II, CRM", "mid"), ("Backend Engineer III", "senior"),
    ("Software Engineer IV", "staff"), ("Engineering Manager II", "manager"),
])
def test_infer_seniority(title, level):
    assert infer_seniority(title) == level


def test_infer_workplace():
    assert infer_workplace("Remote - US") == "remote"
    assert infer_workplace("NYC (Hybrid)") == "hybrid"
    assert infer_workplace("Boston, MA") is None
    assert infer_workplace("Boston", declared="OnSite") == "onsite"
    assert infer_workplace("Boston", declared="Hybrid") == "hybrid"
    assert infer_workplace("Anywhere", declared="remote") == "remote"


def test_dedupe_hash_ignores_case_punctuation_and_spacing():
    a = dedupe_hash("Stripe", "Software Engineer, Backend", "New York, NY")
    assert a == dedupe_hash("stripe", "software engineer - backend", "new  york ny")
    assert a != dedupe_hash("Stripe", "Software Engineer, Frontend", "New York, NY")


def test_bare_graduate_in_title_is_not_new_grad():
    assert infer_seniority("Graduate-level ML Researcher") is None
    assert infer_seniority("Graduate Engineer") == "new_grad"
    assert infer_seniority("Recent Graduate Analyst") == "new_grad"
