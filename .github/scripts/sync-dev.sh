#!/usr/bin/env bash
# Merges main back into dev after a release (sync-dev.yml), so main is always in dev's history and the
# next dev -> main PR shows only new work. Without it, a squash release forks main away from dev and
# every later release PR conflicts.
#   - main already in dev: nothing to do.
#   - main's tree matches a recent dev commit (every release, squash or merge commit): main has nothing
#     dev lacks, so the merge keeps dev's files as they are (-s ours).
#   - anything else (a change made only on main): a normal merge; a conflict fails the run for a human.
# The push uses the workflow's GITHUB_TOKEN, which starts no workflows: dev's files don't change, so
# staging has nothing to deploy. Run from a full-history checkout of dev.
set -euo pipefail

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

for attempt in 1 2 3; do
  git fetch --quiet origin main dev
  git checkout --quiet -B dev origin/dev
  if git merge-base --is-ancestor origin/main origin/dev; then
    echo "dev already contains main ($(git rev-parse --short origin/main))"
    exit 0
  fi
  main_tree=$(git rev-parse 'origin/main^{tree}')
  if git log --first-parent --max-count=200 --format=%T origin/dev | grep -qx "$main_tree"; then
    git merge --quiet -s ours --no-edit -m "Sync main into dev after release ($(git rev-parse --short origin/main))" origin/main
  elif ! git merge --quiet --no-edit -m "Sync main into dev ($(git rev-parse --short origin/main))" origin/main; then
    echo "::error::main has changes that conflict with dev; merge main into dev by hand"
    exit 1
  fi
  if git push --quiet origin HEAD:dev; then
    echo "dev synced with main: $(git rev-parse --short HEAD)"
    exit 0
  fi
  echo "::warning::dev moved during the sync (attempt $attempt); retrying"
done
echo "::error::could not push the sync to dev after 3 attempts"
exit 1
