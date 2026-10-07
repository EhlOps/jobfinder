# JobFinder

Profile-driven job matching: onboarding questionnaire + resume/GitHub/portfolio ingestion,
job scraping (Greenhouse/Lever/Ashby, optional LinkedIn/Indeed), LLM matching, cover letter
drafts, a daily email digest, and emailed clarifying questions for uncertain matches.

Stack: FastAPI + uv, Postgres, Vite + React + TypeScript, docker compose.

## Status

All 7 milestones are done: scaffold, auth/profile/uploads, the AI layer with the onboarding
wizard, job ingestion (27 company boards, ~7,000 jobs cached), matching with a matches feed, cover letters, and scheduled ingestion with a daily email (new matches +
clarifying questions).

- Design and milestones: [docs/PLAN.md](docs/PLAN.md)
- Requirements, what's done and what's left (machine-readable): [docs/PRD.json](docs/PRD.json)

## Quickstart

```sh
cp .env.example .env              # set POSTGRES_PASSWORD
cp backend/.env.example backend/.env   # set SECRET_KEY (required) and ADMIN_EMAILS
docker compose --profile dev up --build
```

- Web app: http://localhost:8180
- API: http://localhost:8100 (`/healthz`, `/docs`)
- Mailpit (dev mail inbox): http://localhost:8025

Postgres is also published on `localhost:5433` (for host-side tests and migrations). Set `API_PORT` / `WEB_PORT` / `DB_PORT` in `.env` to change host ports. AI steps need `CLAUDE_CODE_OAUTH_TOKEN` in `backend/.env` (see below).

## Access (invite-only)

There is no sign-up. Add each person yourself, then they set their own password from an emailed link:

```sh
docker compose exec api python -m jobfinder.cli add-user --email them@example.com
docker compose exec api python -m jobfinder.cli list-users
docker compose exec api python -m jobfinder.cli reset-link --email them@example.com   # prints a link instead of emailing (use if SMTP is down)
```

(Plain SQL works too: `INSERT INTO users (email) VALUES ('them@example.com')`, lowercase.) They open the sign-in page and choose "Email me a link". Links work once and expire after `PASSWORD_LINK_TTL_MINUTES` (60). Note that links go out through your real SMTP relay unless you use the Mailpit dev setup.

## Jobs

`docker compose exec worker python -m jobfinder.cli ingest` pulls every board in
`backend/jobfinder/ingest/companies.yaml` (Greenhouse, Lever, Ashby) into the `jobs` table; use
`--company discord` for one board. Add companies by adding rows to that file. LinkedIn/Indeed scraping is
opt-in and against those sites' terms: set `ENABLE_JOBSPY=true` in `backend/.env`, build the worker with
`WITH_JOBSPY=true` (root `.env`), then `ingest --jobspy`. Ingestion is not scheduled yet.

## Matches

Finishing onboarding (or clicking **Find new matches**) queues a matching run in the worker. For each
user it drops jobs that clearly don't fit (level, place, pay floor, sponsorship, years of experience
asked), ranks the rest cheaply, then has Claude score the best ones and explain why. Only
`MATCH_DAILY_LLM_BUDGET` jobs (default 25) are scored per user per rolling 24h to protect your Claude
usage; the page says how many promising jobs are still waiting. Scoring takes about 25 seconds per job.

## Cover letters

Open a match and click **Draft cover letter**: pick a tone, optionally say what to emphasize, and Claude writes it
from your real background and answers (it's told never to invent anything). Edit it in place, copy it, or
download a Word file; redrafting asks before replacing your edits. Always read it before sending.

## Daily email and questions

The worker ingests job boards every 6 hours (`INGEST_INTERVAL_HOURS`) and, once an hour, sends each user a
single email at their chosen local time (Settings): new good matches plus any questions that would sharpen
their matches. Question links are signed and need no login; every email has a one-click unsubscribe. Nothing
is sent when there's nothing to say. Try it without real email:

```sh
docker compose --profile dev up -d mailpit        # inbox at http://localhost:8025
# put SMTP_HOST=mailpit SMTP_PORT=1025 in backend/.env, restart api + worker
docker compose exec worker python -m jobfinder.cli daily --email you@example.com --preview
```

Production mail goes through the `smtp` postfix service (see Email above). `SCHEDULER_ENABLED=false` in
`backend/.env` turns scheduling off.

## Files

Uploaded documents and fetched link text are stored on the `store` docker volume, not in Postgres:
`docker compose exec api ls -R /data/store`. Each document has an ID (`doc_...`) and tags; see PLAN.md.

## Email

The app speaks plain SMTP. In dev, mail goes to Mailpit. In production the `smtp` (postfix)
service relays through Gmail/Outlook (set `RELAYHOST*` in `.env`) or delivers directly
(needs SPF/DKIM/DMARC and open outbound port 25). See PLAN.md.

## AI

All AI runs through the Claude Code CLI (`claude -p`) on your Claude subscription, in the `worker`
container only. There is no API key, and you never need to open the worker container to set it up:

1. Sign in as an admin (admins are only the emails listed in `ADMIN_EMAILS`).
2. Open **AI setup** in the header.
3. On any computer with Claude Code, run `claude setup-token` and paste the token it prints.
4. Click **Save & test**. It is stored on a private docker volume and applies immediately.

Until it's connected, analysis steps show an "AI isn't connected" notice (with a link for admins).
Setting `CLAUDE_CODE_OAUTH_TOKEN` in `backend/.env` still works as a fallback. Usage limits are shared
by everyone using your instance.

## Develop

```sh
docker compose up -d db                      # tests need Postgres on localhost:5433
cd backend && uv sync && uv run pytest       # creates/uses a jobfinder_test database
cd frontend && npm install && npm test        # Vitest + Testing Library (no backend needed)
cd frontend && npm install && npm run dev
```

To run the worker on the host (uses your own logged-in `claude` CLI) against the compose database:

```sh
cd backend && DATABASE_URL=postgresql+asyncpg://jobfinder:change-me@localhost:5433/jobfinder \
  uv run python -m jobfinder.worker
```

Point the host worker (and api) at a shared `STORE_DIR` if you run them on the host. Stop the docker `worker` first (`docker compose stop worker`) so the two don't compete for tasks.
