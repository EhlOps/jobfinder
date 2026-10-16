#!/usr/bin/env bash
# SSH forced command for staging deploys from GitHub Actions (#43, .github/workflows/deploy-staging.yml).
#
# Installed OUTSIDE the repo, root-owned, so a push can't change what the key is allowed to run:
#   sudo install -o root -g root -m 0755 deploy/k8s/deploy-staging-ssh.sh /usr/local/bin/jobfinder-deploy-staging
# and pinned to the deploy key in ~opc/.ssh/authorized_keys:
#   restrict,command="/usr/local/bin/jobfinder-deploy-staging" ssh-ed25519 AAAA... jobfinder-staging-deploy
#
# The client's command line (SSH_ORIGINAL_COMMAND) must be exactly `sha-<7-40 lowercase hex>`; anything
# else is refused before git or kubectl run. The commit must be on origin/dev. Its deploy/k8s and the
# backend files the manifests read are exported to a temp dir, and that commit's deploy.sh deploys
# the overlay `staging` from there, so manifests and images always come from the same commit.
# Trust: whoever can push to dev can already ship images to staging; running that commit's deploy.sh
# as this user adds no access beyond kubectl, which deploy.sh needs anyway.
set -euo pipefail

REPO=${JOBFINDER_REPO:-/home/opc/jobfinder}   # any clone of EhlOps/jobfinder with an `origin` remote
LOCK=${JOBFINDER_DEPLOY_LOCK:-/tmp/jobfinder-deploy-staging.lock}

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

# One deploy at a time; a second one waits up to 20 minutes.
exec 9>"$LOCK"
flock -w 1200 9 || { echo "another staging deploy is still running" >&2; exit 1; }

# Only touches refs/remotes/origin/dev and the object store: the checkout's files and HEAD stay as they
# are (the compose stack runs from this checkout).
git -C "$REPO" fetch --quiet origin dev
commit=$(git -C "$REPO" rev-parse --verify --quiet "${sha}^{commit}") \
  || { echo "refused: $sha is not a commit in $REPO after fetching dev" >&2; exit 1; }
git -C "$REPO" merge-base --is-ancestor "$commit" refs/remotes/origin/dev \
  || { echo "refused: $commit is not on origin/dev" >&2; exit 1; }

tree=$(mktemp -d)
trap 'rm -rf "$tree"' EXIT
git -C "$REPO" archive "$commit" deploy/k8s backend/kustomization.yaml backend/.env.defaults \
  | tar -x -C "$tree"

echo "deploying $tag (commit $commit) to staging"
"$tree/deploy/k8s/deploy.sh" staging "$tag"
