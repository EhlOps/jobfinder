# backend/jobfinder/api/cover_letters.py:96-110 (today)
# Inline queries, HTTP errors built in the router, and enqueue() commits the request's session.

@router.post("", response_model=TaskRef, status_code=202)
async def generate(match_id: int, body: GenerateIn, user: CurrentUser, db: DB):
    """Draft (or redraft) the letter in the worker. Poll GET /api/tasks/{id}, then GET this resource."""
    _, job = await _match_and_job(db, user, match_id)
    profile = await db.get(Profile, user.id)
    if not profile or not profile.background:
        raise HTTPException(422, "Finish your profile first (onboarding)")
    existing = await _letter(db, user, job.id)
    if existing and existing.edited and not body.force:
        raise HTTPException(409, "You've edited this letter; redrafting would replace your changes.")
    task = await queue.enqueue(
        db, "cover_letter", {"match_id": match_id, "tone": body.tone, "notes": body.notes.strip()}, user_id=user.id, dedupe=True
    )
    return TaskRef(task_id=task.id)

# backend/jobfinder/scheduling/handlers.py:126-130 (today): untyped payload dict, import inside the function
async def cover_letter(db: AsyncSession, ai: AITasks, task: Task) -> dict:
    from jobfinder.letters import service

    p = task.payload
    return await service.generate(db, ai, task.user_id, p["match_id"], p.get("tone", "professional"), p.get("notes", ""))

# backend/jobfinder/letters/service.py:16-30 (today): the same 'profile done?' rule, raised as ValueError
async def generate(db: AsyncSession, ai: AITasks, user_id: int, match_id: int, tone: str, notes: str = "") -> dict:
    row = (
        await db.execute(
            sa.select(JobMatch, Job, Company)
            .join(Job, Job.id == JobMatch.job_id)
            .outerjoin(Company, Company.id == Job.company_id)
            .where(JobMatch.id == match_id, JobMatch.user_id == user_id)
        )
    ).first()
    if not row:
        raise ValueError("Match not found")
    match, job, company = row
    profile = await db.get(Profile, user_id)
    if not profile or not profile.background:
        raise ValueError("Finish your profile before drafting letters")
