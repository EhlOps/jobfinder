#!/usr/bin/env bash
# Sourced by deploy-summary.sh and deploy-report.sh: `dev_side <commit>` prints a release's dev side,
# as deploy/k8s/deploy-ssh.sh's copy finds it on the host. A dev -> main merge commit's is its second
# parent. A squash release's is the dev commit with the same tree: the oldest of the newest run of
# such commits in origin/dev's first-parent history (skipping sync-dev's merge of main on top). Else
# the commit itself. Needs origin/dev in the checkout (fetch-depth: 0).
dev_side() {
  local tree d
  if d=$(git rev-parse -q --verify "$1^2" 2>/dev/null); then echo "$d"; return; fi
  tree=$(git rev-parse -q --verify "$1^{tree}" 2>/dev/null) || { echo "$1"; return; }
  d=$(git log --first-parent --max-count=200 --format='%H %T' refs/remotes/origin/dev 2>/dev/null \
      | awk -v t="$tree" '$2 == t {d = $1; next} d != "" {exit} END {print d}')
  echo "${d:-$1}"
}
