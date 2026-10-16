#!/usr/bin/env bash
# SSH forced command for staging deploys from GitHub Actions (#43, .github/workflows/deploy-staging.yml).
#
# Installed OUTSIDE the repo, root-owned, together with the deploy.sh it runs, so a push can't change
# what the key executes (docs/k3s.md, "Staging", one-time setup):
#   sudo install -o root -g root -m 0755 -D deploy/k8s/deploy-staging-ssh.sh /usr/local/lib/jobfinder/deploy-staging-ssh.sh
#   sudo install -o root -g root -m 0755 -D deploy/k8s/deploy.sh              /usr/local/lib/jobfinder/deploy.sh
# and pinned to the deploy key in ~opc/.ssh/authorized_keys:
#   restrict,command="/usr/local/lib/jobfinder/deploy-staging-ssh.sh" ssh-ed25519 AAAA... jobfinder-staging-deploy
#
# The client's command line (SSH_ORIGINAL_COMMAND) must be exactly `sha-<7-40 lowercase hex>`; anything
# else is refused before git or kubectl run. The commit must be on origin/dev and must not be older
# than what staging runs (a late, out-of-order run is skipped). Only data comes from the commit: its
# deploy/k8s manifests and the backend files they read, exported to a temp dir. The installed deploy.sh
# applies them. Residual trust: kubectl here is cluster-admin, so a manifest on dev could still ask
# for more than the namespace (the namespace enforces Pod Security "baseline", which refuses
# privileged and hostPath pods; a namespace-scoped kubeconfig would close the rest).
set -euo pipefail

REPO=${JOBFINDER_REPO:-/home/opc/jobfinder}   # any clone of EhlOps/jobfinder with an `origin` remote
LOCK=${JOBFINDER_DEPLOY_LOCK:-/tmp/jobfinder-deploy-staging.lock}   # staging-restore.sh takes it too
DEPLOY=${JOBFINDER_DEPLOY_SH:-$(dirname "$(readlink -f "$0")")/deploy.sh}
NS=jobfinder-staging

tag=${SSH_ORIGINAL_COMMAND-${1-}}
if [[ ! $tag =~ ^sha-[0-9a-f]{7,40}$ ]]; then
  echo "refused: expected exactly sha-<7-40 lowercase hex>" >&2
  exit 2
fi
sha=${tag#sha-}

# A forced command gets a non-login shell: /etc/profile.d doesn't set KUBECONFIG, and k3s's bundled
# kubectl would otherwise read the root-only /etc/rancher/k3s/k3s.yaml.
if [ -z "${KUBECONFIG:-}" ]; then
  for k in "$HOME/.kube/k3s.yaml" "$HOME/.kube/config"; do
    if [ -r "$k" ]; then export KUBECONFIG=$k; break; fi
  done
fi

# One deploy (or restore) at a time; a second one waits up to 20 minutes.
exec 9>"$LOCK"
flock -w 1200 9 || { echo "another staging deploy or restore is still running" >&2; exit 1; }

# Only touches refs/remotes/origin/dev and the object store: the checkout's files and HEAD stay as they
# are (the compose stack runs from this checkout).
git -C "$REPO" fetch --quiet origin dev
commit=$(git -C "$REPO" rev-parse --verify --quiet "${sha}^{commit}") \
  || { echo "refused: $sha is not a commit in $REPO after fetching dev" >&2; exit 1; }
git -C "$REPO" merge-base --is-ancestor "$commit" refs/remotes/origin/dev \
  || { echo "refused: $commit is not on origin/dev" >&2; exit 1; }

# Runs can finish out of order (images.yml builds only what changed). Never move staging backwards:
# a commit that the deployed one already contains is skipped (exit 0, nothing to do).
deployed=$(kubectl get namespace "$NS" -o jsonpath='{.metadata.annotations.jobfinder\.io/deployed-commit}' 2>/dev/null || true)
if [[ $deployed =~ ^[0-9a-f]{40}$ ]] && [ "$deployed" != "$commit" ] \
   && git -C "$REPO" merge-base --is-ancestor "$commit" "$deployed" 2>/dev/null; then
  echo "skipped: staging already runs $deployed, which contains $commit"
  exit 0
fi

tree=$(mktemp -d)
trap 'rm -rf "$tree"' EXIT
git -C "$REPO" archive "$commit" deploy/k8s backend/kustomization.yaml backend/.env.defaults \
  | tar -x -C "$tree"

# Older images deploy.sh may fall back to, newest first: the commit's last 50 ancestors on dev.
candidates=$(git -C "$REPO" rev-list --first-parent --max-count=50 --skip=1 "$commit" \
  | cut -c1-7 | sed 's/^/sha-/' | tr '\n' ' ')

echo "deploying $tag (commit $commit) to staging"
DEPLOY_K8S_DIR="$tree/deploy/k8s" DEPLOY_COMMIT="$commit" DEPLOY_CANDIDATE_TAGS="$candidates" \
  "$DEPLOY" staging "$tag"
