# k3s runbook

JobFinder is moving from docker compose to a single-node k3s cluster on the same host (#36, #38).
Production moves from compose to the `jobfinder` namespace with the cutover (#47; runbook: "Production cutover" below). Until it has run, production keeps running on compose next to k3s.

## Shape

- **Edge:** host systemd Caddy keeps 80/443 and terminates TLS (JobFinder and draw.ehlops.com).
- **Ingress:** k3s's Traefik listens on **127.0.0.1:8280** only, plain HTTP (`deploy/k3s/manifests/traefik-config.yaml`).
- **No servicelb:** klipper-lb would bind host ports 80/443.
- **SELinux:** enforcing, with the `k3s-selinux` policy.
- **firewalld:** 10.42.0.0/16 (pods) and 10.43.0.0/16 (services) are sources in the `trusted` zone. Nothing else is opened; the API server (6443) and kubelet (10250) stay unreachable from outside because the `public` zone doesn't allow them.
- **Secrets:** sealed-secrets controller in `kube-system`; `kubeseal` on the host. See `deploy/k8s/README.md` for sealing.
- **Manifests:** `deploy/k8s/` (Kustomize base + overlays). CI renders and validates them (`.github/workflows/k8s.yml`).
- **Storage:** k3s's local-path provisioner; stateful data uses the `local-path-retain` class (see Data services).

Pinned versions live at the top of `deploy/k3s/install.sh` and in `manifests/sealed-secrets.yaml`. Upgrades are PRs that bump them.

## Install / re-converge

```sh
sudo bash deploy/k3s/install.sh
```

The script is idempotent. It prints the status of the compose site, the api and the canvas before and after, and exits 2 if any of them changed.

1. Installs `container-selinux` and the `k3s-selinux` policy. No el10 build exists yet, so it uses the el9 RPM, which only needs `container-selinux` (shipped by OL10).
2. Writes `/etc/rancher/k3s/config.yaml` (`write-kubeconfig-mode: 0600`, `selinux: true`, `disable: [servicelb]`) and copies `deploy/k3s/manifests/*.yaml` into `/var/lib/rancher/k3s/server/manifests/`, which k3s applies automatically.
3. Installs k3s at the pinned version (skipped if already at it). If k3s was already running and the binary version or `config.yaml` changed, it restarts k3s so the change takes effect (`systemctl enable --now` alone is a no-op on a running unit).
4. Adds the cluster CIDRs to firewalld's `trusted` zone, runtime and permanent, **without** `firewall-cmd --reload` (a reload can drop Docker's rules for the running compose stack).
5. Copies the kubeconfig to `~opc/.kube/k3s.yaml` (0600; `~/.kube/config` is never touched) and installs `kubeseal`.
6. Waits for the node, Traefik and the sealed-secrets controller.

## Verify

```sh
kubectl get nodes                                   # Ready
kubectl -n kube-system get pods                     # all Running / Completed
sudo ss -ltnp | grep -E ':(80|443)\b'               # only caddy
sudo iptables-save | grep 'dport 8280.*DNAT'        # hostPort is a DNAT rule, -d 127.0.0.1 only (no socket in ss)
curl -s -m 4 http://$(hostname -I | cut -d' ' -f1):8280/ || echo 'not reachable off loopback: good'
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8280/          # 404: Traefik, no routes
kubeseal --fetch-cert | head -1                     # -----BEGIN CERTIFICATE-----
kubectl apply -k deploy/k8s/hello && kubectl -n jobfinder-hello rollout status deploy/hello
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8280/healthz   # 200
```

Gotchas this script already handles (keep them if you change it):

- **SELinux label.** With the installer's RPM step skipped it doesn't relabel either, and `/usr/local/bin/k3s` keeps `user_tmp_t`, which systemd can't execute ("Permission denied" in `journalctl -u k3s`). The script runs `restorecon` before starting k3s.
- **Traefik bind address.** The chart reuses `ports.web.hostIP` as the in-pod entrypoint address, so Traefik would listen only on the pod's loopback and refuse the DNAT'd traffic. `additionalArguments` re-binds the entrypoint to `:8000` inside the pod.
- **Rolling update on one node.** The new Traefik pod can't bind hostPort 8280 while the old one runs, so the strategy is `maxSurge: 0`.
- **kubectl as opc.** k3s's bundled kubectl ignores `~/.kube/config`; `/etc/profile.d/k3s-kubeconfig.sh` sets `KUBECONFIG=~/.kube/k3s.yaml:~/.kube/config` for non-root logins that don't already set `KUBECONFIG`. The k3s context comes first, so it's current; other contexts in `~/.kube/config` stay available. After the first install, log in again (or `export KUBECONFIG=~/.kube/k3s.yaml`).
- **sudo PATH.** `secure_path` omits `/usr/local/bin`; the script prepends it.

If 8280 doesn't answer, check that the Traefik pod has `hostPort 8280` with `hostIP 127.0.0.1`
(`kubectl -n kube-system get pod -l app.kubernetes.io/name=traefik -o yaml | grep -A3 hostPort`) and that the
HelmChartConfig was applied (`kubectl -n kube-system get helmchartconfig traefik -o yaml`).

## Admin commands (#49)

The api and worker images both ship the `jobfinder` CLI (`jobfinder --help`, and `--help` on every group and verb). Run it in a running pod:

```sh
kubectl -n jobfinder exec deploy/api -- jobfinder users add --email them@example.com
kubectl -n jobfinder exec deploy/api -- jobfinder users admin grant --email you@example.com
kubectl -n jobfinder exec deploy/api -- jobfinder boards list --ats greenhouse
kubectl -n jobfinder exec deploy/api -- jobfinder tasks list --status failed
kubectl -n jobfinder exec deploy/api -- jobfinder tasks retry 1234
kubectl -n jobfinder exec deploy/worker -- jobfinder ai check     # the worker has the Claude CLI
```

Exit codes: 0 ok, 1 the command failed (the message is on stderr), 2 a usage error. The pre-#49 names (`python -m jobfinder.cli add-user ...`) still work for one release, with a deprecation note.

A long verb such as `ingest run` (a full fetch of every board) can outlast an exec session; run it as a one-off Job instead. Copy `deploy/k8s/base/app/migrate-job.yaml`, rename the Job, set `command: ["jobfinder", "ingest", "run"]`, raise `activeDeadlineSeconds`, pin `image` to the tag the api runs (`kubectl -n jobfinder get deploy api -o jsonpath='{..image}'`), then `kubectl apply -f` it, follow `kubectl -n jobfinder logs -f job/<name>`, and delete the Job afterwards.

## Back up the sealed-secrets key

Every SealedSecret in git is useless without the controller's private key. Back it up **off the box, never in git**:

```sh
kubectl -n kube-system get secret -l sealedsecrets.bitnami.com/sealed-secrets-key -o yaml > sealed-secrets-key.yaml
# move it to a password manager / offline storage, then: shred -u sealed-secrets-key.yaml
```

Restore on a rebuilt cluster before the controller starts sealing with a new key:
`kubectl apply -f sealed-secrets-key.yaml && kubectl -n kube-system delete pod -l app.kubernetes.io/name=sealed-secrets`.
The controller renews its key every 30 days, so repeat the backup after renewals.

## Uninstall / roll back

Compose is never touched, so removing k3s returns the host to its previous state. **After the production cutover this no longer holds:** prod's data lives in k3s, so take a backup ("Backups") and restore it to compose first.

```sh
sudo /usr/local/bin/k3s-uninstall.sh
for c in 10.42.0.0/16 10.43.0.0/16; do
  sudo firewall-cmd --zone=trusted --remove-source=$c
  sudo firewall-cmd --permanent --zone=trusted --remove-source=$c
done
sudo dnf remove -y k3s-selinux && sudo rm -f /usr/local/bin/kubeseal ~opc/.kube/k3s.yaml \
  /etc/profile.d/k3s-kubeconfig.sh
```

Then check `curl -sI http://127.0.0.1:8180/` and `https://draw.ehlops.com/`.

## Data services (#40)

The base (`deploy/k8s/base/`) carries the stateful services; they run in staging (`jobfinder-staging`) and in prod (`jobfinder`, from the cutover, #47). Everything in the data tier carries the label `jobfinder.io/tier: data` (the namespace, postgres, valkey, the PVCs, the backup CronJob and the SealedSecrets), which `prod-cutover.sh` applies before it restores.

| Object | What | Storage |
|---|---|---|
| StatefulSet + Service `postgres` | `postgres:16.15`, same major as compose; user and database `jobfinder` | PVC `data-postgres-0`, 20Gi |
| StatefulSet + Service `valkey` | `valkey/valkey:8.1.10`, `--appendonly yes`, `noeviction`, no password; unused until #48 | PVC `data-valkey-0`, 1Gi |
| PVC `store`, `claude-auth` | successors of the compose volumes, mounted by the app in #43 | 5Gi, 100Mi |
| SealedSecret `postgres-credentials` | key `POSTGRES_PASSWORD`, random, sealed per namespace (`overlays/staging/sealed-secret-postgres.yaml`) | |
| NetworkPolicy `postgres`, `valkey` | ingress only from same-namespace pods with the client label | |

All volumes use the `local-path-retain` StorageClass (`deploy/k3s/manifests/local-path-retain.yaml`): local-path with `reclaimPolicy: Retain`, so deleting a PVC or the namespace leaves the data under `/var/lib/rancher/k3s/storage/` and a `Released` PV. Delete the PV (and that directory) by hand once you're sure. The pods run as non-root (uid 999) with all capabilities dropped.

**Backups:** the nightly CronJob `backup` and the restore drill, below ("Backups").

### Connect

From an app pod, in the same namespace, labelled for the policy:

```yaml
metadata:
  labels:
    jobfinder.io/postgres-client: "true"   # reach postgres:5432
    jobfinder.io/valkey-client: "true"     # reach valkey:6379
```

```text
DATABASE_URL=postgresql+asyncpg://jobfinder:$(POSTGRES_PASSWORD)@postgres:5432/jobfinder
redis://valkey:6379/0          # valkey; the env var name is settled in #48
```

with `POSTGRES_PASSWORD` from `secretKeyRef: {name: postgres-credentials, key: POSTGRES_PASSWORD}` (Kubernetes expands `$(VAR)` in env values when `POSTGRES_PASSWORD` is defined earlier in the same `env:` list). Pods without the label, and pods in any other namespace (even labelled), are dropped by k3s's embedded network policy controller (kube-router; don't start k3s with `--disable-network-policy`).

From the host, for a psql shell or a one-off check:

```sh
kubectl -n jobfinder-staging exec -it postgres-0 -- psql -U jobfinder -d jobfinder   # local socket, no policy involved
kubectl -n jobfinder-staging exec -it valkey-0 -- valkey-cli
kubectl -n jobfinder-staging port-forward svc/postgres 15432:5432                    # then psql -h 127.0.0.1 -p 15432
kubectl -n jobfinder-staging get secret postgres-credentials -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d
```

### Apply and verify

```sh
kubectl apply -f deploy/k3s/manifests/local-path-retain.yaml   # once; install.sh also installs it
kubectl apply -k deploy/k8s/overlays/staging
kubectl -n jobfinder-staging rollout status sts/postgres
kubectl -n jobfinder-staging rollout status sts/valkey
kubectl -n jobfinder-staging get pods,pvc,networkpolicy
```

To check the policy, run a throwaway `postgres:16.15` pod with and without `jobfinder.io/postgres-client: "true"` and try `pg_isready -h postgres -t 5`: the labelled one answers `accepting connections`, the unlabelled one `no response`.

**Rotate the password:** reseal (README, "Secrets"), apply, then set it inside the database too, since `POSTGRES_PASSWORD` only applies at `initdb`:
`kubectl -n jobfinder-staging exec -it postgres-0 -- psql -U jobfinder -d jobfinder -c '\password jobfinder'`.

## Staging (#43)

Today's app runs in `jobfinder-staging` next to the data services, on a copy of prod's data, at **https://staging.ehlops.com**. Production runs next to it in `jobfinder` ("Production" below).

```text
browser → host Caddy (TLS, staging.ehlops.com) → Traefik 127.0.0.1:8280 → web (Caddy :8080) → api:8000 (2 pods)
                                                                           worker (1, Recreate) · renderer (0) · smtp → mailpit
```

| Workload | Image | Notes |
|---|---|---|
| `web` | `jobfinder-web` | nobody (65534), `SITE_ADDRESS=:8080`, `TRUSTED_PROXIES=10.42.0.0/16` |
| `api` ×2 | `jobfinder-api` | uid 10001, `/readyz` readiness, `/healthz` liveness, `MIGRATE_ON_START=false` |
| `worker` ×1 | `jobfinder-worker` | uid 10001, `strategy: Recreate` (one loop until #48) |
| `renderer` ×0 | `jobfinder-renderer` | opt-in; patch replicas to 1 and merge `RENDERER_ENABLED=true` |
| `smtp` | `boky/postfix:5.1.0` | root (postfix needs it); staging relays to mailpit only, egress NetworkPolicy |
| `mailpit` | `axllent/mailpit:v1.31.3` | staging only; the app's `SMTP_HOST` |
| Job `migrate` | `jobfinder-api` | `alembic upgrade head`, once per deploy, before the rollout |

**Config.** ConfigMap `jobfinder-config` = `backend/.env.defaults` + `base/app` overrides (`POSTGRES_HOST=postgres`, `STORE_DIR`/`CLAUDE_AUTH_DIR` on the PVCs, `FORWARDED_ALLOW_IPS=10.42.0.0/16`, `MIGRATE_ON_START=false`) + staging's (`PUBLIC_BASE_URL`, SMTP to mailpit, and background Claude work off: `PLANNER_MAX_PER_TICK=0`, `RESOLVER_LLM_ENABLED`, `COVERAGE_ENABLED`, `DISCOVERY_ENABLED` all false, so copied users don't spend the shared subscription; ingestion of known boards still runs). Secret `jobfinder-env` (SealedSecret: a staging-only `SECRET_KEY` and prod's Claude token); `POSTGRES_PASSWORD` from `postgres-credentials`.

**Readiness.** `GET /readyz` on the api answers 200 when the database answers and `alembic_version` equals the image's alembic head, or is a revision the image doesn't know (a newer image migrated: the old pods keep serving through the rollout, so migrations must stay backward compatible for one release). It answers 503 with the reason when the database is unreachable (2s limit), unmigrated, or behind the image. Pods never serve against an unmigrated schema.

**Client IPs.** Host Caddy replaces any client-sent `X-Forwarded-For` with the real IP. Traefik trusts only `10.42.0.1` (the cni0 address its hostPort traffic arrives from; `deploy/k3s/manifests/traefik-config.yaml`), web's Caddy trusts the pod CIDR, and uvicorn takes the rightmost untrusted hop. NetworkPolicies `web` and `api` admit only Traefik → web → api, so no other pod can forge the header. The rate limiter therefore sees the browser's IP. The namespace enforces Pod Security `baseline`.

### Deploy

GitHub Actions deploys every push to dev once `images` has pushed that commit's images (`images.yml` → `deploy-staging.yml`). It SSHes to the host with a key whose forced command is the root-owned copy of `deploy/k8s/deploy-staging-ssh.sh` in `/usr/local/lib/jobfinder/`. It accepts exactly `sha-<7-40 hex>`, checks the commit is on `origin/dev` and not older than what staging runs (namespace annotation `jobfinder.io/deployed-commit`; late out-of-order runs are skipped), exports that commit's `deploy/k8s` as data, and runs the installed `deploy.sh` on it, so code from a push never runs on the host. For a manifest-only change, use **Actions → deploy-staging → Run workflow** on dev. By hand, on the host:

```sh
deploy/k8s/deploy.sh staging sha-1a2b3c4      # from a checkout of that commit
```

`deploy.sh`:

1. **Pins each image.** `sha-<commit>` if GHCR has it; else the newest of the commit's last 50 dev ancestors that has one (a frontend-only push rebuilds only web, a backend-only one only api/worker/renderer); else the image the Deployment already runs; else, on a first deploy, the current digest of `dev`. A registry error stops the deploy instead of falling back. By hand, ancestors are only used if you pass them: `DEPLOY_CANDIDATE_TAGS="sha-… sha-…"`.
2. **Migrates.** Applies everything except the Deployments, replaces Job `migrate`, waits for it. A failed migration prints the Job's log and stops; no pod has rolled.
3. **Rolls out.** Applies the Deployments (label `jobfinder.io/rollout=app`) and waits for each rollout.

Check it:

```sh
kubectl -n jobfinder-staging get pods,job
kubectl -n jobfinder-staging exec deploy/api -- python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8000/readyz').read())"
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: staging.ehlops.com' http://127.0.0.1:8280/healthz   # 200
kubectl -n jobfinder-staging logs job/migrate
```

Roll back by deploying an earlier commit's tag (`deploy.sh staging sha-<older>`); a migration that already ran stays, so only roll back across schema changes that are backward compatible.

### Copy prod's data

```sh
deploy/k8s/staging-restore.sh            # asks first; --yes to skip the prompt
```

It takes the deploy lock (refusing while a deploy runs), scales api/worker/renderer to 0, drops and recreates the staging database, streams `pg_dump -Fc` from the compose container `jobfinder-db-1` into `pg_restore` in `postgres-0` (no file on disk), and scales the app back. The schema is then at prod's revision: if staging's image is newer, the api stays unready until you redeploy (which migrates). Documents in compose's `store` volume are not copied. Staging's users are real people: all mail goes to mailpit, and nothing else may send.

### Read staging's mail

```sh
kubectl -n jobfinder-staging port-forward svc/mailpit 8025:8025   # then http://127.0.0.1:8025 (ssh -L 8025:127.0.0.1:8025 from a laptop)
```

Never expose mailpit through an Ingress: it holds password-reset links for real accounts.

### One-time setup (owner)

1. **DNS:** an A record `staging.ehlops.com` → the host's public IP.
2. **Host Caddy site** (`/etc/caddy/Caddyfile`, then `sudo systemctl reload caddy`):

   ```caddy
   # jobfinder staging (k3s, #43): Traefik on 127.0.0.1:8280, which routes by Host
   staging.ehlops.com {
   	reverse_proxy 127.0.0.1:8280
   }
   ```

3. **Traefik trusts host Caddy's X-Forwarded-For:** re-run `sudo bash deploy/k3s/install.sh` (it copies `deploy/k3s/manifests/traefik-config.yaml` into k3s's manifests dir), then check `kubectl -n kube-system get deploy traefik -o yaml | grep trustedIPs`.
4. **Deploy key** (on the host, as opc):

   ```sh
   ssh-keygen -t ed25519 -N '' -C jobfinder-staging-deploy -f /tmp/staging_deploy
   sudo install -o root -g root -m 0755 -D deploy/k8s/deploy-staging-ssh.sh /usr/local/lib/jobfinder/deploy-staging-ssh.sh
   sudo install -o root -g root -m 0755 -D deploy/k8s/deploy.sh /usr/local/lib/jobfinder/deploy.sh
   echo "restrict,command=\"/usr/local/lib/jobfinder/deploy-staging-ssh.sh\" $(cat /tmp/staging_deploy.pub)" >> ~/.ssh/authorized_keys
   gh secret set STAGING_SSH_KEY  -R EhlOps/jobfinder < /tmp/staging_deploy
   gh secret set STAGING_SSH_HOST -R EhlOps/jobfinder --body '<public IP or hostname>'
   gh secret set STAGING_SSH_USER -R EhlOps/jobfinder --body opc
   ssh-keyscan -t ed25519 <public IP or hostname> | gh secret set STAGING_SSH_KNOWN_HOSTS -R EhlOps/jobfinder
   shred -u /tmp/staging_deploy /tmp/staging_deploy.pub
   ```

   `restrict` turns off forwarding, PTYs and agent use; the forced command runs no matter what the client asks for. Test it: `ssh -i <key> opc@<host> 'rm -rf /'` must answer `refused`. Re-install both (`sudo install …` above) whenever `deploy-staging-ssh.sh` or `deploy.sh` changes on dev; the repo copies are never run by the key. Residual trust: kubectl there is cluster-admin, so a manifest on dev could still create objects outside the namespace; a namespace-scoped kubeconfig for the forced command would close that (follow-up). Until the three required secrets exist, `deploy-staging` skips with a warning.
5. **Data and first deploy:** `deploy/k8s/staging-restore.sh`, then **Run workflow** on dev (or `deploy.sh staging sha-<head of dev>`).

## Backups (#47)

CronJob `backup` (`deploy/k8s/base/data/`, in every namespace) runs at **03:15 UTC**. It waits for postgres, writes `pg_dump -Fc` to `db-<UTC timestamp>.dump` and a tar of the `store` and `claude-auth` PVCs to `files-<UTC timestamp>.tar.gz` (directories `store/` and `claude/`, the layout of `deploy/backup.sh`) on PVC **`backups`** (`local-path-retain`, 20Gi). Each file is written as `.part` and renamed when complete; the newest 14 of each are kept. It runs as uid 10001 (the app's, which owns the files) with a read-only root, carries `jobfinder.io/postgres-client` for the NetworkPolicy, and has `concurrencyPolicy: Forbid`; a run missed while the node was down starts within the hour.

**The backups are node-local** (`/var/lib/rancher/k3s/storage/<pv>_<namespace>_backups`): they survive pod, PVC and namespace deletion (Retain), not the loss of the disk. Copy them off the box until the bucket from #36 Q3 exists. `deploy/backup.sh` is retired (it only knew the compose stack).

```sh
kubectl -n jobfinder get cronjob backup                               # LAST SCHEDULE
kubectl -n jobfinder get jobs                                         # recent runs; kubectl logs job/<name>
kubectl -n jobfinder create job --from=cronjob/backup backup-now      # one now; delete the Job afterwards
```

**Restore drill** (monthly, and once after the cutover):

```sh
deploy/k8s/backup-restore.sh jobfinder                                # newest dump
deploy/k8s/backup-restore.sh jobfinder db-20261018-031500.dump        # or a given one
```

It restores the dump into a scratch database (`restore_drill_<ts>`) in that namespace's postgres from a helper pod that mounts `backups` read-only, compares every table's row count with the live database, checks that the matching files tar reads back, and drops the scratch database. The live database and the app are never touched. The live database moves on after the dump, so drill right after `create job --from=cronjob/backup`; tables written in between show as MISMATCH (exit 1). Rehearsed on staging (2026-10-17, worker paused for an exact comparison): 86 MB dump, restored in 15 s, 16 tables, 0 mismatched, 19 s in all.

**Restoring for real** (replaces the namespace's data): scale `api worker renderer` to 0; in `postgres-0`, `dropdb --force jobfinder && createdb -O jobfinder jobfinder`; run `pg_restore -h postgres -U jobfinder -d jobfinder --no-owner --no-privileges --exit-on-error --single-transaction /backups/db-<ts>.dump` from a pod like the drill's (don't stream a dump out through `kubectl exec` stdout: large outputs break off with a websocket EOF; stdin is fine); untar `files-<ts>.tar.gz` into the PVCs (`store/` into `store`, `claude/` into `claude-auth`) from a root helper pod with `--numeric-owner`; then `deploy.sh <overlay> <running tag>`.

## Production (#47)

`jobfinder` runs the prod overlay (`deploy/k8s/overlays/prod`) at **https://jobs.ehlops.com**: the same workloads as staging, with

- **Secrets** (SealedSecrets for `jobfinder`): `postgres-credentials` (random, prod only); `jobfinder-env` with compose's `SECRET_KEY` (sessions and emailed links stay valid), compose's `CLAUDE_CODE_OAUTH_TOKEN` (empty: the token saved on "AI setup" lives in the copied `claude-auth` volume and wins over the env) and `ADMIN_EMAILS` (kept out of the public repo); `smtp-relay` with `RELAYHOST_USERNAME`/`RELAYHOST_PASSWORD` from compose's root `.env`.
- **Config:** compose's `backend/.env` where it differs from `.env.defaults`: `PUBLIC_BASE_URL=https://jobs.ehlops.com`, `EMAIL_FROM`, `INGEST_MAX_DISCOVERED_BOARDS_PER_RUN=100`, `DISCOVERY_ENABLED=false`, `DISCOVERY_MAX_CANDIDATES=50`. None of staging's "background Claude work off" switches.
- **Mail:** postfix relays to `[smtp.gmail.com]:587` (`ALLOWED_SENDER_DOMAINS=gmail.com`), as compose did. No mailpit and no smtp egress policy.
- **Ingress:** host `jobs.ehlops.com` only. No namespace keeps a hostless rule (`base/app/ingress.yaml` has a placeholder host that each overlay replaces), so from the node an environment is `curl -H 'Host: <host>' http://127.0.0.1:8280/`. Only `jobfinder-hello`'s Exact `/healthz` stays hostless, and it answers `/healthz` for **any** host that has no Ingress: smoke-test `/` and `/api/auth/me`, not only `/healthz`.

### Deploying prod

By hand on the host, from a checkout of the commit to deploy (prod isn't wired to GitHub Actions; staging keeps auto-deploying every dev push):

```sh
git fetch && git checkout <commit>     # deploy.sh applies this checkout's manifests
DEPLOY_COMMIT=$(git rev-parse HEAD) \
DEPLOY_CANDIDATE_TAGS="$(git rev-list --first-parent --max-count=50 --skip=1 HEAD | cut -c1-7 | sed 's/^/sha-/' | tr '\n' ' ')" \
  deploy/k8s/deploy.sh prod sha-$(git rev-parse --short=7 HEAD)
```

`DEPLOY_CANDIDATE_TAGS` lets an image the commit didn't rebuild resolve to the newest older build; without it deploy.sh keeps what the Deployment runs. Migrations run first, so take a backup (`create job --from=cronjob/backup`) before a release that has one. Roll back by deploying an older tag (backward-compatible migrations only).

### Production cutover

Moves prod from compose (host Caddy → `127.0.0.1:8180`) to k3s (→ Traefik `127.0.0.1:8280`). `deploy/k8s/prod-cutover.sh` does the data and the deploy; Caddy and compose are changed **by hand**, below. Expect about **5 minutes** behind the maintenance page; nothing is lost, because the writers stop before the final dump.

**Rehearsal** (`deploy/k8s/prod-cutover.sh --namespace jobfinder-staging --rehearsal --tag sha-b7e058c --yes`, 2026-10-17; compose kept running, 73 MB dump, 14 tables, 15 files):

| Step | Time |
|---|---|
| preflight (compose checks, image lookups) | 1 s |
| data tier (apply and wait; already up on staging) | 3 s |
| scale down the app | 2 s |
| dump (`pg_dump -Fc` from `jobfinder-db-1`, counted before and after) | 15 s |
| restore (drop and create, `pg_restore`) | 18 s |
| row counts (14 tables, 0 mismatched) | <1 s |
| files (`store` 14 files, `claude-auth` 1, checksums match) | 6 s |
| deploy (migrate 0018 → 0021, rollout) | 15 s |
| smoke (`/healthz`, `/`, `/api/auth/me`, `/readyz`) | <1 s |
| **total** | **60 s** |

On prod the data-tier step also creates the namespace and four PVCs and initialises postgres on an empty volume (allow a minute), and the deploy pulls the images unless they are pre-pulled (go/no-go 4).

**Go / no-go** (all yes, or don't start):

1. Staging is stable on a recent prod copy; the last `deploy-staging` run is green.
2. `kubectl -n kube-system get pods` all Running; `kubectl kustomize deploy/k8s/overlays/prod` renders.
3. `df -h /` has room for another copy of the database and the dump (~1 GB).
4. Images pre-pulled: `for i in api worker renderer; do sudo k3s crictl pull ghcr.io/ehlops/jobfinder-$i:sha-0f1283d; done; sudo k3s crictl pull ghcr.io/ehlops/jobfinder-web:sha-b1eb96e` (0f1283d built no web image; deploy.sh resolves web to b1eb96e).
5. You're on the host as opc with `KUBECONFIG` set and a checkout of dev that has this runbook, and you've read the rollback.

**Window** (in `/home/opc/jobfinder`, where compose's `.env` is):

1. **Maintenance page.** `sudo cp -a /etc/caddy/Caddyfile /etc/caddy/Caddyfile.pre-cutover`, then give the `jobs.ehlops.com` site in `/etc/caddy/Caddyfile` this body:

   ```caddy
   jobs.ehlops.com {
   	respond "JobFinder is moving servers, back in a few minutes" 503
   }
   ```

   `sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && sudo systemctl reload caddy`; `curl -sI https://jobs.ehlops.com/ | head -1` → 503.
2. **Stop the writers:** `docker compose stop worker api` (web, smtp and db keep running). The script refuses to start while either runs.
3. **Run the script** from the dev checkout: `deploy/k8s/prod-cutover.sh --tag sha-0f1283d`. Type the namespace when asked. It applies the data tier, dumps (the file is kept; its path is printed), restores, compares row counts (they must match exactly), copies the files (checksums must match), runs `deploy.sh prod sha-0f1283d` (migrate 0018 → 0021, then the rollout) and the smoke checks, and prints the timings. A failure names the step; fix and re-run the same command (every step repeats safely). Once deploy.sh has finished, the script refuses to run again (namespace annotation `jobfinder.io/deployed-tag`), so a stray re-run can't overwrite live prod data with compose's. The manifests applied are the checkout's, not the tag's (0f1283d has no prod overlay); `git diff 0f1283d -- deploy/k8s backend/.env.defaults backend/kustomization.yaml` should show only #47's changes. If the run fails after the restore, the app stays scaled to 0 until a re-run or deploy.sh migrates.
4. **Smoke tests through Traefik** (the public site still shows the maintenance page):

   ```sh
   H='Host: jobs.ehlops.com'
   curl -s -o /dev/null -w '%{http_code}\n' -H "$H" http://127.0.0.1:8280/              # 200, the SPA
   curl -s -o /dev/null -w '%{http_code}\n' -H "$H" http://127.0.0.1:8280/api/auth/me   # 401: the api answers
   curl -s -H "$H" http://127.0.0.1:8280/api/auth/me                                     # the api's JSON, not whoami
   kubectl -n jobfinder get pods                                  # all Running, migrate Completed
   kubectl -n jobfinder logs deploy/worker --tail=50              # scheduler started, no tracebacks
   kubectl -n jobfinder logs deploy/smtp --tail=20                # postfix up, relayhost [smtp.gmail.com]:587
   ```

5. **Switch the upstream:** in the `jobs.ehlops.com` site, replace `respond …` with `reverse_proxy 127.0.0.1:8280`; validate, `sudo systemctl reload caddy`. Check `https://jobs.ehlops.com/` → 200, then in a browser: sign in (sessions from before the move still work), the matches feed, an admin page (Admin › Coverage), AI setup shows the saved token, and a password-reset email to yourself arrives (proves the Gmail relay; `kubectl -n jobfinder logs deploy/smtp` shows `status=sent`). Anything wrong: rollback. Then watch `kubectl -n jobfinder get pods -w` and the worker log for an hour.
6. **Stop the rest of compose:** `docker compose stop web smtp`. **Keep `db` running**: it is the local pytest Postgres (127.0.0.1:5433) and the rollback copy. Keep the compose volumes for two weeks (#53 removes them).
7. **Afterwards:** after the first 03:15 backup, `deploy/k8s/backup-restore.sh jobfinder` once (an acceptance criterion of #47), and copy that backup off the box. Remove the dump the script kept once you're happy.

**Rollback** (while compose's volumes exist; whatever was written on k3s after the switch is lost unless you dump it back first):

1. `kubectl -n jobfinder scale deploy api worker renderer --replicas=0` (two workers would run every scheduled job and email twice).
2. `sudo cp -a /etc/caddy/Caddyfile.pre-cutover /etc/caddy/Caddyfile` (upstream back to `127.0.0.1:8180`), validate, `sudo systemctl reload caddy`.
3. `docker compose start api worker web smtp`; `curl -sI https://jobs.ehlops.com/ | head -1` → 200.

## Resources

The host has 4 vCPU / 22 GB. k3s with Traefik, CoreDNS, metrics-server and sealed-secrets adds roughly 0.6–1 GB; watch `free -g` while staging (#43) runs next to compose.
