from fastapi import FastAPI

from jobfinder.api import (
    ai_admin,
    cover_letters,
    documents,
    interview,
    links,
    matches,
    onboarding,
    profile,
    questions,
    settings,
    tasks,
)
from jobfinder.auth import routes as auth_routes

app = FastAPI(title="JobFinder")
app.include_router(auth_routes.router)
app.include_router(profile.router)
app.include_router(interview.router)
app.include_router(documents.router)
app.include_router(links.router)
app.include_router(onboarding.router)
app.include_router(tasks.router)
app.include_router(ai_admin.router)
app.include_router(matches.router)
app.include_router(cover_letters.router)
app.include_router(questions.router)
app.include_router(settings.router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
