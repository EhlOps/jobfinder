# History

## 2026-10-13: seed list split across companies.d/

- `load_seed()` now reads `companies.yaml` plus every `ingest/companies.d/*.yaml` (sorted), de-duplicating on
  `(ats, slug)` (first wins, with a warning). The seed test validates every row in every file against the ATS
  registry. `--ats` choices come from `ATS_FETCHERS`; `INGEST_CONCURRENCY` default raised 5 to 10; the onboarding
  industry hint now has engineering examples.

## 2026-10-13: admin managed from the CLI

- `users.is_admin` is now the only source of admin; `ADMIN_EMAILS` is retired. New CLI commands `grant-admin` and
  `revoke-admin` (`--email`) take effect on the user's next request. Migration 0018 copies any emails still in
  `ADMIN_EMAILS` into the flag once and makes the column NOT NULL, default false.
- Backend tests pass (622). `conftest.py` pins `PUBLIC_BASE_URL=http://test`, because an https value in
  `backend/.env` makes the test session cookie `Secure` and about 78 tests fail with 401.

## 2026-10-13: production on jobs.ehlops.com, Mailpit removed

### Goal
Review whether the stack would build and deploy correctly once the `jobs.ehlops.com` domain was added, and check
that ports 80/443 are open.

### Review findings
- A `/code-review` at high effort of the last commit (61950d8) found no deploy blockers. Optional cleanups it
  raised, not yet done:
  - Build the DB URL with `sqlalchemy.engine.URL.create` in `backend/jobfinder/config.py` instead of a hand-made f-string.
  - Treat an empty `DATABASE_URL` as unset.
  - `docs/PLAN.md` still described the old `--profile dev` flow (fixed below).
  - No-op overrides in `docker-compose.dev.yml` (`SITE_ADDRESS`, `depends_on: !reset`).
- Infrastructure checks:
  - DNS: `jobs.ehlops.com` A record → 129.80.10.228 (this server's public IP), set at Namecheap; it resolved on
    1.1.1.1, 8.8.8.8 and the authoritative server on the second check. The first check found no record.
  - Host firewall: firewalld allows `http` and `https`. UDP 443 (HTTP/3) is not opened and is optional.
  - Oracle Cloud network rules: not checkable from the host. Let's Encrypt validated over TCP 443, so 443 is open from
    the internet. Port 80 is unconfirmed (the HTTP→HTTPS redirect works locally).

### Production switch
- Root `.env`: `SITE_ADDRESS=jobs.ehlops.com`, prod `COMPOSE_FILE` (`docker-compose.yml:docker-compose.prod.yml`);
  the dev line is commented out.
- `backend/.env`: `PUBLIC_BASE_URL=https://jobs.ehlops.com`, `SMTP_HOST=smtp`, `SMTP_PORT=587`,
  `EMAIL_FROM=JobFinder <samtehlers@gmail.com>`. These were still dev values (localhost URL, `mailpit` host) and
  would have broken email links, cookies and sending in production.
- Backups of both `.env` files were saved to the session scratchpad before editing.
- Caddy obtained the Let's Encrypt certificate on first start (`tls-alpn-01`, over 443).
- Verified from the host (it can't reach its own public IP, so via `--resolve ...:127.0.0.1`): `/healthz` → 200,
  `/` → 200, HTTP → 308 redirect to HTTPS.

### Mailpit removal
- Mailpit was a dev-only SMTP sink; no app code depends on it (the app speaks plain SMTP to `SMTP_HOST`).
- Removed the `mailpit` service from `docker-compose.yml` and `docker-compose.dev.yml`, plus the dev `smtp`
  profile gate and the dev `SMTP_*` overrides. **Dev mode now sends real mail** through the `smtp` postfix relay;
  `cli reset-link` prints a link without sending.
- Default `smtp_port` changed from 1025 to 587 (`backend/jobfinder/config.py`); `backend/.env.example`, README,
  `.env.example` and `docs/PLAN.md` updated. `docs/PRD.json` is a historical log and was left alone.
- Purged all Docker images and build cache (`docker compose down --rmi all --remove-orphans`, `docker image prune -af`,
  `docker builder prune -af`) and rebuilt with `docker compose up -d --build`. Volumes were kept (pgdata,
  caddy-data, caddy-config, store, claude-auth, dkim), so the certificate was reused and no data was lost.
- Verified after the rebuild: `/healthz` OK, no new certificate issuance, and a test email via
  `cli test-email --to samtehlers@gmail.com` was accepted by Gmail (`250 2.0.0 OK`). Postfix needs a few seconds
  after start before it accepts connections, so the first send right after a rebuild can fail.

### Known gaps
- The full backend test suite wasn't run: host-side DB tests use the default `change-me` password and fail
  authentication against the real database. The email unit tests (`tests/test_notify_basics.py`) pass.
- Delivery to the inbox (not spam) is unconfirmed.
- Optional: remove the leftover Mailpit lines in `docs/PLAN.md` (scaffold notes) if wanted.
