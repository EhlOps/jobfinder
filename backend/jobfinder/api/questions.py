"""Clarifying questions: answered in the app (logged in) or from the link in the email (signed token)."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from jobfinder.api.deps import CurrentUser, DbSession
from jobfinder.models import User
from jobfinder.notify.tokens import InvalidToken, read_token
from jobfinder.questions import service
from jobfinder.schemas.questions import AnswerResult, QuestionAnswersIn, QuestionList

router = APIRouter(tags=["questions"])


def _user_from_token(purpose: str, token: str) -> int:
    try:
        return read_token(purpose, token)
    except InvalidToken as e:
        if e.expired:
            raise HTTPException(410, "This link has expired. Log in and answer from the Questions page instead.") from None
        raise HTTPException(400, "This link isn't valid.") from None


# ── logged in ────────────────────────────────────────────────────────────
@router.get("/api/questions", response_model=QuestionList)
async def my_questions(user: CurrentUser, db: DbSession):
    return QuestionList(questions=await service.list_open(db, user.id))


@router.post("/api/questions/answers", response_model=AnswerResult)
async def answer_mine(body: QuestionAnswersIn, user: CurrentUser, db: DbSession):
    return AnswerResult(**await service.answer_questions(db, user.id, [a.model_dump() for a in body.answers]))


# ── from the email link (no login; the signed token is the authorisation) ─
@router.get("/api/q/{token}", response_model=QuestionList)
async def questions_by_token(token: str, db: DbSession):
    return QuestionList(questions=await service.list_open(db, _user_from_token("questions", token)))


@router.post("/api/q/{token}/answers", response_model=AnswerResult)
async def answer_by_token(token: str, body: QuestionAnswersIn, db: DbSession):
    uid = _user_from_token("questions", token)
    return AnswerResult(**await service.answer_questions(db, uid, [a.model_dump() for a in body.answers]))


# ── one-click unsubscribe (also used by the List-Unsubscribe header) ──────
_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>JobFinder</title><body style="font-family:system-ui,sans-serif;max-width:480px;margin:15vh auto;padding:0 16px;line-height:1.5">
<h2>{title}</h2><p>{body}</p></body>"""


def _bad_link() -> HTMLResponse:
    return HTMLResponse(_PAGE.format(title="Link not valid", body="This unsubscribe link isn't valid or has expired."), status_code=400)


@router.get("/api/unsubscribe/{token}", response_class=HTMLResponse)
async def unsubscribe_confirm(token: str):
    """Only asks. Email scanners and link previewers fetch GET URLs, so GET must never change anything."""
    try:
        read_token("unsubscribe", token)
    except InvalidToken:
        return _bad_link()
    return HTMLResponse(_PAGE.format(
        title="Stop the daily email?",
        body=f'''You'll no longer get the daily job email or question emails.</p>
<form method="post" action="/api/unsubscribe/{token}"><button type="submit" style="font:inherit;padding:8px 16px">Unsubscribe</button></form><p>''',
    ))


@router.post("/api/unsubscribe/{token}", response_class=HTMLResponse)
async def unsubscribe(token: str, db: DbSession):
    """The confirmation button and the List-Unsubscribe one-click header (RFC 8058) both POST here."""
    try:
        uid = read_token("unsubscribe", token)
    except InvalidToken:
        return _bad_link()
    user = await db.get(User, uid)
    if user:
        user.digest_enabled = False
        user.question_emails_enabled = False
        await db.commit()
    return HTMLResponse(_PAGE.format(
        title="You're unsubscribed",
        body="We won't send the daily email any more. You can turn it back on any time in Settings inside the app.",
    ))
