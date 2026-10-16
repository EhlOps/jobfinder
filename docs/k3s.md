# k3s runbook

JobFinder is moving from docker compose to a single-node k3s cluster on the same host (#36, #38).
Until the cutover (#47) production keeps running on compose; k3s runs alongside it.

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

Compose is never touched, so removing k3s returns the host to its previous state:

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

The base (`deploy/k8s/base/`) carries the stateful services; they run in staging (`jobfinder-staging`) today, and prod (`jobfinder`) gets them at the cutover (#47).

| Object | What | Storage |
|---|---|---|
| StatefulSet + Service `postgres` | `postgres:16.15`, same major as compose; user and database `jobfinder` | PVC `data-postgres-0`, 20Gi |
| StatefulSet + Service `valkey` | `valkey/valkey:8.1.10`, `--appendonly yes`, `noeviction`, no password; unused until #48 | PVC `data-valkey-0`, 1Gi |
| PVC `store`, `claude-auth` | successors of the compose volumes, mounted by the app in #43 | 5Gi, 100Mi |
| SealedSecret `postgres-credentials` | key `POSTGRES_PASSWORD`, random, sealed per namespace (`overlays/staging/sealed-secret-postgres.yaml`) | |
| NetworkPolicy `postgres`, `valkey` | ingress only from same-namespace pods with the client label | |

All volumes use the `local-path-retain` StorageClass (`deploy/k3s/manifests/local-path-retain.yaml`): local-path with `reclaimPolicy: Retain`, so deleting a PVC or the namespace leaves the data under `/var/lib/rancher/k3s/storage/` and a `Released` PV. Delete the PV (and that directory) by hand once you're sure. The pods run as non-root (uid 999) with all capabilities dropped.

**Backups and restore are deferred** per the owner's comment on #40 (2026-10-15). Nothing backs up the k3s data yet; `deploy/backup.sh` still covers the compose production stack.

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

Today's app runs in `jobfinder-staging` next to the data services, on a copy of prod's data, at **https://staging.ehlops.com**. Production stays on compose until #47.

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

**Config.** ConfigMap `jobfinder-config` = `backend/.env.defaults` + `base/app` overrides (`POSTGRES_HOST=postgres`, `STORE_DIR`/`CLAUDE_AUTH_DIR` on the PVCs, `FORWARDED_ALLOW_IPS=10.42.0.0/16`, `MIGRATE_ON_START=false`) + staging's (`PUBLIC_BASE_URL`, SMTP to mailpit, `PLANNER_MAX_PER_TICK=0` so copied users don't spend the shared Claude subscription in the background). Secret `jobfinder-env` (SealedSecret: a staging-only `SECRET_KEY` and prod's Claude token); `POSTGRES_PASSWORD` from `postgres-credentials`.

**Readiness.** `GET /readyz` on the api answers 200 only when the database answers and `alembic_version` equals the image's alembic head; otherwise 503 with the reason. Pods never serve against an unmigrated schema. Side effect: right after a deploy migrates, the old pods turn unready until the new ones are ready (a few seconds of 503s when a deploy carries a migration).

**Client IPs.** Host Caddy replaces any client-sent `X-Forwarded-For` with the real IP. Traefik trusts only `10.42.0.1` (the cni0 address its hostPort traffic arrives from; `deploy/k3s/manifests/traefik-config.yaml`), web's Caddy trusts the pod CIDR, and uvicorn takes the rightmost untrusted hop. The rate limiter therefore sees the browser's IP.

### Deploy

GitHub Actions deploys every push to dev once `images` has pushed that commit's images (`images.yml` → `deploy-staging.yml`). It SSHes to the host with a key whose forced command runs `deploy/k8s/deploy-staging-ssh.sh`, which accepts exactly `sha-<7-40 hex>`, checks the commit is on `origin/dev`, exports that commit's `deploy/k8s` and runs its `deploy.sh`. For a manifest-only change, use **Actions → deploy-staging → Run workflow** on dev. By hand, on the host:

```sh
deploy/k8s/deploy.sh staging sha-1a2b3c4      # from a checkout of that commit
```

`deploy.sh`:

1. **Pins each image.** `sha-<commit>` if GHCR has it; else the image the Deployment already runs (a frontend-only push rebuilds only web, a backend-only one only api/worker/renderer); else, on a first deploy, the current digest of `dev`. A registry error stops the deploy instead of falling back.
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

It scales api/worker/renderer to 0, drops and recreates the staging database, streams `pg_dump -Fc` from the compose container `jobfinder-db-1` into `pg_restore` in `postgres-0` (no file on disk), and scales the app back. The schema is then at prod's revision: if staging's image is newer, the api stays unready until you redeploy (which migrates). Documents in compose's `store` volume are not copied. Staging's users are real people: all mail goes to mailpit, and nothing else may send.

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
   sudo install -o root -g root -m 0755 deploy/k8s/deploy-staging-ssh.sh /usr/local/bin/jobfinder-deploy-staging
   echo "restrict,command=\"/usr/local/bin/jobfinder-deploy-staging\" $(cat /tmp/staging_deploy.pub)" >> ~/.ssh/authorized_keys
   gh secret set STAGING_SSH_KEY  -R EhlOps/jobfinder < /tmp/staging_deploy
   gh secret set STAGING_SSH_HOST -R EhlOps/jobfinder --body '<public IP or hostname>'
   gh secret set STAGING_SSH_USER -R EhlOps/jobfinder --body opc
   ssh-keyscan -t ed25519 <public IP or hostname> | gh secret set STAGING_SSH_KNOWN_HOSTS -R EhlOps/jobfinder
   shred -u /tmp/staging_deploy /tmp/staging_deploy.pub
   ```

   `restrict` turns off forwarding, PTYs and agent use; the forced command runs no matter what the client asks for. Test it: `ssh -i <key> opc@<host> 'rm -rf /'` must answer `refused`. Re-install the wrapper (`sudo install …` above) whenever `deploy-staging-ssh.sh` changes; the repo copy is never run directly. Until the three required secrets exist, `deploy-staging` skips with a warning.
5. **Data and first deploy:** `deploy/k8s/staging-restore.sh`, then **Run workflow** on dev (or `deploy.sh staging sha-<head of dev>`).

## Resources

The host has 4 vCPU / 22 GB. k3s with Traefik, CoreDNS, metrics-server and sealed-secrets adds roughly 0.6–1 GB; watch `free -g` while staging (#43) runs next to compose.
