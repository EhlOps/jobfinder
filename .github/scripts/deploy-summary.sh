#!/usr/bin/env bash
# Pulls the host's summary out of a deploy run's log (#118): deploy/k8s/deploy-ssh.sh ends every run
# with one `deploy-summary: <JSON>` line (contract C1). Prints that JSON on stdout. When the log has
# no valid line, it prints a minimal summary instead, so deploy-report.sh still runs: outcome
# `failed` when the SSH step failed (ssh never connected, the host died mid-run), `unknown` when it
# succeeded (a host script that predates the summary line).
#
#   DEPLOY_ENV=staging|prod TAG=sha-<7> SHA=<commit> SSH_OUTCOME=<steps.deploy.outcome> \
#     .github/scripts/deploy-summary.sh deploy.log
set -euo pipefail
# shellcheck source=dev-side.sh
. "$(dirname "$0")/dev-side.sh"

log=${1:?usage: deploy-summary.sh <deploy.log>}
line=$(grep -a '^deploy-summary: ' "$log" 2>/dev/null | tail -n1 || true)
json=${line#deploy-summary: }
if [ -n "$line" ] && jq -e 'type == "object"' >/dev/null 2>&1 <<<"$json"; then
  jq -c . <<<"$json"
  exit 0
fi

outcome=failed
if [ "${SSH_OUTCOME:-}" = success ]; then outcome=unknown; fi
last=$(grep -av '^[[:space:]]*$' "$log" 2>/dev/null | tail -n1 || true)
echo "::warning::no deploy-summary line in the deploy output; reporting the run as $outcome" >&2
dev=${SHA:-}
# A release's dev side, as on the host (dev-side.sh); a staging commit is its own.
if [ "${DEPLOY_ENV:-}" = prod ] && [ -n "$dev" ]; then
  dev=$(dev_side "$dev")
fi
jq -nc --arg env "${DEPLOY_ENV:-unknown}" --arg outcome "$outcome" --arg tag "${TAG:-unknown}" \
  --arg commit "${SHA:-}" --arg dev "$dev" \
  --arg msg "no deploy-summary from the host${last:+: ${last:0:300}}" \
  --arg now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  '{env: $env, outcome: $outcome, tag: $tag, commit: $commit, dev_commit: $dev,
    previous_commit: "unknown", previous_tag: "unknown", images: {}, smoke: [], backup: null,
    started_at: $now, finished_at: $now, message: $msg}'
