#!/usr/bin/env bash
# Reports a staging or prod deploy run (#118): the deploy workflows run it after the SSH step, with
# the host's `deploy-summary:` JSON (contract C1, printed by deploy/k8s/deploy-ssh.sh) saved to a file.
#
#   .github/scripts/deploy-report.sh summary.json
#
# Inputs (env): GH_TOKEN, GITHUB_REPOSITORY, GITHUB_SERVER_URL, GITHUB_RUN_ID, GITHUB_STEP_SUMMARY
# (optional), DRY_RUN=1 to print the comments instead of posting them. It needs a full-history
# checkout (fetch-depth: 0) to list the shipped PRs.
#
# Outputs:
#   - a table in $GITHUB_STEP_SUMMARY, for every outcome;
#   - on `deployed`, a one-line comment on each shipped PR: the `(#N)` subjects of
#     `git log --first-parent <previous dev side>..<dev_commit>` (titles from those subjects; the
#     previous dev side is prod's `previous_commit^2`, or staging's `previous_commit`; with none,
#     only dev_commit's own PR; at most 50);
#   - for prod (any outcome but `skipped`), one comment on the release PR (the PR that merged
#     `commit` into main), with the outcome, pinned images, smoke checks, backup and shipped PRs.
# A failed lookup or comment logs ::warning:: and the script carries on; it exits non-zero only when
# the summary file is unreadable, so it never masks the deploy step's own result.
set -euo pipefail

file=${1:?usage: deploy-report.sh <summary.json>}
if ! jq -e 'type == "object"' "$file" >/dev/null 2>&1; then
  echo "::error::$file is not a JSON object"
  exit 1
fi

repo=${GITHUB_REPOSITORY:?}
run_url="${GITHUB_SERVER_URL:-https://github.com}/$repo/actions/runs/${GITHUB_RUN_ID:-0}"
summary_out=${GITHUB_STEP_SUMMARY:-/dev/null}
max_prs=50

field() { jq -r --arg k "$1" '.[$k] // empty | tostring' "$file"; }
env_name=$(field env); env_name=${env_name:-unknown}
outcome=$(field outcome); outcome=${outcome:-failed}
tag=$(field tag); tag=${tag:-unknown}
commit=$(field commit)
dev_commit=$(field dev_commit); dev_commit=${dev_commit:-$commit}
previous_commit=$(field previous_commit)
previous_tag=$(field previous_tag); previous_tag=${previous_tag:-unknown}
backup=$(field backup)
# One line, whatever the host sent: it goes into a table row and a quote.
message=$(field message); message=${message//$'\r'/}; message=${message//$'\n'/ }
finished_at=$(field finished_at)

# "2026-10-21T14:03:12Z" -> "2026-10-21 14:03 UTC"; the run's own clock if the host gave none.
when=$(date -u -d "${finished_at:-now}" '+%Y-%m-%d %H:%M UTC' 2>/dev/null || date -u '+%Y-%m-%d %H:%M UTC')

# A commit we can resolve in the local history, or nothing.
resolve() {
  [[ ${1:-} =~ ^[0-9a-f]{7,40}$ ]] || return 1
  git rev-parse -q --verify "$1^{commit}" 2>/dev/null
}
# The PR number of a squash-merge subject: the LAST "(#N)" (older ones carry two).
pr_of_subject() { grep -oE '\(#[0-9]+\)' <<<"$1" | tail -n1 | tr -dc '0-9' || true; }

# --- shipped PRs --------------------------------------------------------------------------------
# Titles come from the squash-merge subjects (the PR title plus its "(#N)" refs), so no API calls.
prs=()
declare -A titles=()
dev_full=$(resolve "$dev_commit" || true)
if [ -n "$dev_full" ]; then
  base=""
  if prev_full=$(resolve "$previous_commit"); then
    base=$prev_full
    # Prod's previous commit is a release merge, whose dev side is its second parent. Staging's is a
    # dev commit, which may itself be a merge (review-fix merges), so it stays as it is.
    if [ "$env_name" = prod ]; then
      base=$(git rev-parse -q --verify "$prev_full^2" 2>/dev/null || echo "$prev_full")
    fi
  fi
  if [ -n "$base" ]; then
    subjects=$(git log --first-parent --format=%s "$base..$dev_full")
  else
    subjects=$(git log -1 --format=%s "$dev_full")
  fi
  while IFS= read -r s; do
    n=$(pr_of_subject "$s")
    if [ -n "$n" ] && [ -z "${titles[$n]+x}" ]; then
      prs+=("$n")
      t=$(sed -E 's/( \(#[0-9]+\))+$//' <<<"$s")
      titles[$n]=${t:-"(no title)"}
    fi
  done <<<"$subjects"
else
  echo "::warning::dev commit '$dev_commit' is not in the checkout; no shipped PRs listed"
fi
total_prs=${#prs[@]}
shown=("${prs[@]:0:$max_prs}")
more=$((total_prs - ${#shown[@]}))

pr_list() {
  local n
  for n in "${shown[@]}"; do printf -- '- #%s %s\n' "$n" "${titles[$n]}"; done
  if [ "$more" -gt 0 ]; then printf -- '- and %s more\n' "$more"; fi
  if [ "$total_prs" -eq 0 ]; then echo "- (none found)"; fi
}

# --- release PR (prod) --------------------------------------------------------------------------
release_pr=""
if [ "$env_name" = prod ] && [ "$outcome" != skipped ]; then
  if [[ $commit =~ ^[0-9a-f]{40}$ ]]; then
    release_pr=$(gh api "repos/$repo/commits/$commit/pulls" \
      --jq '[.[] | select(.base.ref == "main")][0].number // empty' 2>/dev/null) || release_pr=""
    [[ $release_pr =~ ^[0-9]+$ ]] || release_pr=""
  fi
  if [ -z "$release_pr" ]; then
    echo "::warning::no release PR found for commit '${commit:-unknown}'; skipping its comment"
  fi
fi

# --- tables -------------------------------------------------------------------------------------
images_table() {
  jq -r '(.images // {}) | to_entries | if length == 0 then "_none deployed_"
    else "| Image | Pinned |\n|---|---|\n" +
      (map("| \(.key) | " + (.value | tostring | split("|") | map("`\(.)`") | join(" @ ")) + " |") | join("\n")) end' "$file"
}
smoke_table() {
  jq -r '(.smoke // []) | if length == 0 then "_no smoke checks ran_"
    else "| Check | Want | Got | OK |\n|---|---|---|---|\n" + (map("| \(.check) | \(.want // "") | \(.got // "") | \(if .ok then "yes" else "**no**" end) |") | join("\n")) end' "$file"
}
short() { printf '%s' "${1:0:7}"; }

headline="**${env_name}** deploy: **${outcome}** · ${tag} · ${when} · [run](${run_url})"
{
  echo "### Deploy to ${env_name}: ${outcome}"
  echo
  echo "| | |"
  echo "|---|---|"
  echo "| Tag | ${tag} |"
  echo "| Commit | \`$(short "${commit:-unknown}")\` (dev side \`$(short "${dev_commit:-unknown}")\`) |"
  echo "| Previous | ${previous_tag} (\`$(short "${previous_commit:-unknown}")\`) |"
  echo "| Backup | ${backup:-none} |"
  echo "| Finished | ${when} |"
  if [ -n "$release_pr" ]; then echo "| Release PR | #${release_pr} |"; fi
  echo "| Message | ${message//|/\\|} |"
  echo
  images_table
  echo
  smoke_table
  echo
  echo "**PRs in this deploy (${total_prs})**"
  pr_list
} >>"$summary_out"

# --- comments -----------------------------------------------------------------------------------
post() {  # post <pr number> <body>
  if [ "${DRY_RUN:-0}" = 1 ]; then
    printf -- '--- comment on #%s ---\n%s\n' "$1" "$2"
    return 0
  fi
  if ! gh pr comment "$1" --repo "$repo" --body-file - <<<"$2" >/dev/null; then
    echo "::warning::could not comment on #$1"
  fi
}

if [ "$outcome" = skipped ]; then
  echo "Outcome skipped: step summary only"
  exit 0
fi

if [ "$env_name" = prod ] && [ -n "$release_pr" ]; then
  body=$(
    echo "$headline"
    echo
    if [ -n "$message" ]; then echo "> ${message}"; echo; fi
    echo "Commit \`$(short "$commit")\` (dev side \`$(short "$dev_commit")\`) · previous ${previous_tag} · backup ${backup:-none}"
    echo
    images_table
    echo
    smoke_table
    echo
    if [ "$outcome" = deployed ]; then
      echo "**Shipped (${total_prs})**"
    else
      echo "**In this release, not deployed (${total_prs})**"
    fi
    pr_list
  )
  post "$release_pr" "$body"
fi

if [ "$outcome" = deployed ]; then
  if [ "$env_name" = prod ]; then
    where="Released to **prod**${release_pr:+ in #$release_pr}"
  else
    where="Deployed to **${env_name}**"
  fi
  # Only the listed PRs, so a runaway range can't spam hundreds of comments.
  for n in "${shown[@]}"; do
    [ "$n" = "$release_pr" ] && continue
    post "$n" "${where} · ${tag} · ${when} · [run](${run_url})"
  done
fi
