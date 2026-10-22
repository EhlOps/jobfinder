# k3s runbook

JobFinder runs on a single-node k3s cluster on the host: production in namespace `jobfinder`
(https://jobs.ehlops.com), staging in `jobfinder-staging` (https://staging.ehlops.com). This is the runbook for
both: the cluster, deploying, rolling back, scaling, backups and restores, logs and troubleshooting. Local
development uses its own k3d cluster (`jobfinder dev`, see the [README](../README.md#develop)); the manifests
themselves are described in [deploy/k8s/README.md](../deploy/k8s/README.md).

Commands below assume you are on the host as opc, in a checkout of the repo, with `KUBECONFIG` pointing at k3s
("Verify", kubectl as opc). Most take a namespace: `NS=jobfinder` for prod, `NS=jobfinder-staging` for staging.

## Shape

- **Edge:** host systemd Caddy keeps 80/443 and terminates TLS (JobFinder and draw.ehlops.com).
- **Ingress:** k3s's Traefik listens on **127.0.0.1:8280** only, plain HTTP (`deploy/k3s/manifests/traefik-config.yaml`).
- **No servicelb:** klipper-lb would bind host ports 80/443.
- **SELinux:** enforcing, with the `k3s-selinux` policy.
- **firewalld:** 10.42.0.0/16 (pods) and 10.43.0.0/16 (services) are sources in the `trusted` zone. Nothing else is opened; the API server (6443) and kubelet (10250) stay unreachable from outside because the `public` zone doesn't allow them.
- **Secrets:** sealed-secrets controller in `kube-system`; `kubeseal` on the host. See `deploy/k8s/README.md` for sealing.
- **Manifests:** `deploy/k8s/` (Kustomize base + overlays). CI renders and validates them (`.github/workflows/k8s.yml`).
- **Storage:** k3s's local-path provisioner; stateful data uses the `local-path-retain` class (see Data services).
- **Autoscaling:** KEDA in namespace `keda` scales every task worker (worker-ai, worker-io, and worker-priority with the priority lane) on its stream's backlog ("KEDA" below).

Pinned versions live at the top of `deploy/k3s/install.sh` and in the HelmCharts `manifests/sealed-secrets.yaml` (chart 2.20.0) and `manifests/keda.yaml` (chart 2.21.0, tested against Kubernetes 1.34–1.36; bump it with k3s). Upgrades are PRs that bump them.

## Install / re-converge

```sh
sudo bash deploy/k3s/install.sh
```

The script is idempotent. It prints the status of the host's other sites (the canvas among them) before and after, and exits 2 if any of them changed.

1. Installs `container-selinux` and the `k3s-selinux` policy. No el10 build exists yet, so it uses the el9 RPM, which only needs `container-selinux` (shipped by OL10).
2. Writes `/etc/rancher/k3s/config.yaml` (`write-kubeconfig-mode: 0600`, `selinux: true`, `disable: [servicelb]`) and copies `deploy/k3s/manifests/*.yaml` into `/var/lib/rancher/k3s/server/manifests/`, which k3s applies automatically.
3. Installs k3s at the pinned version (skipped if already at it). If k3s was already running and the binary version or `config.yaml` changed, it restarts k3s so the change takes effect (`systemctl enable --now` alone is a no-op on a running unit).
4. Adds the cluster CIDRs to firewalld's `trusted` zone, runtime and permanent, **without** `firewall-cmd --reload` (a reload can drop Docker's rules, which local k3d clusters rely on).
5. Copies the kubeconfig to `~opc/.kube/k3s.yaml` (0600; `~/.kube/config` is never touched) and installs `kubeseal`.
6. Waits for the node, Traefik, the sealed-secrets controller and KEDA (its operator, its metrics server and the `external.metrics.k8s.io` APIService).

## Verify

```sh
kubectl get nodes                                   # Ready
kubectl -n kube-system get pods                     # all Running / Completed
sudo ss -ltnp | grep -E ':(80|443)\b'               # only caddy
sudo iptables-save | grep 'dport 8280.*DNAT'        # hostPort is a DNAT rule, -d 127.0.0.1 only (no socket in ss)
curl -s -m 4 http://$(hostname -I | cut -d' ' -f1):8280/ || echo 'not reachable off loopback: good'
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8280/          # 404: Traefik, no route for a bare IP
kubeseal --fetch-cert | head -1                     # -----BEGIN CERTIFICATE-----
kubectl -n keda get pods                            # keda-operator, -metrics-apiserver, -admission-webhooks Running
kubectl get apiservice v1beta1.external.metrics.k8s.io   # AVAILABLE True
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

The api and worker images both ship the `jobfinder` CLI (`jobfinder --help`, and `--help` on every group and verb; the README has the full list). Run it in a running pod:

```sh
kubectl -n jobfinder exec deploy/api -- jobfinder users add --email them@example.com
kubectl -n jobfinder exec deploy/api -- jobfinder users admin grant --email you@example.com
kubectl -n jobfinder exec deploy/api -- jobfinder boards list --ats greenhouse
kubectl -n jobfinder exec deploy/api -- jobfinder tasks list --status failed
kubectl -n jobfinder exec deploy/api -- jobfinder tasks retry 1234
kubectl -n jobfinder exec deploy/worker-ai -- jobfinder ai check     # worker-ai has the Claude CLI
```

Exit codes: 0 ok, 1 the command failed (the message is on stderr), 2 a usage error.

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

## Uninstall

**This deletes production and every backup on the box.** `k3s-uninstall.sh` removes `/var/lib/rancher/k3s`, which holds the local-path volumes: prod's and staging's Postgres, the store, and the `backups` PVC. First take a backup and copy it off the box ("Backups", "Off-box copy"), along with the sealed-secrets key.

```sh
sudo /usr/local/bin/k3s-uninstall.sh
for c in 10.42.0.0/16 10.43.0.0/16; do
  sudo firewall-cmd --zone=trusted --remove-source=$c
  sudo firewall-cmd --permanent --zone=trusted --remove-source=$c
done
sudo dnf remove -y k3s-selinux && sudo rm -f /usr/local/bin/kubeseal ~opc/.kube/k3s.yaml \
  /etc/profile.d/k3s-kubeconfig.sh
```

Then check `https://draw.ehlops.com/` still answers.

## Data services (#40)

The base (`deploy/k8s/base/`) carries the stateful services; they run in both namespaces. Everything in the data tier carries the label `jobfinder.io/tier: data` (the namespace, postgres, valkey, the PVCs, the backup CronJob and the SealedSecrets), so it can be applied on its own with `-l jobfinder.io/tier=data`.

| Object | What | Storage |
|---|---|---|
| StatefulSet + Service `postgres` | `postgres:16.15`; user and database `jobfinder` | PVC `data-postgres-0`, 20Gi |
| StatefulSet + Service `valkey` | `valkey/valkey:8.1.10`, `--appendonly yes`, `noeviction`, no password; delivers task ids ("Task platform") | PVC `data-valkey-0`, 1Gi |
| PVC `store`, `claude-auth` | uploaded documents and fetched link text; the saved Claude token | 5Gi, 100Mi |
| SealedSecret `postgres-credentials` | key `POSTGRES_PASSWORD`, random, sealed per namespace (`overlays/<env>/sealed-secret-postgres.yaml`) | |
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
VALKEY_URL=redis://valkey:6379/0   # .env.defaults already has it
```

with `POSTGRES_PASSWORD` from `secretKeyRef: {name: postgres-credentials, key: POSTGRES_PASSWORD}` (Kubernetes expands `$(VAR)` in env values when `POSTGRES_PASSWORD` is defined earlier in the same `env:` list). Pods without the label, and pods in any other namespace (even labelled), are dropped by k3s's embedded network policy controller (kube-router; don't start k3s with `--disable-network-policy`).

From the host, for a psql shell or a one-off check:

```sh
kubectl -n $NS exec -it postgres-0 -- psql -U jobfinder -d jobfinder   # local socket, no policy involved
kubectl -n $NS exec -it valkey-0 -- valkey-cli
kubectl -n $NS port-forward svc/postgres 15432:5432                    # then psql -h 127.0.0.1 -p 15432
kubectl -n $NS get secret postgres-credentials -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d
```

### Apply and verify

```sh
kubectl apply -f deploy/k3s/manifests/local-path-retain.yaml   # once; install.sh also installs it
kubectl apply -k deploy/k8s/overlays/staging -l jobfinder.io/tier=data
kubectl -n jobfinder-staging rollout status sts/postgres
kubectl -n jobfinder-staging rollout status sts/valkey
kubectl -n jobfinder-staging get pods,pvc,networkpolicy
```

To check the policy, run a throwaway `postgres:16.15` pod with and without `jobfinder.io/postgres-client: "true"` and try `pg_isready -h postgres -t 5`: the labelled one answers `accepting connections`, the unlabelled one `no response`.

**Rotate the password:** reseal (`deploy/k8s/README.md`, "Secrets"), apply, then set it inside the database too, since `POSTGRES_PASSWORD` only applies at `initdb`:
`kubectl -n $NS exec -it postgres-0 -- psql -U jobfinder -d jobfinder -c '\password jobfinder'`, then restart the app (`kubectl -n $NS rollout restart deploy/api deploy/scheduler deploy/worker-ai deploy/worker-io`).

## Workloads (#43, #48)

Both namespaces run the same workloads from the same base; the overlays differ only in config, secrets and mail.

```text
browser → host Caddy (TLS) → Traefik 127.0.0.1:8280 → web (Caddy :8080) → api:8000 (2 pods)
                                                      scheduler (1) · worker-ai (1–3) · worker-io (1–8) · renderer (0) · smtp
```

| Workload | Image | Notes |
|---|---|---|
| `web` | `jobfinder-web` | nobody (65534), `SITE_ADDRESS=:8080`, `TRUSTED_PROXIES=10.42.0.0/16` |
| `api` ×2 | `jobfinder-api` | uid 10001, `/readyz` readiness, `/healthz` liveness, `MIGRATE_ON_START=false` |
| `scheduler` ×1 | `jobfinder-api` | cron, relay, reaper ("Task platform"); `strategy: Recreate`, heartbeat-file liveness |
| `worker-ai` ×1–3 | `jobfinder-worker` | stream `jobfinder:ai`, `WORKER_AI_CONCURRENCY`; Claude (store and claude-auth volumes); replicas by KEDA (staging 1; "KEDA") |
| `worker-io` ×1–8 | `jobfinder-worker` | stream `jobfinder:io`, `WORKER_IO_CONCURRENCY`; store volume (it parses uploads and fetches links, #51); replicas by KEDA (staging 1–2; "KEDA") |
| `renderer` ×0 | `jobfinder-renderer` | opt-in, stream `jobfinder:render`; patch replicas to 1 and merge `RENDERER_ENABLED=true` |
| `smtp` | `boky/postfix:5.1.0` | root (postfix needs it); prod relays to Gmail, staging to mailpit only (egress NetworkPolicy) |
| `mailpit` | `axllent/mailpit:v1.31.3` | staging only; the app's `SMTP_HOST` |
| Job `migrate` | `jobfinder-api` | `alembic upgrade head`, once per deploy, before the rollout |
| CronJob `backup` | `postgres:16.15` | nightly at 03:15 UTC ("Backups") |

**Config.** ConfigMap `jobfinder-config` = `backend/.env.defaults` + `base/app` overrides (`POSTGRES_HOST=postgres`, `STORE_DIR`/`CLAUDE_AUTH_DIR` on the PVCs, `FORWARDED_ALLOW_IPS=10.42.0.0/16`, `MIGRATE_ON_START=false`) + the overlay's. Its name carries a hash of its contents, so a changed value rolls the pods that read it. Secret `jobfinder-env` (a SealedSecret per namespace: `SECRET_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`); `POSTGRES_PASSWORD` from `postgres-credentials`.

**Readiness.** `GET /readyz` on the api answers 200 when the database answers and `alembic_version` equals the image's alembic head, or is a revision the image doesn't know (a newer image migrated: the old pods keep serving through the rollout, so migrations must stay backward compatible for one release). It answers 503 with the reason when the database is unreachable (2s limit), unmigrated, or behind the image. Pods never serve against an unmigrated schema.

**Client IPs.** Host Caddy replaces any client-sent `X-Forwarded-For` with the real IP. Traefik trusts only `10.42.0.1` (the cni0 address its hostPort traffic arrives from; `deploy/k3s/manifests/traefik-config.yaml`), web's Caddy trusts the pod CIDR, and uvicorn takes the rightmost untrusted hop. NetworkPolicies `web` and `api` admit only Traefik → web → api, so no other pod can forge the header. The rate limiter therefore sees the browser's IP. The namespaces enforce Pod Security `baseline`.

**Ingress.** Each overlay sets its own host; no namespace keeps a hostless rule (`base/app/ingress.yaml` has a placeholder host that each overlay replaces), so from the node an environment is `curl -H 'Host: <host>' http://127.0.0.1:8280/`. Only `jobfinder-hello`'s Exact `/healthz` stays hostless, and it answers `/healthz` for **any** host that has no Ingress: smoke-test `/` and `/api/auth/me`, not only `/healthz`.

## Production

`jobfinder` runs the prod overlay (`deploy/k8s/overlays/prod`) at **https://jobs.ehlops.com**, with

- **Secrets** (SealedSecrets for `jobfinder`): `postgres-credentials` (random, prod only); `jobfinder-env` with `SECRET_KEY` (random, prod only; the pre-k3s key was rotated out on 2026-10-21) and an empty `CLAUDE_CODE_OAUTH_TOKEN` (the token saved on "AI setup" lives in the `claude-auth` PVC and wins over the env); `smtp-relay` with `RELAYHOST_USERNAME`/`RELAYHOST_PASSWORD`.
- **Config:** where prod differs from `.env.defaults`: `PUBLIC_BASE_URL=https://jobs.ehlops.com`, `EMAIL_FROM`, `INGEST_MAX_DISCOVERED_BOARDS_PER_RUN=100`, `DISCOVERY_ENABLED=false`, `DISCOVERY_MAX_CANDIDATES=50`. None of staging's "background Claude work off" switches.
- **Mail:** postfix relays to `[smtp.gmail.com]:587` (`ALLOWED_SENDER_DOMAINS=gmail.com`). No mailpit and no smtp egress policy.

### Releasing (#118)

**A release is a dev → main PR, merged with "Create a merge commit".** Every push to main deploys prod; there is no approval step, so the merge is the decision. The main ruleset allows only merge commits, which matters for two reasons. A squash would fork main away from dev, and every later release PR would then conflict. A merge commit's second parent (`<commit>^2`) is exactly the dev commit that staging ran, and the staging gate checks that commit.

Before merging, read the release PR's migrations with "Rollback" in mind: the automatic rollback only reverts images, never the schema.

What the merge sets off, in order:

1. **`images.yml`** builds the images the push changed and tags them `sha-<7>`. Its `deploy-prod` job runs only on a push to `refs/heads/main`, and only once every build has succeeded.
2. **`deploy-prod.yml`** runs in environment `production`, whose deployment-branch policy allows only `main`, so only main runs can read the prod key. It SSHes to the host and sends only `sha-<7>`.
3. **On the host, the key's forced command** is the root-owned `/usr/local/lib/jobfinder/deploy-ssh.sh prod`. The env comes from `authorized_keys`, never from the client. The wrapper then works through these steps:
   1. **Checks.** It accepts exactly `sha-<7-40 lowercase hex>`; anything else is refused with exit 2. The commit must be on `origin/main`. Prod never moves backwards: a commit that prod's `jobfinder.io/deployed-commit` already contains is skipped with exit 0, which covers late runs and re-runs. The wrapper holds a per-env lock, so prod and staging deploys can run at the same time, but never two of the same env.
   2. **Staging gate.** The release's dev side (`<commit>^2`, or the commit itself when it has no second parent) must equal, or be an ancestor of, staging's `jobfinder.io/deployed-commit` (on namespace `jobfinder-staging`). If it isn't, the release is refused.
   3. **Prior state.** It records the image of every app Deployment (`jobfinder.io/rollout=app`), plus the namespace's `jobfinder.io/deployed-tag` and `jobfinder.io/deployed-commit`, before anything changes.
   4. **Pre-deploy backup.** It creates Job `backup-pre-<sha7>` from `cronjob/backup` with `BACKUP_PREFIX=pre-`, which writes `pre-db-<ts>.dump` and `pre-files-<ts>.tar.gz` ("Backups"), and waits for it. **A failed backup stops the deploy** before anything changes.
   5. **`deploy.sh prod`** pins the images, runs the migrate Job and rolls out the Deployments (see "Deploying by hand" for how it pins). Its candidate tags are the dev side's first-parent history, then main's. An image the merge didn't rebuild therefore comes from the newest dev build of the same tree.
   6. **HTTP smoke checks.** They go through Traefik on `127.0.0.1:8280` with `Host: jobs.ehlops.com`, the same path users take, and retry for about 2 minutes. They pass when all of these hold:
      - `/readyz` returns 200
      - `/` returns 200 (the SPA)
      - `/api/auth/me` returns 401
      - every app pod is Ready (`pods-ready`)
      - no app pod has restarted since the rollout (`no-restarts`)
   7. **Automatic image rollback.** If the rollout or a smoke check fails, the wrapper sets every Deployment back to its recorded image, waits for the rollouts, restores the two namespace annotations and runs the smoke checks again. It **exits 1 either way**, so the run is red and GitHub emails you even when the rollback worked. Migrations are never rolled back (see "Rollback").
   8. **Summary line.** The last line of every run, refused and skipped runs included, is `deploy-summary: <JSON>`. It carries `env`, `outcome`, `tag`, `commit`, `dev_commit`, `previous_tag`/`previous_commit` (or `unknown`), the pinned `images`, the `smoke` results, the `backup` file, `started_at`/`finished_at` and a one-line `message`.
4. **The report.** `.github/scripts/deploy-report.sh` runs after the SSH step even when that step failed, and reads the summary line:
   - **Step summary.** It writes a table to the run's step summary.
   - **Shipped PRs.** It finds them from the `(#N)` subjects in `git log --first-parent <previous dev side>..<dev_commit>`. On `deployed`, it comments on each one with `Released to **prod** in #<release PR> · <tag> · <UTC time> · [run](…)`.
   - **Release PR.** It always posts one summary comment on the release PR, whatever the outcome. The comment gives the outcome, the tag, the pinned images, the smoke table, the backup file, the previous tag, the shipped PRs with their titles, the time and a link to the run.

   With `DRY_RUN=1`, the script prints the comments instead of posting them.

Exit status: 0 for `deployed` and `skipped`, 2 for a malformed request, 1 for everything else.

**Run log:** every run also writes its whole output to `~/.local/state/jobfinder/deploy-<env>-<UTC start>-<pid>.log` on the host (mode 0600, newest 50 kept; the last line is the summary). A run ignores SIGHUP/SIGPIPE, so a cancelled or disconnected Actions run still finishes its deploy, checks or rollback: read the outcome there. After a rollback the summary also carries `rollback_smoke`, the re-check's results.

**Outcomes** (`outcome` in the summary line, shown in the run summary and the release PR comment):

| Outcome | What happened | What to do |
|---|---|---|
| `deployed` | Backup, migration, rollout and smoke checks all passed; the namespace records the new tag and commit. | Nothing. |
| `skipped` | Prod already runs this commit or one that contains it (a late run or a re-run). | Nothing. |
| `refused` | Bad tag, a commit not on `origin/main`, or **the staging gate**: the dev side isn't on staging yet. Nothing changed. | Gate: let staging's deploy of that dev commit finish (or fix it), then **Actions → deploy-prod → Run workflow** on main. A squash or a commit made straight on main can never pass the gate (see "Hotfixes"). |
| `backup_failed` | The pre-deploy backup Job failed or timed out. Nothing was deployed. | `kubectl -n jobfinder logs job/backup-pre-<sha7>`; the usual causes are postgres being unreachable or the `backups` PVC being full ("Backups"). Fix it, `kubectl -n jobfinder delete job backup-pre-<sha7>`, then **Run workflow** on main. |
| `migrate_failed` | The migrate Job failed. `deploy.sh` stops before any Deployment changes, so the old pods keep serving and there is nothing to roll back. | `kubectl -n jobfinder logs job/migrate`. Fix forward on dev, let staging take it, and release again. |
| `failed` | Any other error, such as git, the registry, the lock or kubectl. | Read the run log; `message` says where it stopped. Fix the cause and **Run workflow** on main. |
| `rolled_back` | The rollout or a smoke check failed. The images went back to `previous_tag`, and the re-check passed. Prod is healthy, but it runs the old images on the **new** schema if the release migrated. | Find the cause in the smoke table and the run log, fix it on dev, and release again. |
| `rollback_failed` | The rollback's re-check failed too. **Prod may be down: act now.** | The run log prints the recorded prior images. Follow "Rollback" by hand. If the old code can't run on the new schema, restore from the run's `pre-db-<ts>.dump`. |

**Hotfixes** go through dev like everything else: land the fix on dev, let staging deploy it, then open and merge a dev → main release PR. A commit pushed straight to main is refused by the ruleset. A squash-merged release has no second parent and was never on staging, so the gate refuses it.

**Manifest-only changes** build no images, so no deploy runs. Use **Actions → deploy-prod → Run workflow** on main.

### One-time setup for prod autodeploy (owner, #118)

On the host, as opc, from a checkout of dev that has #118:

1. **Install both scripts**, root-owned, outside the repo, so a push can't change what the keys run:

   ```sh
   sudo install -o root -g root -m 0755 -D deploy/k8s/deploy-ssh.sh /usr/local/lib/jobfinder/deploy-ssh.sh
   sudo install -o root -g root -m 0755 -D deploy/k8s/deploy.sh     /usr/local/lib/jobfinder/deploy.sh
   ```

   Re-install both whenever `deploy-ssh.sh` or `deploy.sh` changes. The repo copies are never run by a key.
2. **Switch the staging key** to the new wrapper. In `~/.ssh/authorized_keys`, change the staging key's `command=` to `restrict,command="/usr/local/lib/jobfinder/deploy-ssh.sh staging"`. The wrapper was renamed from `deploy-staging-ssh.sh` (#118), so remove the old installed copy: `sudo rm /usr/local/lib/jobfinder/deploy-staging-ssh.sh`.
3. **Prod deploy key** (ed25519, no passphrase) and its forced command:

   ```sh
   ssh-keygen -t ed25519 -N '' -C jobfinder-prod-deploy -f /tmp/prod_deploy
   echo "restrict,command=\"/usr/local/lib/jobfinder/deploy-ssh.sh prod\" $(cat /tmp/prod_deploy.pub)" >> ~/.ssh/authorized_keys
   ssh -i /tmp/prod_deploy opc@<public IP or hostname> 'rm -rf /'      # must answer "refused"
   ```

4. **Environment `production`**, limited to the `main` branch, with no reviewers:

   ```sh
   gh api -X PUT repos/EhlOps/jobfinder/environments/production --input - <<'EOF'
   {"deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
   EOF
   gh api -X POST repos/EhlOps/jobfinder/environments/production/deployment-branch-policies -f name=main -f type=branch
   ```

5. **Its secrets** (environment secrets, so only main runs can read them):

   ```sh
   gh secret set PROD_SSH_KEY  --env production -R EhlOps/jobfinder < /tmp/prod_deploy
   gh secret set PROD_SSH_HOST --env production -R EhlOps/jobfinder --body '<public IP or hostname>'
   gh secret set PROD_SSH_USER --env production -R EhlOps/jobfinder --body opc
   ssh-keyscan -t ed25519 <public IP or hostname> | gh secret set PROD_SSH_KNOWN_HOSTS --env production -R EhlOps/jobfinder
   shred -u /tmp/prod_deploy /tmp/prod_deploy.pub
   ```

6. **Bootstrap main** before the rulesets exist, because afterwards nobody can push to main directly. Fast-forward main to the commit prod runs now, so that main matches prod:

   ```sh
   kubectl get ns jobfinder -o jsonpath='{.metadata.annotations.jobfinder\.io/deployed-commit}{"\n"}'
   git push origin <that commit>:main      # must be a fast-forward of main
   ```

7. **The two main rulesets.** "main: owner only" lets only repository admins (the owner) update or delete main, and forbids force pushes. "main: merge commits" requires a PR merged with a merge commit, needs 0 approvals and has no bypass:

   ```sh
   gh api -X POST repos/EhlOps/jobfinder/rulesets --input - <<'EOF'
   {"name": "main: owner only", "target": "branch", "enforcement": "active",
    "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
    "rules": [{"type": "update"}, {"type": "deletion"}, {"type": "non_fast_forward"}],
    "bypass_actors": [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}]}
   EOF
   gh api -X POST repos/EhlOps/jobfinder/rulesets --input - <<'EOF'
   {"name": "main: merge commits", "target": "branch", "enforcement": "active",
    "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
    "rules": [{"type": "pull_request", "parameters": {
      "required_approving_review_count": 0, "dismiss_stale_reviews_on_push": false,
      "require_code_owner_review": false, "require_last_push_approval": false,
      "required_review_thread_resolution": false, "allowed_merge_methods": ["merge"]}}],
    "bypass_actors": []}
   EOF
   gh api repos/EhlOps/jobfinder/rulesets --jq '.[] | [.id, .name, .enforcement] | @tsv'
   ```

   In `bypass_actors`, `actor_id` 5 is the built-in admin role. The repo keeps squash merges allowed, because dev's PRs use them; the ruleset narrows only main.
8. **First release:** open a dev → main PR, merge it with **Create a merge commit**, and watch **Actions → deploy-prod**.

Until `PROD_SSH_KEY`, `PROD_SSH_HOST` and `PROD_SSH_USER` all exist, `deploy-prod` skips with a warning, as staging's does.

### Deploying by hand (fallback)

Use this when GitHub Actions is unavailable, or to deploy something a release can't, such as a rollback. Run it on the host, from a checkout of the commit to deploy. The checkout's `deploy/k8s/deploy.sh` applies the checkout's manifests. The installed copy in `/usr/local/lib/jobfinder/` has no overlays next to it, so it needs `DEPLOY_K8S_DIR=<checkout>/deploy/k8s`; the simplest way is to run it from the checkout.

First note what runs now (you need it to roll back), then take a pre-deploy backup, because migrations run before the rollout:

```sh
kubectl get ns jobfinder -o jsonpath='{.metadata.annotations.jobfinder\.io/deployed-tag}{"\n"}'   # the running tag
kubectl -n jobfinder create job --from=cronjob/backup backup-pre-<sha7> --dry-run=client -o yaml \
  | kubectl set env --local -f - BACKUP_PREFIX=pre- -o yaml | kubectl apply -f -
kubectl -n jobfinder wait --for=condition=complete job/backup-pre-<sha7> --timeout=15m

git fetch && git checkout <commit>     # deploy.sh applies this checkout's manifests
DEV=$(git rev-parse --verify --quiet 'HEAD^2' || git rev-parse HEAD)   # a release's dev side
DEPLOY_COMMIT=$(git rev-parse HEAD) \
DEPLOY_CANDIDATE_TAGS="$( { git rev-list --first-parent --max-count=50 "$DEV"; git rev-list --first-parent --max-count=50 --skip=1 HEAD; } | cut -c1-7 | sed 's/^/sha-/' | tr '\n' ' ')" \
  deploy/k8s/deploy.sh prod sha-$(git rev-parse --short=7 HEAD)
```

Always pass `DEPLOY_COMMIT`. Without it, the namespace records `unknown`, and the wrapper's never-backwards check, the report's shipped-PR list and the next rollback all have nothing to compare against. A by-hand deploy doesn't run the smoke checks or the rollback, and doesn't post a report.

`deploy.sh` pins every image by an immutable tag or digest, never a moving one:

1. **Pins each image.** `images.yml` only rebuilds what a commit changed (a frontend-only commit builds only web; a backend-only one only api, worker and renderer). Per image, it takes the first that exists in GHCR of: `sha-<commit>`; the tags in `DEPLOY_CANDIDATE_TAGS`, newest first (the line above lists the dev side's first-parent ancestors, then main's); otherwise **the image the Deployment already runs is kept**; on a first deploy, the current digest of `main`. So without `DEPLOY_CANDIDATE_TAGS`, an image the commit didn't rebuild stays at whatever was deployed, even if a newer ancestor built one. A registry error stops the deploy instead of falling back. With `DEPLOY_PINS_OUT=<file>` it also writes one `<image> <tag-or-digest>` line per image (the wrapper uses this for the summary's `images`).
2. **Migrates.** Applies everything except the Deployments, replaces Job `migrate`, waits for it. A failed migration prints the Job's log and stops; no pod has rolled.
3. **Rolls out.** Applies the Deployments and the workers' ScaledObjects (label `jobfinder.io/rollout=app`), waits for each rollout, and lifts every KEDA pause a script left marked ("KEDA").
4. **Records** the tag and `DEPLOY_COMMIT` as namespace annotations `jobfinder.io/deployed-tag` and `jobfinder.io/deployed-commit`.

Check it (these are the wrapper's smoke checks):

```sh
H='Host: jobs.ehlops.com'
kubectl -n jobfinder get pods,job                                               # all Running, migrate Completed
curl -s -o /dev/null -w '%{http_code}\n' -H "$H" http://127.0.0.1:8280/readyz        # 200
curl -s -o /dev/null -w '%{http_code}\n' -H "$H" http://127.0.0.1:8280/              # 200, the SPA
curl -s -o /dev/null -w '%{http_code}\n' -H "$H" http://127.0.0.1:8280/api/auth/me   # 401: the api answers
kubectl -n jobfinder logs job/migrate
kubectl -n jobfinder logs deploy/scheduler --tail=50                            # cron jobs added, no tracebacks
```

### Rollback

**Automatic (prod, #118).** When a release's rollout or smoke check fails, the wrapper rolls the images back by itself ("Releasing", the automatic image rollback), and the outcome is `rolled_back` or `rollback_failed`. It doesn't touch the database. The old images keep running on the new schema, which is safe only under the backward-compatibility rule below. What follows is the by-hand path, for `rollback_failed` or for a release that passed its checks and turned out bad later.

**Deploy the older tag.** Check out the commit that ran before (the previous tag in the release PR's comment, or `git log --first-parent main`), and deploy it by hand ("Deploying by hand") with its own candidate tags, so every image goes back, including the ones that commit didn't rebuild:

```sh
git checkout <older commit>
DEV=$(git rev-parse --verify --quiet 'HEAD^2' || git rev-parse HEAD)
DEPLOY_COMMIT=$(git rev-parse HEAD) \
DEPLOY_CANDIDATE_TAGS="$( { git rev-list --first-parent --max-count=50 "$DEV"; git rev-list --first-parent --max-count=50 --skip=1 HEAD; } | cut -c1-7 | sed 's/^/sha-/' | tr '\n' ' ')" \
  deploy/k8s/deploy.sh prod sha-$(git rev-parse --short=7 HEAD)
```

Without `DEPLOY_CANDIDATE_TAGS`, an image the older commit didn't build keeps running at the newer version. Re-running **Actions → deploy-prod** on an older commit doesn't work, because the wrapper skips a commit older than the one prod runs. After a by-hand rollback, the next release moves prod forward as usual.

That works as long as no migration ran in between.

**Across a migration.** Migrations are never rolled back, and the older image's migrate Job fails (`alembic upgrade head` can't locate the newer revision in the database), so `deploy.sh` stops before anything rolls: nothing breaks, but nothing rolls back either. The older code can still run on the newer schema, because every migration stays backward compatible for one release (add columns and tables; drop or rename only in a later release, once no deployed code reads them) and `/readyz` accepts a revision the image doesn't know. So roll the images back by hand, to the tags the older commit ran (`images` in the release PR's comment for that deploy, `deploy.sh`'s log of it, or `kubectl -n jobfinder rollout history deploy/api`):

```sh
OLD=sha-<older>      # per image: web is often an older tag than api/worker
kubectl -n jobfinder set image deploy/api api=ghcr.io/ehlops/jobfinder-api:$OLD
kubectl -n jobfinder set image deploy/scheduler scheduler=ghcr.io/ehlops/jobfinder-api:$OLD
kubectl -n jobfinder set image deploy/worker-ai worker-ai=ghcr.io/ehlops/jobfinder-worker:$OLD
kubectl -n jobfinder set image deploy/worker-io worker-io=ghcr.io/ehlops/jobfinder-worker:$OLD
kubectl -n jobfinder set image deploy/renderer renderer=ghcr.io/ehlops/jobfinder-renderer:$OLD
kubectl -n jobfinder rollout status deploy/api   # and the others
```

(`kubectl -n jobfinder rollout undo deploy/<name>` does the same per Deployment when the previous ReplicaSet is the one you want.) The next `deploy.sh` brings everything back in line with git; fix forward from there. Review a release's migrations with this in mind before merging it: a release that breaks the rule can't be rolled back without a restore.

**Restore from a backup** only as a last resort, for a destructive migration or bad data written by the new release. Follow "Restoring for real" under Backups, using the release's `pre-db-<ts>.dump` (the `backup` in its summary and in the release PR comment). Everything written since that dump is lost.

**Staging** isn't rolled back automatically. Roll it back by hand on the host the same way (`deploy.sh staging ...`), with the same `DEPLOY_COMMIT` and `DEPLOY_CANDIDATE_TAGS` lines, but with `DEV=$(git rev-parse HEAD)`. Re-running **Actions → deploy-staging** on an older commit doesn't work, because the SSH wrapper skips a commit older than the one staging runs. Pass `DEPLOY_COMMIT`, or the namespace records `unknown` and the wrapper's out-of-order check has nothing to compare the next push against.

## Staging (#43)

`jobfinder-staging` runs the staging overlay on a copy of prod's data, at **https://staging.ehlops.com**. Staging's config turns background Claude work off (`PLANNER_MAX_PER_TICK=0`; `RESOLVER_LLM_ENABLED`, `COVERAGE_ENABLED`, `DISCOVERY_ENABLED` all false), so copied users don't spend the shared subscription; ingestion of known boards still runs. Its `jobfinder-env` has a staging-only `SECRET_KEY` and prod's Claude token. Staging's users are real people: all mail goes to mailpit, and nothing else may send.

### Deploy

GitHub Actions deploys every push to dev once `images` has pushed that commit's images (`images.yml` → `deploy-staging.yml`). It SSHes to the host with a key whose forced command is the root-owned `/usr/local/lib/jobfinder/deploy-ssh.sh staging`, the same wrapper prod uses ("Releasing"). The wrapper accepts exactly `sha-<7-40 hex>`, checks that the commit is on `origin/dev` and not older than what staging runs (namespace annotation `jobfinder.io/deployed-commit`; late, out-of-order runs are skipped), exports that commit's `deploy/k8s` as data, and runs the installed `deploy.sh` on it with the commit's ancestors as `DEPLOY_CANDIDATE_TAGS`, so code from a push never runs on the host. It then runs the same HTTP smoke checks as prod, with `Host: staging.ehlops.com`. A failed check fails the run, but staging has **no pre-deploy backup and no automatic rollback**. The run prints the same `deploy-summary:` line, and `deploy-report.sh` writes the step summary. On `deployed`, it comments `Deployed to **staging** · <tag> · <UTC time> · [run](…)` on each PR that the push shipped. For a manifest-only change, use **Actions → deploy-staging → Run workflow** on dev. By hand, on the host:

```sh
deploy/k8s/deploy.sh staging sha-1a2b3c4      # from a checkout of that commit
```

`deploy.sh` works as described under "Deploying by hand"; on a first deploy it falls back to the digest of `dev`. Check it:

```sh
kubectl -n jobfinder-staging get pods,job
kubectl -n jobfinder-staging exec deploy/api -- python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8000/readyz').read())"
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: staging.ehlops.com' http://127.0.0.1:8280/healthz   # 200
kubectl -n jobfinder-staging logs job/migrate
```

After a change to `deploy.sh` or `deploy-ssh.sh`, reinstall the root-owned copies ("One-time setup for prod autodeploy", step 1), or deploys from GitHub Actions keep running the old ones.

### Copy prod's data

```sh
deploy/k8s/staging-restore.sh            # asks first; --yes to skip the prompt
```

It takes the deploy lock (refusing while a deploy runs), pauses every worker's ScaledObject and scales staging's app (api, renderer, scheduler and the workers) to 0, drops and recreates the staging database, streams `pg_dump -Fc` from prod's `postgres-0` (namespace `jobfinder`; prod is only read) into `pg_restore` in staging's `postgres-0` (no file on disk), empties staging's task streams (their ids belong to the dropped database; the relay re-sends the copy's queued rows) and scales the app back. The schema is then at prod's revision: if staging's image is newer, the api stays unready until you redeploy (which migrates). Documents in prod's `store` PVC are not copied, so files uploaded on prod are missing on staging. It refuses to restore into `jobfinder`.

### Read staging's mail

```sh
kubectl -n jobfinder-staging port-forward svc/mailpit 8025:8025   # then http://127.0.0.1:8025 (ssh -L 8025:127.0.0.1:8025 from a laptop)
```

Never expose mailpit through an Ingress: it holds password-reset links for real accounts.

### One-time setup (owner)

1. **DNS:** an A record `staging.ehlops.com` → the host's public IP.
2. **Host Caddy site** (`/etc/caddy/Caddyfile`, then `sudo systemctl reload caddy`; `jobs.ehlops.com` has the same body):

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
   sudo install -o root -g root -m 0755 -D deploy/k8s/deploy-ssh.sh /usr/local/lib/jobfinder/deploy-ssh.sh
   sudo install -o root -g root -m 0755 -D deploy/k8s/deploy.sh /usr/local/lib/jobfinder/deploy.sh
   echo "restrict,command=\"/usr/local/lib/jobfinder/deploy-ssh.sh staging\" $(cat /tmp/staging_deploy.pub)" >> ~/.ssh/authorized_keys
   gh secret set STAGING_SSH_KEY  -R EhlOps/jobfinder < /tmp/staging_deploy
   gh secret set STAGING_SSH_HOST -R EhlOps/jobfinder --body '<public IP or hostname>'
   gh secret set STAGING_SSH_USER -R EhlOps/jobfinder --body opc
   ssh-keyscan -t ed25519 <public IP or hostname> | gh secret set STAGING_SSH_KNOWN_HOSTS -R EhlOps/jobfinder
   shred -u /tmp/staging_deploy /tmp/staging_deploy.pub
   ```

   `restrict` turns off forwarding, PTYs and agent use, and the forced command runs no matter what the client asks for. The env (`staging`) is part of the forced command, so a client can't pick prod. Test it: `ssh -i <key> opc@<host> 'rm -rf /'` must answer `refused`. Re-install both (`sudo install …` above) whenever `deploy-ssh.sh` or `deploy.sh` changes; the repo copies are never run by the key. Residual trust: kubectl there is cluster-admin, so a manifest on dev could still create objects outside the namespace. A namespace-scoped kubeconfig for the forced command would close that (follow-up). Until the three required secrets exist, `deploy-staging` skips with a warning. Prod's key, environment and rulesets are under "One-time setup for prod autodeploy".
5. **Data and first deploy:** `deploy/k8s/staging-restore.sh`, then **Run workflow** on dev (or `deploy.sh staging sha-<head of dev>`).

## Task platform (#48)

Background work runs on a Postgres **ledger**, with **valkey** for delivery, a **scheduler** and one worker per queue.

```text
api / workers ──(1) INSERT the tasks row, in the caller's transaction──▶ Postgres `tasks` (the ledger: the truth)
      │ (2) after COMMIT: XADD the row's id to its queue's stream
      ▼
valkey  jobfinder:ai     ──▶ worker-ai  (WORKER_AI_CONCURRENCY=2; the only one with the claude CLI)
        jobfinder:io     ──▶ worker-io  (WORKER_IO_CONCURRENCY=10: ingest, planning, discovery, coverage scores, uploads, links)
        jobfinder:render ──▶ renderer   (RENDERER_CONCURRENCY; opt-in, 0 replicas)
        worker: (3) claim the row (one atomic UPDATE, queued → running) → run → finish → XACK

scheduler (1 replica, Recreate)
        cron:   writes the scheduled rows (ingest ticks, daily emails, ...), deduped like user work
        relay:  every RELAY_SECONDS (15s), XADDs due rows that are still queued, so a lost publish is re-sent
        reaper: a running row whose heartbeat is older than TASK_STUCK_AFTER_MINUTES (5) goes back to queued
        prune:  done/failed tasks older than TASK_PRUNE_DAYS (30; up to 5000 rows a tick), rate events, sessions
```

Which kind runs on which queue is in `backend/jobfinder/tasks/kinds.py`.

**How does the api hand work to the workers? Through valkey as well?** Yes, but valkey only delivers. The api writes the ledger row in the request's own transaction. After the commit, it publishes the row's id to the queue's valkey stream, so a worker never sees an uncommitted row. Suppose the publish is lost (valkey is down, or the pod is killed between the commit and the publish). The row is still queued in Postgres, and the scheduler's relay re-sends it on a later tick. A duplicate delivery does no harm: the worker's claim is a single `UPDATE … WHERE status = 'queued'`, so one delivery wins and the others are acked and dropped. Workers enqueue their follow-ups the same way (ingest → `plan_work`, matching → `collect_questions`).

**Failure handling.**
- **Acks.** A worker acks a message only after its task has finished (`--ack-type when_executed`). Every outcome (done, failed, retry scheduled) acks.
- **A worker killed mid-task** leaves its row `running` and its message unacked. Once the heartbeat is stale, the reaper requeues the row and the relay re-sends it. The old message stays pending: taskiq-redis reclaims messages idle for over 10 minutes only on a read that also fetched new messages, so on a quiet stream it comes back once new work arrives. Then it fails the claim and is dropped. Nothing waits on it, since the relay already re-sent the row.
- **A valkey that hangs** rather than refuses (paused, or a network that drops packets) cannot stall a publish: it gives up after 5 seconds, the broker's lazy startup included, and the relay sends the row later. The stream connections have no read timeout (a task's synchronous work may block the event loop for longer), so TCP keepalive notices a vanished valkey instead, in about 45 seconds (idle 30s, then 3 probes 5s apart; `TCP_USER_TIMEOUT` gives up on unacknowledged writes as quickly). A valkey process that is alive but stuck still answers at the TCP level, so a worker's reads and acks wait until it recovers.
- **A retryable Claude error** (a rate limit) sets `run_after` with a 30s/2m/8m backoff, and the relay delivers the row when it is due.
- **Rows that were never published** (inserted by hand, or whose publish was lost) are picked up by the relay on its next ticks.

| Deployment | Image | Command | Replicas | Volumes |
|---|---|---|---|---|
| `scheduler` | `jobfinder-api` | `python -m jobfinder.scheduler` | 1, Recreate, never more than 1 | `/tmp` (heartbeat) |
| `worker-ai` | `jobfinder-worker` | `python -m jobfinder.tasks ai` | KEDA 1–3 (staging 1), RollingUpdate (the Claude cap is the cluster-wide gate, `AI_MAX_CONCURRENCY`) | store, claude-auth, `/tmp`, `$HOME` |
| `worker-io` | `jobfinder-worker` | `python -m jobfinder.tasks io` | KEDA 1–8 (staging 1–2), Recreate | store, `/tmp` |
| `renderer` | `jobfinder-renderer` | `python -m jobfinder.renderer` | 0 (opt-in) | `/tmp`, `/dev/shm` |

- **Labels.** All of them carry `jobfinder.io/postgres-client` and `jobfinder.io/valkey-client`, so the NetworkPolicies let them in.
- **Claude.** Only worker-ai runs Claude. worker-io uses the worker image only for ingestion's dependencies, and never reads the Claude token. It does mount the store: `parse_document` and `fetch_link` read uploads and profile links there off the api, which saves them as "processing" (#51).
- **Liveness.** The scheduler touches `/tmp/scheduler-alive` on every relay tick and is restarted when the file is more than 2 minutes old. The workers have no HTTP endpoint, so their probe checks that the `jobfinder.tasks` process is there.
- **Shutdown.** An in-flight task can finish and ack: worker-ai and worker-priority get `terminationGracePeriodSeconds: 200` and wait up to 180s for their tasks (a matching slice runs ~2.5 minutes), worker-io 60s and 50s (`WAIT_TASKS_SECONDS`, `tasks/__main__.py`).
- **Scaling.** Every worker scales on its stream's lag ("Scale", "KEDA"). worker-ai can run several pods because the Claude gate, not the process, caps parallel Claude calls; CI fails a render where a `worker-*` Deployment has no ScaledObject or sets `replicas`.

### Check the streams

```sh
kubectl -n $NS exec sts/valkey -- valkey-cli xinfo groups jobfinder:ai    # also jobfinder:io, jobfinder:render
kubectl -n $NS exec sts/valkey -- valkey-cli xinfo consumers jobfinder:ai jobfinder-ai
kubectl -n $NS exec sts/valkey -- valkey-cli xpending jobfinder:ai jobfinder-ai
kubectl -n $NS exec sts/valkey -- valkey-cli xlen jobfinder:ai
kubectl -n $NS exec sts/valkey -- valkey-cli info memory | grep used_memory_human
kubectl -n $NS logs deploy/scheduler --tail=50
kubectl -n $NS exec deploy/scheduler -- ls -l --time-style=+%T /tmp/scheduler-alive   # touched within the last 15s
```

What healthy looks like:
- **Groups:** one per stream (`jobfinder-ai`, `jobfinder-io`, `jobfinder-render`), with `lag` at 0 or falling. `jobfinder-io`'s lag is what KEDA scales worker-io on.
- **Pending:** no higher than about twice the worker's concurrency per pod (each read takes at most the concurrency, and the next read comes when a slot frees: 4 for ai, 20 for each worker-io replica), apart from messages of killed pods. Those come back within 10 minutes and are dropped.
- **Length:** `xlen` stays under 10,000, the length the streams are trimmed to (each entry is only an id).
- **Consumers:** a killed pod's consumer stays listed in `xinfo consumers`. Once its `pending` is 0, `valkey-cli xgroup delconsumer jobfinder:ai jobfinder-ai <name>` removes it.

The ledger is the truth for what is outstanding:

```sql
select queue, status, count(*), min(run_after), max(now() - coalesce(locked_at, created_at)) as oldest
from tasks where status in ('queued', 'running') group by 1, 2;
```

A queued row that has been due for more than a minute or two means the relay or that queue's worker isn't running.

### KEDA (#52)

Every task worker scales on the backlog of its own stream, each through a ScaledObject of the same name (`deploy/k8s/base/app/worker-*-scaledobject.yaml`), from which KEDA creates HPA `keda-hpa-<worker>`. KEDA runs cluster-wide in namespace `keda` (`deploy/k3s/manifests/keda.yaml`, chart 2.21.0) and serves `external.metrics.k8s.io`. The scheduler, api and renderer don't autoscale.

| ScaledObject | Stream (group) | Target lag per replica | Replicas: prod | staging |
|---|---|---|---|---|
| `worker-priority` (with the priority lane) | `jobfinder:priority` (`jobfinder-priority`) | 1 | 1–3 | 1–2 |
| `worker-ai` | `jobfinder:ai` (`jobfinder-ai`) | 2 | 1–3 | 1 |
| `worker-io` | `jobfinder:io` (`jobfinder-io`) | 50 | 1–8 | 1–2 |

The base carries prod's maximum; `overlays/staging/kustomization.yaml` patches each ScaledObject's `maxReplicaCount` and its trigger's valkey address.

```text
KEDA operator ──every 15s: XINFO GROUPS jobfinder:<queue>──▶ valkey.<ns>.svc.cluster.local:6379 (NetworkPolicy valkey admits it)
      │ lag of group jobfinder-<queue> (entries not yet delivered to any consumer)
      ▼
HPA keda-hpa-worker-<queue>: replicas = ceil(lag / lagCount), between min and max
      up:   at once (up to double every 15s)
      down: after 5 minutes of a lower backlog, one pod a minute
```

- **Why lag and not pending.** A new replica can only take entries no consumer has read yet. The pending list is what the existing pods have already read (which no other consumer can claim for 10 minutes), plus the entries of killed pods until they are reclaimed. Scaling on it would add pods with nothing to take.
- **No read-ahead.** Each XREADGROUP takes at most the queue's concurrency (`tasks/brokers.py`; taskiq-redis's default is 100), so the lag is the real backlog, and a pod scaled in leaves at most one read's worth of unstarted entries behind (back after 10 minutes, or re-sent by the relay).
- **worker-ai and Claude.** More worker-ai pods mean more task slots, not more Claude calls: the Claude gate (`AI_MAX_CONCURRENCY` permits in valkey, `integrations/claude/gate.py`) caps parallel calls across every pod. Its `lagCount` is 2 because ai tasks are long (a matching slice ~2.5 minutes): two unread messages are a whole pod's worth at `WORKER_AI_CONCURRENCY=2`. Staging stays at one pod: it shares prod's subscription.
- **Replicas.** The worker Deployments have no `replicas`: the HPA owns the count, and a value in the manifest would reset it on every deploy. CI (`.github/workflows/k8s.yml`, "Scaling policy") checks that every `worker-*` Deployment has exactly one ScaledObject and no `replicas`, and that each trigger reads its own namespace's valkey.
- **Deploys.** The ScaledObjects carry `jobfinder.io/rollout=app`, so `deploy.sh` applies them in phase 2, after the migrate Job: an unpause never starts a worker on an unmigrated schema.
- **Scale-down is safe.** A stopped pod gets SIGTERM and its grace period: taskiq stops reading and waits for its running tasks, which ack (worker-io 50s of 60s; worker-ai and worker-priority 180s of 200s, so a matching slice finishes). A task cut off anyway stays `running` until the reaper requeues it; its unread prefetched messages stay pending and come back to another consumer after 10 minutes (and are dropped, since the relay already re-sent the rows).
- **No PodDisruptionBudget.** On one node it would only matter for a drain (a k3s upgrade), and `minAvailable: 1` would block that drain, while a pod stopping mid-task costs nothing.
- **Capacity.** A worker-io replica requests 250m CPU and 768Mi (limit 1536Mi; 8 of them are 12 GiB worst case on the 24 GB node), a worker-ai one 200m and 512Mi (limit 2Gi). With every worker of both environments at its maximum the CPU requests exceed the node's 4 CPUs: the last replicas wait `Pending` until the backlog (and the other pods) shrink, which delays nothing that the running ones aren't already working through. Memory limits are overcommitted the same way (every pod at its limit would be well past 24 GB); the scheduler reserves only the requests, so a sustained peak of both environments can OOM-kill a pod. The per-host caps in the fetchers are per process, so 8 worker-io replicas mean up to 8x the load on one site.
- **If KEDA fails**, the HPA can't read the metric and keeps the current replica count (never below 1). If valkey is down, the workers can't work anyway.

**Install / upgrade.** `sudo bash deploy/k3s/install.sh` copies the HelmChart into k3s's manifests directory, which k3s's Helm controller installs. `kubectl apply -f deploy/k3s/manifests/keda.yaml` as opc does the same right away (idempotent; the copy in the manifests directory is what survives a k3s reinstall). Install it before deploying an overlay: `deploy.sh` refuses to deploy without the `scaledobjects.keda.sh` CRD.

**Verify.**

```sh
kubectl -n keda get pods                                   # operator, metrics-apiserver, admission-webhooks Running
kubectl get apiservice v1beta1.external.metrics.k8s.io      # AVAILABLE True
kubectl -n $NS get scaledobject                            # each READY True, ACTIVE True/False, PAUSED (empty)
kubectl -n $NS get hpa                                     # keda-hpa-<worker>: TARGETS <lag>/<lagCount> (avg), MINPODS, MAXPODS
kubectl -n $NS describe hpa keda-hpa-worker-ai | tail -5   # scaling events (also keda-hpa-worker-io, -worker-priority)
kubectl -n keda logs deploy/keda-operator --tail=50 | grep -i -e error -e worker-
kubectl get --raw "/apis/external.metrics.k8s.io/v1beta1/namespaces/$NS/s0-redis-streams-jobfinder-io?labelSelector=scaledobject.keda.sh%2Fname%3Dworker-io"
```

Pausing and resuming are under "Scale".

### Kill-tests

Run them on staging (`NS=jobfinder-staging`), never on prod, after any change to the task platform.

**Before each test:**
1. In a second terminal, watch the pods with `kubectl -n $NS get pods -w`.
2. Open psql: `kubectl -n $NS exec -it postgres-0 -- psql -U jobfinder -d jobfinder`.
3. Mark the start with `select now() \gset t0_`. `:'t0_now'` is then that time.

After each test, every task created since then must have run exactly once: `done`, or `failed` for a reason it would fail anyway.

```sql
-- No lost tasks: nothing left queued or running; attempts 1, or 2 for a task that was cut off.
select kind, status, attempts, count(*) from tasks where created_at > :'t0_now' group by 1, 2, 3 order by 1, 2, 3;
-- No doubled scheduled work: one row per deduped kind and minute.
select kind, date_trunc('minute', created_at) as minute, count(*) from tasks
where created_at > :'t0_now' and dedupe_key is not null group by 1, 2 having count(*) > 1;
-- No doubled Claude work: one call per task (plus one for a task that was cut off and re-run).
select task, ok, count(*) from ai_calls where created_at > :'t0_now' group by 1, 2 order by 1, 2;
```

To queue cheap work by hand, insert rows straight into the ledger. Only the relay can deliver them. On staging, `plan_work` does nothing (`PLANNER_MAX_PER_TICK=0`), and `ai_check` makes one small Claude call:

```sql
insert into tasks (kind, queue, payload, status, attempts, run_after, created_at)
select k, q, '{}', 'queued', 0, now(), now()
from (values ('plan_work', 'io'), ('ai_check', 'ai')) v(k, q), generate_series(1, 10);
```

1. **valkey down for 5 minutes while work is queued.**

   ```sh
   kubectl -n $NS scale sts/valkey --replicas=0
   # for 5 minutes: run the insert above twice, and start a cover letter and a match in the staging UI
   kubectl -n $NS logs deploy/api --tail=20          # publish failures are logged; the requests still succeed
   kubectl -n $NS scale sts/valkey --replicas=1 && kubectl -n $NS rollout status sts/valkey
   ```

   **Expect:**
   - **While valkey is down,** the rows stay `queued` and the requests succeed, because the publish runs after the commit. The workers and the scheduler may restart while they can't reach valkey; `kubectl get pods` shows the restarts, perhaps with a back-off of a few minutes.
   - **Within a few minutes of valkey coming back,** the relay sends everything. The first query shows every row `done` with attempts 1, and nothing queued.
   - **In `xinfo groups`,** the groups survived (AOF) and `lag` is 0.

2. **Kill worker-ai mid-task.** Start a cover letter in the UI. When its row is `running` (`select id, status, attempts, locked_at from tasks where kind = 'cover_letter' order by id desc limit 1;`):

   ```sh
   kubectl -n $NS delete pod -l app.kubernetes.io/name=worker-ai --grace-period=0 --force
   ```

   **Expect:**
   - **The new pod** starts at once.
   - **The row** stays `running` until its heartbeat is `TASK_STUCK_AFTER_MINUTES` (5) old. Then the reaper requeues it and the relay re-sends it, so it is `done` with attempts 2 after about 6 minutes. The scheduler log shows the requeue.
   - **The letter** exists once, and `ai_calls` has one cover-letter call for each attempt.
   - **Within 10 minutes,** `xpending jobfinder:ai jobfinder-ai` is back to 0: the dead consumer's message was redelivered, failed its claim and was acked.

3. **Restart the scheduler while work is queued.** Run the insert first, then:

   ```sh
   kubectl -n $NS rollout restart deploy/scheduler && kubectl -n $NS rollout status deploy/scheduler
   kubectl -n $NS get pods -l app.kubernetes.io/name=scheduler      # never two at once (Recreate)
   ```

   **Expect:**
   - **Rows published before the restart** finish in the meantime.
   - **The rows inserted by hand** wait for the new scheduler's first relay tick.
   - **The first two queries** show everything `done` once, and no deduped kind twice in a minute.

   Repeat the test across a cron boundary (an ingest tick, every 15 minutes) to check that a job the old pod fired isn't fired again.

4. **A cover letter while ingest runs.** Queue an ingest tick, or wait for one; it fans out one `ingest_board` row per due board:

   ```sql
   insert into tasks (kind, queue, payload, status, attempts, run_after, created_at)
   values ('ingest_tick', 'io', '{}', 'queued', 0, now(), now());
   ```

   While its `ingest_board` rows are running, start a cover letter in the UI. The letter must not wait for the ingest: they are on different streams, run by different workers.

   ```sql
   select id, kind, status, attempts, created_at, locked_at, finished_at, finished_at - created_at as took
   from tasks where created_at > :'t0_now' order by id;
   ```

   **Expect:** `cover_letter` is claimed (`locked_at`) within seconds of `created_at`, while `ingest_board` rows are still `running`. All end `done`, with attempts 1.

Record the results (date, commit, timings) on the PR that changed the platform.

### Scheduling baseline (#124)

#124's PRD 1 is that every profile task returns within 2 minutes of being queued. Since migration 0027, `ledger.claim` stamps `tasks.started_at` on each claim, so a task's wait (`started_at - run_after`, the latest attempt's) and its run (`finished_at - started_at`) are recorded separately. The **Queues (last 24h)** table on `/settings/schedule` shows them per queue: tasks, wait p50/p95, run p95, and waits over 60s. A worker also logs a warning for every task that waited more than 60s before its claim (`task <id> (<kind>) on <queue> waited <n>s before claim`). The wait runs from `run_after`, so a task the reaper requeued counts its dead worker's run as wait, and a rerun held behind its running twin counts only from the twin's finish (its `run_after` moves while it is held). Retries waiting out their backoff are left out of the table until they are claimed again.

This is prod over the 7 days to 2026-10-22, before #124's changes. `started_at` did not exist yet, so wait-before-claim was not recorded and these are end-to-end times only. The owner wants to revisit the match_user slice size against this baseline: compare it with the Queues table once #124 is on prod.

| Task (`tasks`, created → finished) | Runs | p50 | p95 | Max |
|---|---|---|---|---|
| `extract_profile` | 4 | 14.4s | 22.6s | |
| `audit_profile` | 1 | 39s | | |
| `match_user` | 12 | 236s | 2621s | 3911s |
| `collect_questions` | 15 | 0.1s | 96s | 297s |
| `ingest_board` | 2111 | 5.9s | 74s | 245s |

| Claude call (`ai_calls`) | Calls | p50 | p95 | Max |
|---|---|---|---|---|
| `score_match` | 1329 | 11.0s | 13.7s | 31.1s |
| `extract_profile` | | 14.0s | 22.5s | |
| `audit_profile` | | 38s | | |

- **match_user** scored 21–245 jobs per run. User 2 had a daily budget of 1000 with about 34.7k matches pending.
- **Jobs.** 111,248 active jobs.

## Scale

| Workload | How it scales |
|---|---|
| `worker-priority`, `worker-ai`, `worker-io` | KEDA, each on the lag of its own stream (the table under "KEDA": ranges, `lagCount`). Change a range in `base/app/<worker>-scaledobject.yaml` (`minReplicaCount`/`maxReplicaCount`; prod's) or in the staging overlay's patch. More worker-ai or worker-priority pods don't mean more Claude calls: the Claude gate caps them cluster-wide, so to allow more calls raise `AI_MAX_CONCURRENCY`, not the replicas. CI refuses a worker Deployment that sets `replicas` or has no ScaledObject. |
| `api` | 2 replicas (`base/app/api.yaml`), stateless. Change `replicas` there (or patch it in an overlay) and deploy; `kubectl -n $NS scale deploy/api --replicas=3` works until the next deploy. |
| `scheduler` | Exactly 1 (Recreate). A second one would fire every cron job twice. |
| `renderer` | 0 or 1, opt-in (README, "render worker"). |

**Pause and resume a worker.** `kubectl scale deploy/<worker>` is undone within 15 seconds; pause its ScaledObject instead (one worker by name, or all of them with `--all`):

```sh
kubectl -n $NS annotate scaledobject worker-io autoscaling.keda.sh/paused-replicas=0 --overwrite   # worker-io at 0, HPA removed
kubectl -n $NS annotate scaledobject worker-io autoscaling.keda.sh/paused-replicas-                # back to KEDA
kubectl -n $NS annotate scaledobject --all autoscaling.keda.sh/paused-replicas=0 --overwrite       # every worker at 0
```

A pause set like this survives deploys (the manifests don't carry the annotation). `staging-restore.sh` pauses every ScaledObject itself (`keda_pause`, `deploy/k8s/pg-lib.sh`), also marking each `jobfinder.io/paused-by=<script>`, and resumes those when it scales the app back; a pause it didn't set is left alone. `deploy.sh` lifts every marked pause after its rollout, so a script that failed halfway recovers with the redeploy its log asks for. To pause for something that ends with a deploy, mark it too: add `jobfinder.io/paused-by=<you>` to the annotate command.

**Stop all background work** (for maintenance, or before a restore):

```sh
kubectl -n $NS annotate scaledobject --all autoscaling.keda.sh/paused-replicas=0 jobfinder.io/paused-by=maintenance --overwrite
kubectl -n $NS scale deploy scheduler renderer --replicas=0
```

The pause scales every worker to 0. Queued rows wait in the ledger. To resume, deploy the running tag again (`deploy.sh <overlay> <running tag>`), which restores the scheduler's replicas and lifts the marked pauses; or `kubectl -n $NS scale deploy scheduler --replicas=1` and remove the annotations by hand (`kubectl -n $NS annotate scaledobject --all autoscaling.keda.sh/paused-replicas- jobfinder.io/paused-by-`).

## Logs & troubleshooting

```sh
kubectl -n $NS get pods                                                     # restarts, CrashLoopBackOff, Pending
kubectl -n $NS logs deploy/api --tail=100                                    # one pod of the Deployment
kubectl -n $NS logs -l app.kubernetes.io/name=api --prefix --tail=50         # every pod (also the workers)
kubectl -n $NS logs deploy/worker-ai --tail=100 -f                           # follow
kubectl -n $NS logs deploy/scheduler --previous                              # the container before its last restart
kubectl -n $NS logs job/migrate                                              # the last deploy's migration
kubectl -n $NS describe pod <pod>                                            # events: probes, OOMKilled, image pulls
kubectl -n $NS get events --sort-by=.lastTimestamp | tail -20
```

Deployments: `web`, `api`, `scheduler`, `worker-ai`, `worker-io`, `renderer`, `smtp` (and `mailpit` in staging).

**Stuck or failed tasks.** Start with the CLI, then the ledger:

```sh
kubectl -n $NS exec deploy/api -- jobfinder tasks list --status failed --limit 20
kubectl -n $NS exec deploy/api -- jobfinder tasks list --status running
kubectl -n $NS exec deploy/api -- jobfinder tasks list --kind ingest_board --limit 10
kubectl -n $NS exec deploy/api -- jobfinder tasks retry <id>      # a done or failed task, from scratch
```

- **Queued and due for minutes:** that queue's worker isn't running, or the relay isn't (scheduler down, or its heartbeat stale). Check `kubectl -n $NS get pods`, the scheduler log, and the SQL in "Check the streams".
- **Running for more than 5 minutes** with no progress: the reaper requeues it once the heartbeat is stale; if it doesn't, the scheduler isn't running.
- **Failed:** the error is on the row (`select id, kind, error from tasks where status = 'failed' order by id desc limit 20;`). Fix the cause, then `tasks retry`.
- **Claude errors:** `kubectl -n $NS exec deploy/worker-ai -- jobfinder ai check` (exits 1 when Claude doesn't answer: a bad or expired token, or a rate limit). Replace the token on the AI setup page.
- **valkey:** `kubectl -n $NS exec valkey-0 -- valkey-cli ping` (PONG), then the stream and consumer checks in "Check the streams". Lag that grows while a worker is at its maximum (`kubectl -n $NS get hpa`) is a backlog; lag that grows with 0 consumers listed means the workers can't reach valkey (NetworkPolicy label, or valkey restarting).

**api not ready** (`0/1` READY, 503 from `/readyz`): the body names the reason. `schema at X, image expects Y` or `database is not migrated` means the migration didn't run: check `logs job/migrate` and redeploy. `database unreachable` means postgres: `kubectl -n $NS get pods postgres-0` and its log.

**Mail:** `kubectl -n $NS logs deploy/smtp --tail=50` (prod: `status=sent` lines and the relayhost `[smtp.gmail.com]:587`); `kubectl -n $NS exec deploy/api -- jobfinder email test --to you@example.com` sends a test message (on staging it lands in mailpit).

## Backups (#47)

CronJob `backup` (`deploy/k8s/base/data/`, in every namespace) runs at **03:15 UTC**. It waits for postgres, writes `pg_dump -Fc` to `db-<UTC timestamp>.dump` and a tar of the `store` and `claude-auth` PVCs to `files-<UTC timestamp>.tar.gz` (directories `store/` and `claude/`) on PVC **`backups`** (`local-path-retain`, 20Gi). Each file is written as `.part` and renamed when complete; the newest 14 of each are kept (`BACKUP_KEEP`). It runs as uid 10001 (the app's, which owns the files) with a read-only root, carries `jobfinder.io/postgres-client` for the NetworkPolicy, and has `concurrencyPolicy: Forbid`; a run missed while the node was down starts within the hour.

**Pre-deploy backups (#118)** are a separate pool on the same PVC. Each prod release takes one before it touches anything ("Releasing"): a Job `backup-pre-<sha7>` from the same CronJob with `BACKUP_PREFIX=pre-`, which writes `pre-db-<ts>.dump` and `pre-files-<ts>.tar.gz`. `BACKUP_PREFIX` is empty for the nightly run; the CronJob accepts only empty or `pre-`. A second rotation keeps the newest `BACKUP_PRE_KEEP` (5) of each `pre-` kind. The nightly rotation only matches `db-*`/`files-*`, so several releases in a day never push out the nightlies. Take one by hand (the wrapper's command):

```sh
kubectl -n jobfinder create job --from=cronjob/backup backup-pre-<sha7> --dry-run=client -o yaml \
  | kubectl set env --local -f - BACKUP_PREFIX=pre- -o yaml | kubectl apply -f -
kubectl -n jobfinder wait --for=condition=complete job/backup-pre-<sha7> --timeout=15m
```

**The backups are node-local** (`/var/lib/rancher/k3s/storage/<pv>_<namespace>_backups`): they survive pod, PVC and namespace deletion (Retain), not the loss of the disk. Copy them off the box ("Off-box copy") until the bucket from #36 Q3 exists.

```sh
kubectl -n jobfinder get cronjob backup                               # LAST SCHEDULE
kubectl -n jobfinder get jobs                                         # recent runs; kubectl logs job/<name>
kubectl -n jobfinder create job --from=cronjob/backup backup-now      # one now; delete the Job afterwards
```

**Restore drill** (monthly):

```sh
deploy/k8s/backup-restore.sh jobfinder                                # newest dump
deploy/k8s/backup-restore.sh jobfinder db-20261018-031500.dump        # or a given one
deploy/k8s/backup-restore.sh jobfinder pre-db-20261021-142000.dump    # or a pre-deploy one
```

It restores the dump into a scratch database (`restore_drill_<ts>`) in that namespace's postgres from a helper pod that mounts `backups` read-only, compares every table's row count with the live database, checks that the matching files tar reads back, and drops the scratch database. The live database and the app are never touched. The live database moves on after the dump, so drill right after `create job --from=cronjob/backup`; tables written in between show as MISMATCH (exit 1). Rehearsed on staging (2026-10-17, worker paused for an exact comparison): 86 MB dump, restored in 15 s, 16 tables, 0 mismatched, 19 s in all.

### Off-box copy

Copy the newest dump and files tar to another machine at least weekly, and before anything risky. From the host, start a helper pod that mounts `backups` read-only as the CronJob's uid (the files are 0600), copy out, and delete the pod:

```sh
NS=jobfinder
kubectl -n $NS run backup-copy --image=postgres:16.15 --restart=Never \
  --overrides='{"spec":{"securityContext":{"runAsNonRoot":true,"runAsUser":10001,"runAsGroup":10001,"seccompProfile":{"type":"RuntimeDefault"}},
    "containers":[{"name":"backup-copy","image":"postgres:16.15","command":["sleep","3600"],
      "securityContext":{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]}},
      "volumeMounts":[{"name":"backups","mountPath":"/backups","readOnly":true}]}],
    "volumes":[{"name":"backups","persistentVolumeClaim":{"claimName":"backups","readOnly":true}}]}}'
kubectl -n $NS wait --for=condition=Ready pod/backup-copy --timeout=120s
kubectl -n $NS exec backup-copy -- ls -lt /backups | head -5            # pick the newest <ts>
mkdir -p ~/jobfinder-backups && chmod 700 ~/jobfinder-backups
kubectl -n $NS cp --retries=10 backup-copy:/backups/db-<ts>.dump ~/jobfinder-backups/db-<ts>.dump
kubectl -n $NS cp --retries=10 backup-copy:/backups/files-<ts>.tar.gz ~/jobfinder-backups/files-<ts>.tar.gz
kubectl -n $NS delete pod backup-copy
```

`kubectl cp` streams through `kubectl exec`, which can break off on large files; `--retries` resumes it. Check the copy (`pg_restore --list ~/jobfinder-backups/db-<ts>.dump | head`, `tar tzf ~/jobfinder-backups/files-<ts>.tar.gz | head`), then pull it to the other machine (`scp` or `rsync` from there) and delete the local copy. The dump holds every user's data and the tar holds the Claude token: keep them encrypted at rest. If `kubectl cp` keeps failing, `sudo` can read the files straight from the PV directory (`sudo ls /var/lib/rancher/k3s/storage/ | grep _${NS}_backups`).

### Restoring for real

This replaces the namespace's data:

1. Stop the app: pause every worker's ScaledObject, marked so the final deploy resumes them (`kubectl -n $NS annotate scaledobject --all autoscaling.keda.sh/paused-replicas=0 jobfinder.io/paused-by=restore --overwrite`), and `kubectl -n $NS scale deploy api renderer scheduler --replicas=0`.
2. In `postgres-0`: `dropdb -U jobfinder --force jobfinder && createdb -U jobfinder -O jobfinder jobfinder`.
3. Run `pg_restore -h postgres -U jobfinder -d jobfinder --no-owner --no-privileges --exit-on-error --single-transaction /backups/db-<ts>.dump` (or `pre-db-<ts>.dump`) from a pod like the drill's (`backup-restore.sh` shows the spec: uid 10001, `jobfinder.io/postgres-client`, `PGPASSWORD` from `postgres-credentials`, `backups` read-only). Don't stream a dump out through `kubectl exec` stdout: large outputs break off with a websocket EOF; stdin is fine.
4. Untar the matching `files-<ts>.tar.gz` (or `pre-files-<ts>.tar.gz`) into the PVCs (`store/` into `store`, `claude/` into `claude-auth`) from a root helper pod with `--numeric-owner`.
5. Empty the task streams: `for q in ai io render; do kubectl -n $NS exec valkey-0 -- valkey-cli XTRIM jobfinder:$q MAXLEN 0; done`.
6. `deploy.sh <overlay> <running tag>` (it migrates if the dump is older than the image, restores the replicas and lifts the pause).

## Resources

The host has 4 vCPU / 22 GB. k3s with Traefik, CoreDNS, metrics-server, sealed-secrets and KEDA (3 pods, 300m CPU and 300Mi requested) adds roughly 0.8–1.2 GB on top of the two environments; watch `free -g` and `kubectl top pods -A`.

## History

Production ran on docker compose until the cutover to k3s on 2026-10-17 (#47, `prod-cutover.sh`); the ledger replaced the legacy worker the same day, and #53 removed compose, the legacy worker and the cutover script. Those runbooks are in this file's git history.

**Compose leftovers on the host.** The old compose named volumes (`docker volume ls -q --filter name=^jobfinder_`: `pgdata`, `store`, `claude-auth`, `caddy-data`, `caddy-config`, `dkim`) are kept as a last-resort copy of the pre-cutover data until **2026-10-31**. After that date, remove them (not `k3d-jobfinder-dev-images`, which belongs to the local dev cluster):

```sh
docker ps -a --filter name=^jobfinder- --format '{{.Names}}'    # stopped compose containers, if any: docker rm them first
docker volume rm $(docker volume ls -q --filter name=^jobfinder_)
```
