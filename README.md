# JobFinder

Profile-driven job matching: onboarding questionnaire + resume/GitHub/portfolio ingestion, job scraping
(Greenhouse, Lever, Ashby, Workday and other public ATS boards, plus recipes for custom careers sites; optional
LinkedIn/Indeed and USAJobs), LLM matching, cover letter drafts, a daily email digest, and emailed clarifying
questions for uncertain matches.

**Stack:** FastAPI + uv, Postgres, valkey (task delivery for a Postgres task ledger, run by taskiq workers),
Vite + React + TypeScript, and a single-node k3s cluster for staging and production.

Production: https://jobs.ehlops.com (namespace `jobfinder`). Staging: https://staging.ehlops.com
(`jobfinder-staging`).

## Documentation

| Document | Covers |
|---|---|
| This README | What the app does, the architecture, local development, access, the CLI, deploys |
| [docs/k3s.md](docs/k3s.md) | The runbook for both clusters: [install](docs/k3s.md#install--re-converge), [releasing](docs/k3s.md#releasing-118), [rollback](docs/k3s.md#rollback), [staging](docs/k3s.md#staging-43), [the task platform](docs/k3s.md#task-platform-48), [KEDA](docs/k3s.md#keda-52), [scaling](docs/k3s.md#scale), [logs and troubleshooting](docs/k3s.md#logs--troubleshooting), [backups](docs/k3s.md#backups-47) |
| [deploy/k8s/README.md](deploy/k8s/README.md) | The manifests: layout, rendering and applying, sealed secrets |
| [backend/.env.defaults](backend/.env.defaults) | Every setting, with its default and what it does |
| [Backend packages](#backend-layout) | Each package's `__init__.py` docstring describes its modules and rules |
| [integrations/claude/prompts/](backend/jobfinder/integrations/claude/prompts/) | The prompt for each AI task |
| [integrations/site_terms/TERMS.md](backend/jobfinder/integrations/site_terms/TERMS.md) | The terms and robots.txt review of big-tech careers sites, and which ones are refused |
| [docs/PLAN.md](docs/PLAN.md), [docs/PRD.json](docs/PRD.json), [docs/HISTORY.md](docs/HISTORY.md) | The original design and milestones. Not kept up to date. |

## How it fits together

```text
browser → host Caddy (TLS) → Traefik 127.0.0.1:8280 → web (Caddy, the SPA) → api (FastAPI, 2 pods)
                                                                              │ writes a ledger row, then publishes its id
                                    Postgres `tasks` (the truth) ◀────────────┤
                                    valkey streams jobfinder:ai / :io / :render
                                                 │
                     worker-ai (Claude, 1 pod) · worker-io (ingest, uploads, links; 1–4 pods by KEDA) · renderer (opt-in)
                     scheduler (1 pod): cron, relay (re-sends unpublished rows), reaper (requeues stuck rows)
```

Every piece of background work is a row in the `tasks` table, written in the same transaction as the request
that caused it. valkey only delivers the row's id to a worker; if a publish is lost, the scheduler's relay sends
it again. Details: [docs/k3s.md, "Task platform"](docs/k3s.md#task-platform-48).

### Backend layout

`backend/jobfinder/` is layered. A request goes `api` → `services` → `repositories` → `models`; nothing below
imports anything above it, and `lint-imports` (blocking in CI) enforces it.

Each package name links to its docstring, which lists its modules.

| Package | What lives there |
|---|---|
| [`api/`](backend/jobfinder/api/__init__.py) | FastAPI routers: parse the request, call a service, commit, return a schema. No SQL, no pydantic models of its own. |
| [`schemas/`](backend/jobfinder/schemas/__init__.py) | Pydantic request and response models (the API contract). |
| [`services/`](backend/jobfinder/services/__init__.py) | Business logic. Takes a session, never commits: the caller (router, task handler, CLI) owns the transaction. |
| [`repositories/`](backend/jobfinder/repositories/__init__.py) | SQL (SQLAlchemy queries), one module per aggregate. |
| [`models/`](backend/jobfinder/models/__init__.py) | SQLAlchemy ORM models. Migrations are in `backend/alembic/`. |
| [`integrations/`](backend/jobfinder/integrations/__init__.py) | External systems: ATS boards (`ats/`), Claude (`claude/`), SMTP, GitHub, careers-site recipes (`recipes/`), board directories, USAJobs, jobspy. |
| [`tasks/`](backend/jobfinder/tasks/__init__.py) | The task ledger, the taskiq brokers and the handlers for each queue (`ai`, `io`, `render`). |
| [`scheduler/`](backend/jobfinder/scheduler/__init__.py) | The scheduler process: cron table, relay and reaper. |
| [`cli/`](backend/jobfinder/cli/__init__.py) | The Typer CLI, `jobfinder <group> <verb>`. |
| [`core/`](backend/jobfinder/core/__init__.py) | Config, database engine, request context and small helpers; kept tiny and imports nothing else from `jobfinder`. |

Beside `integrations/` sit three small helpers on the same layer:
[`profile/`](backend/jobfinder/profile/__init__.py) (reading resumes and web pages),
[`resumes/`](backend/jobfinder/resumes/__init__.py) (tailored resumes: export and keywords) and
[`storage/`](backend/jobfinder/storage/__init__.py) (the document store).
[`renderer/`](backend/jobfinder/renderer/__init__.py) is the Playwright container's entrypoint; its client is
[`integrations/render/`](backend/jobfinder/integrations/render/__init__.py).
[`context.py`](backend/jobfinder/context.py) adds the integrations to core's process context.

Subpackages with their own docstring:

- [`services/matching/`](backend/jobfinder/services/matching/__init__.py): hard filters, a cheap rank, then Claude's scoring.
- [`services/resolver/`](backend/jobfinder/services/resolver/__init__.py): the ladder that turns a pasted job link
  into a board.
- [`integrations/ats/`](backend/jobfinder/integrations/ats/__init__.py): one fetcher per ATS vendor and their registry.
- [`integrations/recipes/`](backend/jobfinder/integrations/recipes/__init__.py): recipes, data that says how to read a
  careers site no ATS adapter covers.
- [`integrations/claude/`](backend/jobfinder/integrations/claude/__init__.py): the `claude -p` client and one typed
  task per AI feature.
- [`integrations/directories/`](backend/jobfinder/integrations/directories/__init__.py): public board lists used for
  discovery.
- [`integrations/site_terms/`](backend/jobfinder/integrations/site_terms/__init__.py): the reviewed big-tech careers
  sites.

`tests/test_architecture.py` checks what import-linter can't:
commits happen only at the boundaries, `core` modules stay under 150 lines (`config.py`, the list of every setting,
may reach 160), and `api/` defines no pydantic `BaseModel`.

### Frontend layout

`frontend/src/` is a React single-page app; server state goes through TanStack Query.

| Path | What lives there |
|---|---|
| `App.tsx` | The routes. `/login`, `/reset-password` and `/q/:token` (questions from an email) need no session. |
| `routes/` | One component per page; `routes/onboarding/` holds the onboarding steps. The `Admin*` pages are under `/settings/`. |
| `components/` | Pieces shared by pages: match details, the cover letter and resume panels, AI notices, form fields. |
| `lib/api.ts` | The fetch wrapper; non-2xx responses throw `ApiError` with the server's `detail`. |
| `lib/types.ts` | The response types, written by hand to match `backend/jobfinder/schemas/`. |
| `lib/useTask.ts` | Polls `/api/tasks/{id}` until a background task finishes. |
| `auth/AuthProvider.tsx` | The signed-in user, sign-in and sign-out. |

Tests sit next to the file they test (`*.test.tsx`); shared setup is in `src/test/`.

### Configuration

Every backend setting is an env var, listed with its default and a comment in
[`backend/.env.defaults`](backend/.env.defaults) (committed, no secrets). `backend/.env` (gitignored, start from
`backend/.env.example`) overrides any of them and holds the secrets; the process environment (in the clusters, the
`jobfinder-config` ConfigMap and the `jobfinder-env` Secret) overrides both. There are no defaults in Python: a key
missing from all three, or a bad value, stops the api, a worker or the CLI at startup with a message naming every
such key. Run backend commands from `backend/`, where both files are read. To add a setting, declare it in
`backend/jobfinder/core/config.py` and add it, in the same place, to `.env.defaults`; a test fails when the two
disagree. In the clusters, `backend/.env.defaults` becomes the ConfigMap (`backend/kustomization.yaml`) and each
overlay merges its own values in.

## Develop

You need [uv](https://docs.astral.sh/uv/), Node 22, Docker, `kubectl` and [k3d](https://k3d.io/stable/#installation)
(k3s in Docker; a release binary in `~/.local/bin` is enough).

```sh
cp backend/.env.example backend/.env    # set SECRET_KEY (required)
cd backend && uv sync && uv run jobfinder dev
```

`jobfinder dev` creates (or starts) a k3d cluster `jobfinder-dev` from `deploy/dev/` with Postgres on
`127.0.0.1:5433` and valkey on `127.0.0.1:6380`, runs the migrations, then runs on the host, with reload:

- the api on `127.0.0.1:8100` (`/healthz`, `/docs`),
- the scheduler,
- worker-ai and worker-io.

Their output is interleaved with a prefix per process. Ctrl-C stops them; the cluster keeps running, so the next
`jobfinder dev` starts quickly.

```sh
uv run jobfinder dev status    # the cluster, its pods and the host ports
uv run jobfinder dev down      # delete the cluster, its data and its kubeconfig
```

Set `DEV_PG_PORT`, `DEV_VALKEY_PORT` or `DEV_API_PORT` in your shell to move a port.

The frontend runs separately:

```sh
cd frontend && npm ci && npm run dev    # http://localhost:5173, proxies /api to 127.0.0.1:8100
```

On a remote machine, tunnel it: `ssh -L 5173:localhost:5173 user@server`.

Local notes:

- **First user.** There is no sign-up. Run `uv run jobfinder users add`, `users admin grant` and `users reset-link`
  with `--email you@example.com`; the last prints a link to set your password (see [Access](#access-invite-only)).
- **Mail.** The dev stack has no mail server. Point `SMTP_*` in `backend/.env` at a relay you can use, or rely on
  `users reset-link`.
- **Claude.** worker-ai runs your own `claude` CLI (`CLAUDE_BIN`) with the token from the AI setup page or
  `CLAUDE_CODE_OAUTH_TOKEN` in `backend/.env`.
- **Files.** Uploads land in `backend/store/` (`STORE_DIR`).

### Tests and checks

```sh
cd backend
uv run jobfinder dev up --deps-only    # only the cluster with Postgres and valkey, then exits
uv run pytest -n auto                  # one jobfinder_test_<xdist worker> database each
uv run ruff check .
uv run lint-imports                    # the layer contracts (blocking in CI)

cd frontend
npm test                               # Vitest + Testing Library, no backend needed
npm run typecheck
```

CI (`.github/workflows/`) runs on pull requests and pushes to `dev` and `main`, each workflow only when its paths
change:

| Workflow | Runs |
|---|---|
| `backend` | `ruff`, `pytest -n auto` and `lint-imports` against a Postgres service |
| `k8s` | Renders every overlay with kustomize, validates it with kubeconform, and checks the scaling rules (worker-ai never autoscaled, worker-io's replicas left to KEDA) |
| `images` | Builds the images whose directory changed and, on a push, pushes them to ghcr.io, then calls `deploy-staging` (dev) or `deploy-prod` (main) |

The frontend's tests and typecheck are not in CI; run them before you push. The image build runs `tsc` as part of
`npm run build`.

The tests connect to `postgresql+asyncpg://jobfinder:jobfinder@localhost:5433/postgres` and create their own
databases there. `TEST_ADMIN_URL` overrides that URL (URL-encode special characters such as `@` and `!`).

### Parallel worktrees

Give each worktree its own `TEST_DB` prefix, so parallel `pytest` runs don't drop each other's schema. Within one
run every xdist worker uses `<TEST_DB>_<worker>` (built once, each test rolled back) and `<TEST_DB>_<worker>_rc`
for tests marked `real_commit`:

```sh
git worktree add ../jobfinder-<task> -b plan/<task> dev
cd ../jobfinder-<task>/backend && uv sync
TEST_DB=jobfinder_test_<task> uv run pytest -n auto -q   # TEST_DB defaults to jobfinder_test
```

All worktrees share the one dev cluster's Postgres.

## Access (invite-only)

There is no sign-up. Add each person yourself, then they set their own password from an emailed link. In a
cluster, run the CLI in the api pod (`-n jobfinder-staging` for staging); locally, `uv run jobfinder ...` from
`backend/`:

```sh
kubectl -n jobfinder exec deploy/api -- jobfinder users add --email them@example.com
kubectl -n jobfinder exec deploy/api -- jobfinder users list
kubectl -n jobfinder exec deploy/api -- jobfinder users reset-link --email them@example.com   # prints a link instead of emailing (use if SMTP is down)
```

They open the sign-in page and choose "Email me a link". Links work once and expire after
`PASSWORD_LINK_TTL_MINUTES` (60).

### Admins

Admin is a flag on the user, managed from the CLI:

```sh
kubectl -n jobfinder exec deploy/api -- jobfinder users admin grant --email you@example.com
kubectl -n jobfinder exec deploy/api -- jobfinder users admin revoke --email you@example.com
```

1. Add the person as a normal user first (`users add` above); the email must match. Granting a pending user works
   and applies once they activate.
2. It takes effect on their next page load, with no re-login and no restart.
3. `users list` shows an `admin` column to confirm. Admins get the **AI setup** page and the admin pages (for
   example Admin › Sources, to re-enable boards that were auto-disabled, and Admin › Coverage).

First-time setup is `users add`, then `users admin grant`, then `users reset-link`.

### The CLI

Every admin command is `jobfinder <group> <verb>`; `jobfinder --help`, and `--help` on every group and verb, list
them. Exit codes: 0 ok, 1 the command failed (the message is on stderr), 2 a usage error.

| Group | Verbs |
|---|---|
| `users` | `add`, `list`, `reset-link`, `admin grant`, `admin revoke` |
| `boards` | `add`, `list`, `set` |
| `ingest` | `run` (`--ats`, `--company`, `--jobspy`) |
| `digest` | `send` (`--email`, `--preview`) |
| `questions` | `collect` |
| `email` | `test --to ADDRESS` |
| `ai` | `check` (run it in worker-ai, which has the Claude CLI) |
| `tasks` | `list` (`--status`, `--kind`, `--limit`), `retry ID` |
| `dev` | `up` (`--deps-only`), `down`, `status` (local only) |

A long verb such as `ingest run` can outlast an exec session; in a cluster, run it as a one-off Job
([docs/k3s.md, "Admin commands"](docs/k3s.md#admin-commands-49)).

## Jobs

The `companies` table is the only list of boards: a new deployment gets the starting set from migration 0021,
weekly discovery adds more, and pasting a job link adds its board. The scheduler queues an ingest tick every 15
minutes (`INGEST_TICK_MINUTES`), and each tick fetches only the boards that are due: busy boards about hourly,
quiet ones up to weekly, failing ones with a backoff (`INGEST_*` in `.env.defaults`). A board that fails
`INGEST_MAX_CONSECUTIVE_FAILURES` (5) times in a row is disabled until an admin re-enables it.

`jobfinder ingest run` fetches every enabled board now; `--company discord` fetches one. Admins manage boards with
the CLI (validated against the live board before it is stored):

```sh
jobfinder boards add --ats greenhouse --slug acme --name "Acme Inc"
jobfinder boards list [--ats lever] [--origin seed|discovered|user]
jobfinder boards set --id 42 --prestige 4 --industry fintech \
  [--size startup|mid|large] [--sponsors-visas yes|no] [--enabled yes|no] [--name "Acme"]
```

**Pasted links.** A link from a site that isn't a known ATS goes through the resolver: known ATS patterns first,
then a learned API recipe, and as a last step Claude writes a recipe (`RESOLVER_LLM_ENABLED`). A recipe board
whose results degrade is re-induced automatically.

**LinkedIn/Indeed** scraping is opt-in and against those sites' terms: set `ENABLE_JOBSPY=true`, and install the
`jobspy` extra (`uv sync --extra jobspy` locally; build the worker image with `--build-arg WITH_JOBSPY=true`).
Then `ingest run --jobspy`, and the scheduled runs include it.

**US federal jobs** (engineering, computer science, IT and program-management series) come from the official
USAJobs API. Request a free key at <https://developer.usajobs.gov/APIRequest>, then set `USAJOBS_API_KEY` and
`USAJOBS_EMAIL`; the source turns on once both are set (nationwide plus users' target locations; postings not
seen for 14 days expire). Postings state that U.S. citizenship is required, so they are filtered out for users
who need sponsorship.

### Optional: the render worker (JavaScript-only careers sites)

Some careers sites only show their jobs after JavaScript runs. A separate, sandboxed headless-browser worker
(`renderer`, image built from `backend/Dockerfile.renderer`, stream `jobfinder:render`) can open such a page when
a user pastes a link from it. It records the JSON the page loads and, where it can, turns that into an ordinary
API recipe so later fetches need no browser. It is off by default (0 replicas). To turn it on in an environment,
patch the `renderer` Deployment to 1 replica in the overlay and merge `RENDERER_ENABLED=true` into its
`jobfinder-config`; without the setting nothing is sent to the renderer and boards that need it are skipped.

The pod runs as a non-root user with a read-only filesystem, no capabilities, no Secret and no volumes. Every
request the page makes is checked first: only public http(s) addresses, robots.txt honoured, a 30-second limit
per page, and no downloads or saved cookies. Try it on one page without the database:

```sh
docker build -f backend/Dockerfile.renderer -t jobfinder-renderer backend
docker run --rm jobfinder-renderer python -m jobfinder.renderer capture https://careers.example.com/jobs
```

## Matches

Finishing onboarding (or clicking **Find new matches**) queues a matching run. For each user it drops jobs that
clearly don't fit (level, place, pay floor, sponsorship, years of experience asked), ranks the rest cheaply, then
has Claude check each requirement in the posting against your profile (met / partial / not in your profile / not
met) and computes the score from that checklist. Requirements your profile is silent on count as neutral, not as
misses, and each one becomes a question you can answer on the job page. The **Strengthen** page keeps
interviewing you the way a recruiter would until your profile is complete. Only `MATCH_DAILY_LLM_BUDGET` jobs
(default 25) are scored per user per rolling 24h to protect your Claude usage; each user can change that number,
or turn the limit off, under **Settings**; the page says how many promising jobs are still waiting. Scoring uses
Sonnet (`MATCH_MODEL`).

A background planner (`plan_work`, every `PLANNER_INTERVAL_MINUTES`) queues this work for users who need it, a
few per tick (`PLANNER_MAX_PER_TICK`) and staggered, so the shared Claude subscription isn't hit all at once.

## Cover letters

Open a match and click **Draft cover letter**: pick a tone, optionally say what to emphasize, and Claude writes it
from your real background and answers (it's told never to invent anything). Edit it in place, copy it, or
download a Word file; redrafting asks before replacing your edits. Always read it before sending.

## Daily email and questions

Once an hour the scheduler queues the daily emails: each user gets a single email at their chosen local time
(Settings) with new good matches plus any questions that would sharpen their matches. Question links are signed
and need no login; every email has a one-click unsubscribe. Nothing is sent when there's nothing to say. To send
one now:

```sh
jobfinder digest send --email you@example.com --preview    # sends without marking anything as sent
```

`SCHEDULER_ENABLED=false` turns all scheduled work off.

## Files

Uploaded documents and fetched link text are stored under `STORE_DIR` (the `store` PVC in the clusters), not in
Postgres. Each document has an ID (`doc_...`) and tags. Uploads return at once as "processing"; worker-io parses
them and fetches profile links.

## Email

The app speaks plain SMTP (`SMTP_*`). In the clusters it sends to an `smtp` (postfix) Deployment: production
relays through Gmail, staging delivers only to mailpit, so the real addresses in its copy of prod's data never
receive anything.

## AI

All AI runs through the Claude Code CLI (`claude -p`) on your Claude subscription, in worker-ai only. There is no
API key, and you never need to open a pod to set it up:

1. Sign in as an admin (see [Admins](#admins)).
2. Open **AI setup** in the header.
3. On any computer with Claude Code, run `claude setup-token` and paste the token it prints.
4. Click **Save & test**. It is stored in `CLAUDE_AUTH_DIR` (the `claude-auth` PVC) and applies immediately.

Until it's connected, analysis steps show an "AI isn't connected" notice (with a link for admins).
`CLAUDE_CODE_OAUTH_TOKEN` still works as a fallback. Usage limits are shared by everyone using your instance, and
at most `AI_MAX_CONCURRENCY` Claude calls run at once across the whole cluster, however many pods call Claude:
every call first takes a permit from the Claude gate, shared through valkey (`integrations/claude/gate.py`).
Calls someone is waiting on (the api, the priority worker) get the next free permit before background work.

## Deploy

**Staging** deploys itself from every push to `dev` (GitHub Actions `images` → `deploy-staging`).

**Production** deploys itself from every push to `main` (`images` → `deploy-prod`). There is no approval
step: **a release is a dev → main PR merged with "Create a merge commit"**, which is the only merge method
main's ruleset allows. Nobody pushes to main directly. Hotfixes land on dev first, reach staging, and are then
released the same way.

Both workflows SSH to the host, where a root-owned forced command (`deploy-ssh.sh staging|prod`) checks the
commit and runs `deploy.sh`. `deploy.sh` pins the images, runs the migrate Job and only then rolls the
Deployments. HTTP smoke checks then go through Traefik (`/readyz`, `/`, `/api/auth/me`, pods Ready, no
restarts). For prod, the wrapper adds three things:

- It refuses a release whose dev side isn't running on staging yet.
- It takes a pre-deploy backup first (`pre-db-<ts>.dump`, newest 5 kept).
- If the rollout or a smoke check fails, it rolls the images back automatically. Migrations are never
  rolled back, so they stay backward compatible for one release.

Each run posts a step summary and comments "Deployed to staging" or "Released to prod" on every PR it
shipped. A prod run also posts one summary comment on the release PR: the outcome, the pinned images, the
smoke results, the backup and the shipped PRs.

The manual `deploy/k8s/deploy.sh` commands remain as a fallback. Releasing, the outcomes, rolling back,
the one-time setup, scaling, logs and troubleshooting are in [docs/k3s.md](docs/k3s.md#releasing-118).

### Backups

A CronJob in each namespace backs up nightly at 03:15 UTC (a Postgres dump plus the `store` and `claude-auth`
files, newest 14 kept) to the node-local `backups` PVC; every prod release also takes a pre-deploy backup
(`pre-*`, newest 5 kept). `deploy/k8s/backup-restore.sh <namespace>` proves a dump
restores. Copy them off the box regularly. See [docs/k3s.md, "Backups"](docs/k3s.md#backups-47).
