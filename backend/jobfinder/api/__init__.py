"""api: the HTTP routers, one module per area, mounted by app.py.

    app           create_app, the factory uvicorn runs (`--factory`)
    deps          the dependencies routers declare: DbSession, CurrentUser, AdminUser, Ctx, Store
    health        /healthz and /readyz
    auth          sign-in, sign-out, the signed-in user, password links
    onboarding    profile extraction and the follow-up questions during onboarding
    profile, interview, links, documents    the profile, the Strengthen interview, profile links, uploads
    matches, cover_letters, resumes         matches and what is drafted from one
    questions     clarifying questions, in the app and from the emailed link; unsubscribe
    job_links     pasted job links
    settings, tasks                         user settings and the digest preview; one task's status
    ai_admin, coverage                      the Claude token and status; admin's schedule, sources and coverage

A router parses the request, calls a service, and returns a schema from jobfinder.schemas. No SQL here and no
pydantic models of its own (tests/test_architecture.py checks the second).
"""
