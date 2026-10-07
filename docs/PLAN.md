# JobFinder: implementation plan

## Context
You want a multi-user web app that:
1. Learns who you are through an onboarding questionnaire and your uploads (resume, portfolio, GitHub, socials).
2. Pulls jobs from ATS boards and from LinkedIn/Indeed.
3. Scores each job against your profile.
4. Drafts cover letters.
5. Emails a daily digest of new matches.

When it isn't sure about a job, it emails you targeted questions ("have you done X?"). Your answers become part of your profile and the job is scored again.

The repo will go in the empty `jobfinder/` folder, which is greenfield. Two sibling projects already solve most of the infrastructure, so we copy their patterns instead of reinventing them:
- **`stockmedia/`**: compose layout, uv Dockerfile, async SQLAlchemy and Alembic, and a Vite+React+TanStack Query frontend served by nginx with an `/api` proxy (same origin, cookie auth, no CORS).
- **`samai/`**: only the hardened non-root uv Dockerfile and the idea of a worker image that bundles the `claude` CLI (already applied in the scaffold). **The AI layer and the task queue are written from scratch for jobfinder, and nothing is ported from samai.**

### Decisions
- **Users:** invite-only (DEC-14): no self-signup. The owner adds an email-only row (`python -m jobfinder.cli add-user --email x@y.com`); the person asks for an emailed link on the sign-in page and sets a password, which activates the row. `reset-link` prints a link when SMTP is down. `SECRET_KEY` is required (32+ chars). A `users` table (email + argon2 password hash) and a `sessions` table (random token, user_id, expires_at). The token goes in an httpOnly cookie. No auth library, OAuth or email verification for now.
- **Job sources:**
  - Public ATS APIs: Greenhouse, Lever and Ashby.
  - LinkedIn/Indeed through `python-jobspy`. This is opt-in behind an env flag, rate-limited and capped per run. It breaks those sites' ToS and is fragile, so it's isolated in its own source module and can fail without breaking ingestion.
- **AI: our own layer, backed only by the Claude Code CLI (headless `claude -p`) on your subscription.** There is no Anthropic SDK and no API key.
  - Details are in the "AI layer" section below.
  - Subscription rate limits are the real bottleneck, so calls are budgeted. A cheap prefilter runs first, then the LLM scores only the top N per user per day, cached per profile version. A global concurrency cap keeps the worker from hammering the subscription.
  - Remember that your subscription is being used on behalf of other people in the group; usage limits are shared across all of them.
- **Email: self-hosted SMTP in docker, no third-party email API.** Only a small group uses the app, so volume is low.
  - The app only speaks plain SMTP, through `aiosmtplib` with `SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASSWORD`/`SMTP_STARTTLS`. Resend and the email-provider abstraction are dropped.
  - **Production:** a `smtp` compose service using the `boky/postfix` image. The app sends to `smtp:587` on the private compose network, and the port is never published to the host. Postfix has two modes, chosen by env alone with no code change:
    - **Relay mode (recommended default):** `RELAYHOST=[smtp.gmail.com]:587` plus a Gmail/Outlook app password. Mail leaves through a reputable provider, so it lands in inboxes. Gmail's ~500/day limit is plenty for this group.
    - **Direct mode:** Postfix delivers straight to recipients' mail servers and signs mail with DKIM (the image has built-in OpenDKIM; keys go in a volume). This needs a domain with SPF/DKIM/DMARC records and a host with outbound port 25 open. Residential ISPs and most clouds block port 25, and mail from those IPs gets spam-foldered, so only use this on a proper VPS.
  - **Development:** the `mailpit` service (dev profile) catches all mail at `localhost:8025`, with `SMTP_HOST=mailpit`, `SMTP_PORT=1025`.
  - You answer questions through signed links to an in-app Q&A page.

## Architecture
```
docker-compose: db (postgres:16) · api (FastAPI) · worker (APScheduler + queue consumer, has claude CLI)
                · web (Vite build → nginx, proxies /api) · smtp (boky/postfix, internal only)
                · mailpit (dev profile; catches mail locally)
```
Host ports: API on `127.0.0.1:${API_PORT:-8100}`, web on `127.0.0.1:${WEB_PORT:-8180}`. Port 8000 is used by another local project.

**Status:** all seven milestones are done (scaffold; auth/profile/file store; AI layer, queue, worker and onboarding wizard; job ingestion; matching and the matches feed; cover letters; scheduling, email, daily digest and clarifying questions), plus the in-app "Connect Claude" setup and the profile/settings pages. Requirements, progress and known gaps live in [`PRD.json`](PRD.json), which is the source of truth; update both when something changes.
The api and worker share one Python package. Only the api runs `alembic upgrade head` on start.

### Repo layout (`jobfinder/`)
```
docker-compose.yml  .env.example  README.md
backend/
  pyproject.toml  uv.lock  Dockerfile  Dockerfile.worker  docker-entrypoint.sh  alembic/
  jobfinder/
    config.py db.py models.py main.py worker.py
    auth/            routes, password hashing, session dependency
    api/             onboarding.py profile.py documents.py jobs.py matches.py
                     cover_letters.py questions.py settings.py
    ai/              claude_code.py (CLI runner), tasks.py (typed AI functions), schemas.py,
                     prompts/*.md (profile_extract, followups, match, dedupe_question, cover_letter)
    ingest/          base.py (JobPosting dataclass + normalize/dedupe)
                     sources/greenhouse.py lever.py ashby.py jobspy_source.py
                     companies.yaml (slug, ats, prestige_tier, size, industry)
    profile/         resume_parse.py (pdfplumber + python-docx), github.py (REST API),
                     web_page.py (httpx + trafilatura), extract.py (LLM → structured profile)
    matching/        prefilter.py (hard filters + skill overlap), score.py (LLM), service.py
    notify/          email.py (single async send_email() over aiosmtplib), templates/ (Jinja, HTML + text),
                     digest.py, questions.py
    scheduling/      queue.py (own Postgres task queue), handlers.py (task name → function)
  tests/
frontend/
  package.json vite.config.ts Dockerfile nginx.conf
  src/ lib/api.ts  auth/  routes/  components/
```

## File store (docker volume, not Postgres)
Uploaded files and the text snapshot of each fetched link live on the `store` volume (`/data/store`, shared by api and worker), one directory per document:
`/data/store/{user_id}/{doc_id}/{meta.json, original.<ext>, text.txt}`. `doc_id` is `doc_` + ms-timestamp hex + random hex, so it sorts by creation time. `meta.json` holds kind, filename, sha256, size, `chars`, fetch status and **tags** (automatic: `kind`, `ext`, `source`, `url`, `link_kind`; plus user-defined). API: list with `?kind=&tag=key:value`, `GET /api/documents/{id}`, `/download`, `/text`, `PUT /tags`. Identical uploads are deduplicated by sha256; writes are atomic. Code: `backend/jobfinder/storage/documents.py` (`DocumentStore`; an S3/MinIO class can replace it without touching callers). Cover letters stay in Postgres.

## Data model (SQLAlchemy 2, Alembic)
- `users`: id, email (unique), password_hash, timezone, digest_hour, digest_enabled, is_admin, created_at.
- `sessions`: token (pk, the SHA-256 hex of the random 32-byte cookie token, so a leaked table can't be replayed), user_id, created_at, expires_at. The `current_user` dependency hashes the cookie and looks it up here; the worker prunes expired rows.
- `profiles` (one per user):
  - `status` JSONB: new grad?, graduation date, need-job-by date, target locations, remote preference, relocation, salary min/target, prestige preference, company size, industries, visa sponsorship needed, target roles and seniority.
  - `background` JSONB: education, internships/co-ops/jobs (company, role, dates, what you did), projects, skills with proficiency, links.
  - `version` int, bumped on every change.
- `profile_facts`: user_id, question, answer, source (`onboarding|followup|job_question`), job_id nullable. These are atomic "have you done X?" answers, and every LLM prompt includes them.
- `companies` (built): name, ats, slug, prestige_tier (1–5), size, industry, enabled, last_fetched_at, last_error. Seeded from `ingest/companies.yaml`.
- `jobs` (built; the cache). `workplace_type` (remote|hybrid|onsite) replaces a boolean remote flag, and `company_name` is stored on every row because JobSpy jobs have no company row:
  - Columns: source, external_id, company_id, title, location, remote, salary_min/max, seniority, description_text, url, posted_at, first_seen_at, last_seen_at, is_active, dedupe_hash.
  - Unique on (source, external_id). An index on `dedupe_hash` (normalized company+title+location) collapses the same posting seen in several sources.
  - A job that is missing from a source's latest response is marked `is_active=false`.
- `job_matches` (built): user_id, job_id, profile_version, prefilter_score, llm_score (0–100), confidence, verdict (strong|good|stretch|no), reasons JSONB, unknowns JSONB, status (new|saved|applied|dismissed), digested_at. Unique on (user_id, job_id).
- `clarifying_questions` (built): user_id, question, why, match_ids (the matches that raised it), status (pending|emailed|answered|skipped), answer, created/emailed/answered timestamps. Also built: `users.is_admin/question_emails_enabled/last_digest_at` and `job_matches.questions_collected`.
- `cover_letters` (built): user_id, job_id (unique together), content, tone, edited, profile_version, generated_at, updated_at. One current draft per user+job; user edits set `edited`, and the API requires `force` to redraft over an edited letter.
- `tasks` (built): id, user_id nullable, kind, payload JSONB, status (queued|running|done|failed), result JSONB, error, attempts, dedupe_key, run_after, locked_at, created_at, finished_at. Claimed with `SELECT … FOR UPDATE SKIP LOCKED`. `enqueue(dedupe=True)` is atomic (partial unique index on `dedupe_key` for queued/running tasks). The worker heartbeats `locked_at` every 60s while a task runs; the reaper (every 60s) requeues tasks silent for 5 minutes, or fails them once they used all 3 attempts. The frontend polls `GET /api/tasks/{id}` for long AI operations.
- `ai_calls` (built): id, task kind, model, duration_ms, ok, error_kind, input_tokens, output_tokens, created_at. Used for debugging and for watching subscription usage.

## AI layer (`backend/jobfinder/ai/`, written from scratch)
**`claude_code.py`: `ClaudeCode.run(prompt, *, system, output: type[BaseModel] | None, model="sonnet", timeout_s=180) -> BaseModel | str`**
- Spawns `claude` with `asyncio.create_subprocess_exec` and passes the prompt on **stdin**, so it never appears in argv. The flags below were checked against the installed CLI 2.1.288:
  ```
  claude -p --output-format json --model <haiku|sonnet|opus>
         --system-prompt <system>          # replaces the default agent prompt
         --tools "" --restricted           # no tools; ignore user/project settings, CLAUDE.md, hooks
         --permission-prompts none --strict-mcp-config --disable-slash-commands
         --no-session-persistence
         [--json-schema <output.model_json_schema()>]
  ```
- Prompt injection from scraped job postings can't reach anything, because the model has no tools. The worst it can do is skew text that you then review.
- **Environment:** a clean env with only `PATH`, `HOME` and `CLAUDE_CODE_OAUTH_TOKEN`. `ANTHROPIC_API_KEY` is explicitly excluded so the CLI can never silently switch to API billing. Don't use `--bare`, because it forces API-key auth.
- **Parsing:** read the JSON envelope, take `structured_output` (or, as a fallback, the JSON in `result`), and validate it with the Pydantic `output` model. If validation fails, make one repair call that includes the validation error, then give up.
- **Errors:** `AIError(kind)`, where kind is one of `auth` (not logged in or token expired), `rate_limited` (usage limit), `timeout`, `bad_output` or `cli_missing`. These are classified from stderr and the envelope. The queue retries `rate_limited` and `timeout` with backoff.
- **Concurrency and logging:** an `asyncio.Semaphore(AI_MAX_CONCURRENCY=2)` limits parallel calls. Each call writes an `ai_calls` row. On timeout the whole process group is killed.

**`tasks.py`: typed functions, one per feature, each with its own prompt file and output model:**
- `extract_profile(sources) -> Background`, model sonnet. **Built.**
- `followup_questions(status, background, facts) -> list[FollowupQuestion]`, model sonnet. **Built.**
- `score_match(status, background, facts, job) -> MatchScore {score, confidence, reasons, gaps, unknowns}`, model haiku. **Built** (the verdict is derived from the score in code).
- `already_answered(question, facts) -> bool`, model haiku. *Not built yet.*
- `cover_letter(name, background, facts, job, analysis, tone, notes) -> str`, model sonnet. **Built.** Retries once if the text has [placeholders] or is too short.

Every prompt includes the guardrail "use only facts present in the profile; never invent experience, metrics or titles". Untrusted text (job descriptions, scraped pages) is wrapped in delimited blocks and labelled as data.

**Where it runs:** only in the worker container. The API enqueues a task and returns its id, and the UI polls for the result. For local non-docker dev, the host's logged-in `claude` is used.

**Auth setup (from the web app, no worker access needed):** the admin runs `claude setup-token` on their own machine and pastes the token on the **AI setup** page (`/settings/ai`). It is stored as a 0600 file on the `claude-auth` volume (`/data/claude/oauth_token`), shared by api (writes) and worker (reads before every call, so no restart). Precedence: saved token, then `CLAUDE_CODE_OAUTH_TOKEN` env, then the CLI's own login. `GET /api/ai/status` (any user) reports whether it's connected; `PUT/DELETE /api/admin/ai/token` and `POST /api/admin/ai/check` are admin-only; the check runs an `ai_check` task in the worker and shows the real error. Admins are only the emails in `ADMIN_EMAILS`.

**Testing:** a `FakeClaude` with canned outputs per task is injected via `get_ai()`. There's one subprocess-level test using a stub `claude` shell script that emits a fixed envelope, which checks argv, the env scrubbing and the error classification. No test calls the real CLI.

## Core flows
1. **Onboarding wizard** (frontend `routes/onboarding/*`, backend `api/onboarding.py`):
   - Step 1: status questions, a fixed form covering everything you listed plus visa sponsorship.
   - Step 2: upload resume and portfolio and add links. Recommend uploading the LinkedIn "Save to PDF" export, since LinkedIn profiles can't be scraped reliably.
   - Step 3: the background is extracted (`profile/extract.py`: resume text + GitHub repos/languages/READMEs + portfolio text → LLM → structured `background`), and you review and edit it.
   - Step 4: **adaptive follow-ups**. The LLM finds gaps in your background ("You list 2 co-ops. What did you own at X?", "Have you used Kubernetes in production?") and asks 5–10 questions. Answers become `profile_facts`.
2. **Ingestion** (built; `python -m jobfinder.cli ingest`, also the `ingest_jobs` task; scheduling every 6h arrives in milestone 7):
   - Each source returns `list[JobPosting]`. Upsert into `jobs`, then deactivate jobs that have disappeared.
   - JobSpy queries come from the distinct role and location combinations across all users' targets, capped per run.
   - After ingestion, enqueue `match_user` for each active user.
3. **Matching** (`matching/service.py`):
   - (a) SQL hard filters: active, location/remote compatible, salary not below your floor when known, seniority compatible, sponsorship if you need it.
   - (b) Cheap prefilter score: skill keyword overlap, title similarity, recency, prestige fit.
   - (a, as built) also drops jobs that ask for 3+ years of experience when the candidate only wants entry-level roles, and level labels understand Engineer I/II/III/IV.
   - (c) The top N unscored jobs (configurable, default 25/user/day) go to the LLM with a JSON schema returning `{score, confidence, verdict, reasons[], unknowns[{question, why}]}`.
   - Results are cached per `profile_version`. When the profile changes, only saved jobs and still-visible new matches are scored again.
4. **Uncertain jobs → email questions:**
   - (as built) Condition: `confidence < 0.6` and score ≥ 50, i.e. it could be a good match but the model is unsure. The `unknowns` become `clarifying_questions` after one LLM call that merges duplicates across jobs and drops anything already answered or previously asked.
   - Questions are deduped against existing `profile_facts` by asking the LLM whether the fact is already answered, so you aren't asked "have you done React?" twice.
   - (as built) Pending questions go out in the **same daily email** as the digest (max 6; one email per user per day), with a signed 14-day token link to `/q/<token>` that works without logging in.
   - Answering saves facts, bumps the profile version and rescores the affected jobs.
5. **Cover letter** (`api/cover_letters.py`):
   - Generated on demand from your profile, facts and the job description, with the Resume Editor guardrails: never invent experience, metrics or titles.
   - Tone options. Edit in the app, then copy or download as .docx.
6. **Daily digest:**
   - (as built) An hourly worker task (at :05) sends to users whose local `digest_hour` has arrived (window of 6 hours, once per local day); never sends an empty email.
   - Contents: new matches since the last digest with score ≥ threshold, plus a teaser for pending questions.
   - Matches are marked `digested_at`.

## Frontend (Vite + React + TS, TanStack Query, react-router, Tailwind)
Pages:
- Login and signup.
- Onboarding wizard.
- **Matches feed**: score badge, verdict, top reasons, filters for score, remote, salary and status, and save/dismiss/applied actions.
- **Job detail**: description, match reasoning, generate/edit cover letter.
- **Questions inbox.**
- **Profile**: edit status and background, documents, links, facts.
- **Settings**: digest time and enabled flag, sources.

`lib/api.ts` is a typed fetch wrapper. Types are generated from FastAPI's OpenAPI with `openapi-typescript`.

## Build order (milestones)
1. **(done)** Scaffold:
   - Compose, backend skeleton (config, db, Alembic, health check), frontend skeleton, nginx proxy, Mailpit.
   - Copy the Dockerfile and compose patterns from stockmedia/samai.
2. **(done)** Auth, plus the profile, document and link models and API. Resume and GitHub parsing.
3. **(done)** Our own AI layer (`ai/`), the task queue with `tasks`/`ai_calls` tables, a worker loop that consumes it, and `GET /api/tasks/{id}`. Then profile extraction, follow-up questions, and the frontend login and onboarding wizard.
   - **Done.** The remaining AI functions (`score_match`, `already_answered`, `cover_letter`) land with milestones 5-7.
4. **(done)** Ingestion: Greenhouse, Lever and Ashby, the company seed list and the job cache. JobSpy source behind a flag.
5. **(done)** Matching pipeline and the matches feed UI.
6. **(done)** Cover letters.
7. **(done)** Worker scheduling, the `smtp` (postfix) compose service, `send_email()`, the daily digest and the clarifying-question emails with Q&A links.
   - Already in the scaffold: SMTP env vars, the `smtp` compose service, Mailpit and the `aiosmtplib` dependency. Remaining: the sending code, templates, scheduler and the flows above.

## Verification
- `docker compose --profile dev up --build` brings up db, api, worker, web, smtp and mailpit. Check `/healthz` and that the web app loads at `localhost:8180`.
- SMTP relay test: point `SMTP_HOST=smtp`, set the relay credentials, and run `uv run python -m jobfinder.cli test-email you@…`. Confirm it arrives in a real inbox, not spam, and check the postfix logs with `docker compose logs smtp`.
- Backend: `docker compose up -d db`, then `uv run pytest` (tests create and use a `jobfinder_test` database on `localhost:5433`). Already covered: auth, profile, documents, links, the SSRF guard, the queue, and the AI runner against a stub `claude` binary (argv, env scrubbing, error classification, repair retry, timeout).
  - Source parsers tested against saved JSON fixtures (no network).
  - Dedupe and deactivation logic.
  - Prefilter rules.
  - Matching and question generation using a `FakeAI` (the pattern `tests/test_onboarding.py` already uses).
  - Digest selection by timezone and hour.
  - Signed-token Q&A endpoint.
- AI: `docker compose exec worker python -m jobfinder.cli ai-check` (needs `CLAUDE_CODE_OAUTH_TOKEN`).
- End to end, by hand (steps 1 was also checked with a one-off Playwright run, with a worker on the host using the logged-in CLI):
  1. Get invited, set a password from the emailed link, and finish onboarding with a real resume and GitHub URL. Confirm the extracted profile looks right.
  2. Trigger ingestion with an admin endpoint/CLI (`uv run python -m jobfinder.cli ingest`) and see rows in `jobs`.
  3. Run `match` and see the scored feed.
  4. Generate a cover letter.
  5. Run `digest --now` and `questions --now`, then open Mailpit at `localhost:8025` to see both emails.
  6. Click the question link, answer, and confirm the job is scored again.
- Frontend: `npm run build` and `tsc --noEmit` pass.
