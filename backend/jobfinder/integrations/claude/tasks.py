"""Typed AI functions: one per feature, each with its own prompt file and output schema."""
import html as htmllib
import json
import re
from pathlib import Path

from jobfinder.core.config import settings
from jobfinder.integrations.claude.claude_code import AIError, ClaudeCode
from jobfinder.integrations.claude.schemas import (
    Background,
    Consolidated,
    FollowupQuestion,
    FollowupQuestions,
    InducedRecipe,
    MatchScore,
    Ping,
    ProfileAudit,
    TailoredResume,
)

PROMPTS = Path(__file__).parent / "prompts"
MAX_PAGE_CHARS = 40_000  # recipe induction: the careers page after stripping scripts and styles
MAX_SAMPLE_CHARS = 8_000
MAX_SAMPLES = 3
MAX_INLINE_JSON_CHARS = 2_000  # inline scripts kept when they look like data (window.__STATE__ = {...})

TONES = {
    "professional": "Polished, confident and formal-but-human. About 300 words in 3 or 4 paragraphs.",
    "warm": "Personable and conversational while still professional; sounds like a real person. About 300 words.",
    "concise": "Tight and direct. About 180 words in 3 short paragraphs; no filler.",
    "enthusiastic": "Energetic and openly excited about the mission, without exaggeration. About 300 words.",
}
_PLACEHOLDER = re.compile(r"\[[^\]\n]{2,60}\]|\{\{.*?\}\}")


def load_prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text()


def data_block(tag: str, text: str, attrs: str = "") -> str:
    """Wrap untrusted text so it can't close its own block and is clearly labelled as data."""
    safe = re.sub(rf"<\s*/\s*{re.escape(tag)}\b", f"< /{tag}", text, flags=re.IGNORECASE)
    return f"<{tag}{(' ' + attrs) if attrs else ''}>\n{safe}\n</{tag}>"


class AITasks:
    def __init__(self, claude: ClaudeCode):
        self.claude = claude

    async def extract_profile(self, sources: list[tuple[str, str]]) -> Background:
        """sources: (label, text) pairs, e.g. ("resume.pdf", "..."), ("github", "...")."""
        blocks, budget = [], settings.ai_max_source_chars
        for label, text in sources:
            if budget <= 0:
                break
            chunk = text[:budget]
            budget -= len(chunk)
            blocks.append(data_block("source", chunk, f'name="{label}"'))
        return await self.claude.run(
            "Extract the profile from these sources.\n\n" + "\n\n".join(blocks),
            system=load_prompt("profile_extract"),
            output=Background,
            model=settings.profile_model,
            task="extract_profile",
            timeout_s=240,
        )

    async def followup_questions(
        self, status: dict, background: dict, facts: list[tuple[str, str]]
    ) -> list[FollowupQuestion]:
        known = {
            "job_search_status": status,
            "background": background,
            "previous_answers": [{"question": q, "answer": a} for q, a in facts],
        }
        result = await self.claude.run(
            "Here is what we know so far.\n\n" + data_block("data", json.dumps(known, indent=1)),
            system=load_prompt("followups"),
            output=FollowupQuestions,
            model=settings.followup_model,
            task="followup_questions",
        )
        return result.questions[:10]

    async def audit_profile(
        self, status: dict, background: dict, facts: list[tuple[str, str]], demand: list[dict], skipped: list[str],
        queued: list[str] | None = None,
    ) -> ProfileAudit:
        """Screen the candidate like a recruiter: build the dossier, rate readiness, pick the next questions.
        demand: [{requirement, jobs, importance}] that scored jobs asked for and the profile left open."""
        known = {
            "job_search_status": {k: v for k, v in status.items() if v not in (None, "", [])},
            "background": background,
            "answers_given": [{"question": q, "answer": a} for q, a in facts],
            "requirements_target_jobs_ask_for_that_are_unconfirmed": demand,
            "questions_the_candidate_skipped": skipped,
            "questions_already_queued": queued or [],
        }
        return await self.claude.run(
            "Audit this candidate.\n\n" + data_block("data", json.dumps(known, indent=1)),
            system=load_prompt("profile_audit"),
            output=ProfileAudit,
            model=settings.audit_model,
            task="audit_profile",
            timeout_s=240,
        )

    async def ping(self) -> bool:
        """Cheapest possible call, used to check that Claude is connected."""
        out = await self.claude.run(
            "Reply with ok=true.", system="Reply with JSON only.", output=Ping, model=settings.ai_check_model,
            timeout_s=60, task="ai_check",
        )
        return out.ok

    async def score_match(
        self, status: dict, background: dict, facts: list[tuple[str, str]], job: dict, dossier: dict | None = None,
        stage_info: dict | None = None, sponsorship: dict | None = None,
    ) -> MatchScore:
        """job: title, company, location, workplace_type, salary, seniority, description."""
        candidate = {
            # "not sure" answers are null/empty: leave them out so they don't read as negatives
            "preferences": {k: v for k, v in status.items() if v not in (None, "", [])},
            "background": background,
            **({"recruiter_dossier": dossier} if dossier else {}),
            **({"career_stage": stage_info} if stage_info else {}),
            **({"sponsorship": sponsorship} if sponsorship else {}),
            "answers_given": [{"question": q, "answer": a} for q, a in facts],
        }
        posting = {k: v for k, v in job.items() if k != "description" and v not in (None, "")}
        body = (
            "Assess this candidate for this job.\n\n"
            + data_block("candidate", json.dumps(candidate, indent=1))
            + "\n\n"
            + data_block("job", json.dumps(posting, indent=1) + "\n\nDescription:\n" + (job.get("description") or "")[:settings.ai_max_job_chars])
        )
        return await self.claude.run(
            body, system=load_prompt("match"), output=MatchScore, model=settings.match_model, task="score_match",
            timeout_s=180,
        )

    async def cover_letter(
        self, *, name: str, background: dict, facts: list[tuple[str, str]], job: dict,
        analysis: dict | None, tone: str, notes: str = "",
    ) -> str:
        """Draft a cover letter. job: title, company, location, description (+ optional hints)."""
        candidate = {
            "name": name or None,
            "background": background,
            "answers_given": [{"question": q, "answer": a} for q, a in facts],
        }
        posting = {k: v for k, v in job.items() if k != "description" and v not in (None, "")}
        parts = [
            "Write the cover letter.\n",
            f"Tone and length: {TONES[tone]}\n",
            data_block("candidate", json.dumps(candidate, indent=1)),
            data_block("job", json.dumps(posting, indent=1) + "\n\nDescription:\n" + (job.get("description") or "")[:settings.ai_max_job_chars]),
        ]
        if analysis:
            parts.append(data_block("analysis", json.dumps(analysis, indent=1)))
        if notes.strip():
            parts.append(data_block("notes", notes.strip()[:500]))
        prompt = "\n\n".join(parts)

        async def draft(extra: str = "") -> str:
            text = await self.claude.run(
                prompt + extra, system=load_prompt("cover_letter"), model=settings.letter_model, task="cover_letter", timeout_s=180,
            )
            return clean_letter(text)

        letter = await draft()
        if _PLACEHOLDER.search(letter) or len(letter) < 200:
            # One retry that names the problem; a letter with [placeholders] is unusable as-is.
            letter = await draft(
                "\n\nYour previous draft contained bracketed placeholders or was too short. Rewrite it complete, "
                "with real names only and no brackets."
            )
        if _PLACEHOLDER.search(letter) or len(letter) < 200:
            raise AIError("bad_output", "The letter came back incomplete; try again.")
        return letter

    async def consolidate_questions(
        self, facts: list[tuple[str, str]], asked: list[tuple[str, str]], raw: list[tuple[str, str]], max_questions: int = 6
    ) -> Consolidated:
        """raw: (question, why) pairs. Indexes in the result refer to positions in `raw`."""
        body = "\n\n".join([
            data_block("known_answers", json.dumps([{"question": q, "answer": a} for q, a in facts], indent=1)),
            data_block("already_asked", json.dumps([{"question": q, "status": s} for q, s in asked], indent=1)),
            data_block("raw_questions", json.dumps([{"index": i, "question": q, "why": w} for i, (q, w) in enumerate(raw)], indent=1)),
        ])
        return await self.claude.run(
            body, system=load_prompt("consolidate_questions").replace("{max_questions}", str(max_questions)),
            output=Consolidated, model=settings.questions_model, task="consolidate_questions", timeout_s=120,
        )

    async def tailor_resume(
        self, *, name: str, background: dict, facts: list[tuple[str, str]], documents: list[tuple[str, str]], job: dict,
    ) -> TailoredResume:
        """Reorder and reword the candidate's real content for a posting. documents: (label, text) pairs."""
        candidate = {
            "name": name or None,
            "background": background,
            "answers_given": [{"question": q, "answer": a} for q, a in facts],
        }
        posting = {k: v for k, v in job.items() if k != "description" and v not in (None, "")}
        parts = ["Tailor the resume to this job.\n", data_block("candidate", json.dumps(candidate, indent=1))]
        budget = settings.ai_max_source_chars
        for label, text in documents:
            if budget <= 0:
                break
            chunk = text[:budget]
            budget -= len(chunk)
            parts.append(data_block("document", chunk, f'name="{label}"'))
        parts.append(
            data_block("job", json.dumps(posting, indent=1) + "\n\nDescription:\n" + (job.get("description") or "")[:settings.ai_max_job_chars])
        )
        return await self.claude.run(
            "\n\n".join(parts), system=load_prompt("resume_tailor"), output=TailoredResume, model=settings.resume_model,
            task="tailor_resume", timeout_s=180,
        )

    async def induce_recipe(
        self, page_url: str, html_excerpt: str, json_samples: list[tuple[str, str]], pasted_url: str,
        previous: dict | None = None, failure: str | None = None,
    ) -> InducedRecipe:
        """Propose a recipe for a careers site no deterministic rung could read. Everything from the site
        (page, samples, URLs, the old recipe and its failure) goes in as data; the caller validates the result.
        json_samples: (url, response text) pairs from endpoints the page refers to."""
        parts = [
            "Propose a recipe that lists every job on this careers site, including the pasted job.",
            data_block("data", pasted_url[:2048], 'kind="pasted_job_url"'),
            data_block("data", page_excerpt(html_excerpt), f'kind="page" url="{_attr(page_url)}"'),
        ]
        for url, text in json_samples[:MAX_SAMPLES]:
            parts.append(data_block("data", text[:MAX_SAMPLE_CHARS], f'kind="json_sample" url="{_attr(url)}"'))
        if previous is not None:
            prev = {k: v for k, v in previous.items() if k != "provenance"}
            parts.append(data_block("data", json.dumps(prev, indent=1)[:MAX_SAMPLE_CHARS], 'kind="previous_recipe"'))
        if failure:
            parts.append("The previous recipe failed; fix the problem described here.")
            parts.append(data_block("data", failure[:1000], 'kind="failure"'))
        return await self.claude.run(
            "\n\n".join(parts), system=load_prompt("recipe_induce"), output=InducedRecipe,
            model=settings.recipe_model, task="recipe_induce", timeout_s=240,
        )


# (?=[\s/>]) and not \b: <svg-icon> or <template-card> are custom elements, not blocks to drop
_BLOCK_OPEN = re.compile(r"<(script|style|svg|noscript|template)(?=[\s/>])([^>]{0,500})>|<!--", re.IGNORECASE)
_LD_TYPE = re.compile(r"type\s*=\s*[\"']?application/ld\+json", re.IGNORECASE)
_JSONISH = re.compile(r"^\s*(?:[\w.$\[\]\"']{1,80}\s*=\s*)?[\[{]")
_DROP_ATTRS = re.compile(r"\s(?:style|on[a-z]+|srcset|sizes|d)\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)",
                         re.IGNORECASE)
_DATA_URI = re.compile(r"data:[a-z]+/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=]{100,}", re.IGNORECASE)


def page_excerpt(page_html: str, limit: int = MAX_PAGE_CHARS) -> str:
    """The page as the model needs it to write selectors: markup kept, but scripts dropped (except JSON-LD
    and short inline JSON, which often hold the jobs), styles, SVG, comments and noisy attributes removed,
    whitespace collapsed, at most `limit` chars. Linear in the page size: an unclosed block ends the scan."""
    text = page_html[: limit * 40]
    out, pos = [], 0
    while (m := _BLOCK_OPEN.search(text, pos)) is not None:
        out.append(text[pos:m.start()])
        tag = (m.group(1) or "").lower()
        close = re.compile(rf"</{tag}\s*>", re.IGNORECASE).search(text, m.end()) if tag else None
        if not tag:  # comment
            end = text.find("-->", m.end())
            pos = end + 3 if end >= 0 else len(text)
            continue
        body = text[m.end():close.start()] if close else ""
        if tag == "script" and _LD_TYPE.search(m.group(2)):
            out.append(f'<script type="application/ld+json">{body[:MAX_SAMPLE_CHARS]}</script>')
        elif tag == "script" and body and len(body) <= MAX_INLINE_JSON_CHARS and _JSONISH.match(body):
            out.append(f"<script>{body}</script>")
        pos = close.end() if close else len(text)
    out.append(text[pos:])
    text = _DATA_URI.sub("data:...", _DROP_ATTRS.sub("", "".join(out)))
    text = re.sub(r">\s+<", "><", re.sub(r"\s+", " ", text))
    return text.strip()[:limit]


def _attr(value: str) -> str:
    return htmllib.escape(value[:500], quote=True)


def clean_letter(text: str) -> str:
    """Strip code fences, wrapping quotes and stray preamble the model sometimes adds."""
    t = text.strip()
    t = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", t).strip()
    t = re.sub(r"^(?:here(?:'s| is)[^\n]*:|subject:[^\n]*)\n+", "", t, flags=re.IGNORECASE).strip()
    if len(t) > 2 and t[0] in "\"“" and t[-1] in "\"”":
        t = t[1:-1].strip()
    return t
