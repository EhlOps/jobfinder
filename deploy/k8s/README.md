# deploy/k8s

Kubernetes manifests for JobFinder on the single-node k3s cluster (#38).
The cluster itself (k3s install, Traefik on `127.0.0.1:8280`, firewalld,
SELinux) is set up by `deploy/k3s/` and documented in `docs/k3s.md`. Host
systemd Caddy stays the TLS edge on 80/443.

## Layout

```
deploy/k8s/
  base/                 # shared by every environment; never applied directly
    kustomization.yaml  # common labels
    namespace.yaml      # Namespace `jobfinder`
    postgres/           # #40: Postgres 16 StatefulSet, Service, NetworkPolicy
    valkey/             # #40: valkey StatefulSet (AOF), Service, NetworkPolicy; the task streams (#48)
    storage/            # #40: PVCs store, claude-auth
    data/               # #47: backup CronJob and PVC backups
    app/                # #43: web, api, worker, renderer, smtp, migrate Job, Ingress, config overrides;
                        # #48: scheduler, worker-ai, worker-io (0 replicas until an overlay sets TASKS_BACKEND=ledger);
                        # #52: worker-io's KEDA ScaledObject (1-4 replicas on the io backlog)
  overlays/
    staging/            # namespace `jobfinder-staging`; sealed secrets, mailpit, staging config
    prod/               # namespace `jobfinder` (the base default); sealed secrets, Gmail relay, prod config
  hello/                # #38 smoke test: whoami behind Traefik at /healthz
  deploy.sh             # #43: pin images, migrate, roll out (deploy.sh <overlay> sha-<commit>)
  deploy-staging-ssh.sh # #43: SSH forced command GitHub Actions deploys staging through
  staging-restore.sh    # #43: copy prod's compose database into staging
  prod-cutover.sh       # #47: compose -> k3s data move and first prod deploy (--rehearsal on staging)
  backup-restore.sh     # #47: restore drill of the newest backup into a scratch database
  pg-lib.sh             # #47: row-count, stream and KEDA-pause helpers the scripts above share
```

The app's ConfigMap comes from `backend/kustomization.yaml`, which turns
`backend/.env.defaults` into `jobfinder-config` (kustomize can only read files
in or below a kustomization's directory). `base/app` and each overlay merge
their overrides into it.

**Namespaces.** Prod uses `jobfinder`; staging uses `jobfinder-staging` so both
can run side by side on one node with identical object names. The overlay's
`namespace:` field rewrites every object, including the Namespace itself. The
smoke test lives in its own `jobfinder-hello` namespace, so deleting it never
touches an environment.

**Labels.** The base adds `app.kubernetes.io/part-of: jobfinder` and
`app.kubernetes.io/managed-by: kustomize`; each overlay adds
`app.kubernetes.io/instance: <env>`. They are applied with `labels:` (not
`commonLabels:`), which leaves selectors alone: Deployment selectors are
immutable, so changing a common label later must not touch them.

**Images.** GHCR images (`ghcr.io/ehlops/jobfinder-*`) are public, so no
`imagePullSecret` is needed. Each overlay's `images:` block names a default
tag (`dev`/`main`); `deploy.sh` layers a pin per image on top at deploy time:
`sha-<commit>` when that commit built the image, otherwise the image the
Deployment already runs, otherwise the current digest of `dev`/`main`. Deploy
with `deploy.sh`, not a bare `kubectl apply -k`, which would deploy the moving
default tag and skip the migrate-before-rollout ordering. Runbook:
`docs/k3s.md`, "Staging".

**Data services.** Postgres and valkey only accept pods in the same namespace
labelled `jobfinder.io/postgres-client: "true"` / `jobfinder.io/valkey-client:
"true"`. Each overlay must supply the Secret `postgres-credentials`
(`POSTGRES_PASSWORD`) as a SealedSecret. Details and connection strings:
`docs/k3s.md`, "Data services".

## Render and apply

Render offline (no cluster needed):

```sh
kubectl kustomize deploy/k8s/overlays/staging
# without a local kubectl:
docker run --rm -v "$PWD:/w" -w /w registry.k8s.io/kubectl:v1.33.0 kustomize deploy/k8s/overlays/staging
```

Validate the rendered output:

```sh
kubectl kustomize deploy/k8s/overlays/staging > /tmp/staging.yaml
docker run --rm -v /tmp:/r ghcr.io/yannh/kubeconform:v0.6.7 -strict -ignore-missing-schemas -summary /r/staging.yaml
```

Apply (on the node, `kubectl` talks to k3s via `/etc/rancher/k3s/k3s.yaml`):

```sh
kubectl apply -k deploy/k8s/overlays/staging --dry-run=server   # check first
kubectl apply -k deploy/k8s/overlays/staging
kubectl diff  -k deploy/k8s/overlays/prod                       # before touching prod
```

That is fine for the data services and for checking a change. To ship the app,
use `deploy/k8s/deploy.sh staging sha-<commit>`: it pins the images and runs
the migrate Job before any Deployment rolls (`docs/k3s.md`, "Staging").

### Smoke test

```sh
kubectl apply -k deploy/k8s/hello
kubectl -n jobfinder-hello rollout status deploy/hello
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8280/healthz   # 200
```

The Ingress matches `/healthz` exactly (whoami answers 200 on any path), so it
does not shadow app routes added later. Remove it with
`kubectl delete -k deploy/k8s/hello`.

## Secrets (sealed-secrets)

**Plain `Secret` manifests never go in git.** Only `SealedSecret` objects do:
they are encrypted with the cluster's sealed-secrets public key and can only
be decrypted by the controller in that cluster, for the namespace (and name)
they were sealed for.

1. Generate a plain Secret locally, without touching the cluster, and pipe it
   straight into `kubeseal` so the plaintext never lands on disk:

   ```sh
   kubectl create secret generic jobfinder-env \
     --namespace jobfinder-staging \
     --from-env-file=.env \
     --dry-run=client -o yaml \
   | kubeseal --format yaml \
       --controller-namespace kube-system \
       --controller-name sealed-secrets-controller \
   > deploy/k8s/overlays/staging/sealed-secret-env.yaml
   ```

   The controller flags shown are kubeseal's defaults; change them if the
   controller is installed elsewhere. `--from-env-file` keeps quotes
   literally, so `.env` values must be unquoted. Use
   `--from-literal=KEY=value` for single values. The `--namespace` must be
   the namespace the secret will live in (`jobfinder-staging` or `jobfinder`):
   a SealedSecret sealed for staging will not decrypt in prod, so seal once per
   overlay.

2. Add the file to that overlay's `resources:` (staging already lists
   `sealed-secret-env.yaml` and `sealed-secret-postgres.yaml`) and commit it.
3. Apply the overlay; the controller creates the real `Secret` in-cluster.

To seal offline (e.g. on a laptop without cluster access), fetch the public
certificate once with `kubeseal --fetch-cert > pub-cert.pem` and pass
`--cert pub-cert.pem` instead of the controller flags. The certificate is
public and safe to share; the controller's private key must be backed up
out-of-band (see `docs/k3s.md`), or sealed secrets cannot be decrypted after a
cluster rebuild.

To rotate a value, re-run step 1 and commit the new file. Add
`--merge-into <file>` to `kubeseal` to update a single key.

**What the app's Secret holds.** `jobfinder-env` carries only `SECRET_KEY` and
`CLAUDE_CODE_OAUTH_TOKEN`; the pods take `POSTGRES_PASSWORD` from
`postgres-credentials`, so the database password lives in one Secret. Staging's
was sealed like this (the token is the one saved on prod's admin "AI setup"
page, which lives in the compose `claude-auth` volume; `backend/.env` has none):

```sh
kubectl create secret generic jobfinder-env --namespace jobfinder-staging \
  --from-literal=SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" \
  --from-literal=CLAUDE_CODE_OAUTH_TOKEN="$(docker exec jobfinder-api-1 cat /data/claude/oauth_token)" \
  --dry-run=client -o yaml \
| kubeseal --format yaml --controller-namespace kube-system --controller-name sealed-secrets-controller \
> deploy/k8s/overlays/staging/sealed-secret-env.yaml
```

Pods read the Secret at start: after applying a new one, `kubectl -n
jobfinder-staging rollout restart deploy/api deploy/worker deploy/scheduler deploy/worker-ai deploy/worker-io`.
