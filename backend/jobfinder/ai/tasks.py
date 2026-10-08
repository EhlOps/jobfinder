"""Typed AI functions: one per feature, each with its own prompt file and output schema."""
import json
import re
from pathlib import Path

from jobfinder.ai.claude_code import AIError, ClaudeCode
from jobfinder.config import get_settings
from jobfinder.ai.schemas import (
    Background,
    Consolidated,
    FollowupQuestion,
    FollowupQuestions,
    MatchScore,
    Ping,
    ProfileAudit,
)

PROMPTS = Path(__file__).parent / "prompts"
MAX_SOURCE_CHARS = 30_000
MAX_JOB_CHARS = 12_000

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
    safe = text.replace(f"</{tag}>", f"< /{tag}>")
    return f"<{tag}{(' ' + attrs) if attrs else ''}>\n{safe}\n</{tag}>"


class AITasks:
    def __init__(self, claude: ClaudeCode):
        self.claude = claude

    async def extract_profile(self, sources: list[tuple[str, str]]) -> Background:
        """sources: (label, text) pairs, e.g. ("resume.pdf", "..."), ("github", "...")."""
        blocks, budget = [], MAX_SOURCE_CHARS
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
            model="sonnet",
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
            model="sonnet",
            task="followup_questions",
        )
        return result.questions[:10]

    async def audit_profile(
        self, status: dict, background: dict, facts: list[tuple[str, str]], demand: list[dict], skipped: list[str]
    ) -> ProfileAudit:
        """Screen the candidate like a recruiter: build the dossier, rate readiness, pick the next questions.
        demand: [{requirement, jobs, importance}] that scored jobs asked for and the profile left open."""
        known = {
            "job_search_status": {k: v for k, v in status.items() if v not in (None, "", [])},
            "background": background,
            "answers_given": [{"question": q, "answer": a} for q, a in facts],
            "requirements_target_jobs_ask_for_that_are_unconfirmed": demand,
            "questions_the_candidate_skipped": skipped,
        }
        return await self.claude.run(
            "Audit this candidate.\n\n" + data_block("data", json.dumps(known, indent=1)),
            system=load_prompt("profile_audit"),
            output=ProfileAudit,
            model="sonnet",
            task="audit_profile",
            timeout_s=240,
        )

    async def ping(self) -> bool:
        """Cheapest possible call, used to check that Claude is connected."""
        out = await self.claude.run(
            "Reply with ok=true.", system="Reply with JSON only.", output=Ping, model="haiku",
            timeout_s=60, task="ai_check",
        )
        return out.ok

    async def score_match(
        self, status: dict, background: dict, facts: list[tuple[str, str]], job: dict, dossier: dict | None = None,
        stage_info: dict | None = None,
    ) -> MatchScore:
        """job: title, company, location, workplace_type, salary, seniority, description."""
        candidate = {
            # "not sure" answers are null/empty: leave them out so they don't read as negatives
            "preferences": {k: v for k, v in status.items() if v not in (None, "", [])},
            "background": background,
            **({"recruiter_dossier": dossier} if dossier else {}),
            **({"career_stage": stage_info} if stage_info else {}),
            "answers_given": [{"question": q, "answer": a} for q, a in facts],
        }
        posting = {k: v for k, v in job.items() if k != "description" and v not in (None, "")}
        body = (
            "Assess this candidate for this job.\n\n"
            + data_block("candidate", json.dumps(candidate, indent=1))
            + "\n\n"
            + data_block("job", json.dumps(posting, indent=1) + "\n\nDescription:\n" + (job.get("description") or "")[:MAX_JOB_CHARS])
        )
        return await self.claude.run(
            body, system=load_prompt("match"), output=MatchScore, model=get_settings().match_model, task="score_match",
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
            data_block("job", json.dumps(posting, indent=1) + "\n\nDescription:\n" + (job.get("description") or "")[:MAX_JOB_CHARS]),
        ]
        if analysis:
            parts.append(data_block("analysis", json.dumps(analysis, indent=1)))
        if notes.strip():
            parts.append(data_block("notes", notes.strip()[:500]))
        prompt = "\n\n".join(parts)

        async def draft(extra: str = "") -> str:
            text = await self.claude.run(
                prompt + extra, system=load_prompt("cover_letter"), model="sonnet", task="cover_letter", timeout_s=180,
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
            output=Consolidated, model="sonnet", task="consolidate_questions", timeout_s=120,
        )


def clean_letter(text: str) -> str:
    """Strip code fences, wrapping quotes and stray preamble the model sometimes adds."""
    t = text.strip()
    t = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", t).strip()
    t = re.sub(r"^(?:here(?:'s| is)[^\n]*:|subject:[^\n]*)\n+", "", t, flags=re.IGNORECASE).strip()
    if len(t) > 2 and t[0] in "\"“" and t[-1] in "\"”":
        t = t[1:-1].strip()
    return t
