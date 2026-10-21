#!/usr/bin/env bash
# SSH forced command for deploys from GitHub Actions: staging on dev pushes (#43,
# .github/workflows/deploy-staging.yml) and prod on main pushes (#118, .github/workflows/deploy-prod.yml).
#
#   deploy-ssh.sh <staging|prod>        the client's SSH_ORIGINAL_COMMAND is the tag: sha-<7-40 hex>
#   deploy-ssh.sh <staging|prod> sha-<7-40 hex>     the same, by hand on the host
#
# Installed OUTSIDE the repo, root-owned, together with the deploy.sh it runs, so a push can't change
# what a key executes (docs/k3s.md, "Staging" and "Production", one-time setup):
#   sudo install -o root -g root -m 0755 -D deploy/k8s/deploy-ssh.sh /usr/local/lib/jobfinder/deploy-ssh.sh
#   sudo install -o root -g root -m 0755 -D deploy/k8s/deploy.sh     /usr/local/lib/jobfinder/deploy.sh
# and pinned per environment in ~opc/.ssh/authorized_keys, one key each:
#   restrict,command="/usr/local/lib/jobfinder/deploy-ssh.sh staging" ssh-ed25519 AAAA... jobfinder-staging-deploy
#   restrict,command="/usr/local/lib/jobfinder/deploy-ssh.sh prod"    ssh-ed25519 AAAA... jobfinder-prod-deploy
# The environment comes only from that command line, never from the client.
#
#   staging  branch dev,  namespace jobfinder-staging, lock /tmp/jobfinder-deploy-staging.lock
#            (staging-restore.sh takes it too), smoke Host staging.ehlops.com
#   prod     branch main, namespace jobfinder,         lock /tmp/jobfinder-deploy-prod.lock,
#            smoke Host jobs.ehlops.com
#
# Both environments:
#   1. The client's command line must be exactly `sha-<7-40 lowercase hex>`; anything else is refused
#      (exit 2) before git or kubectl run.
#   2. The commit must be on origin/<branch> and must not be older than what the namespace runs
#      (annotation jobfinder.io/deployed-commit): a late, out-of-order run is skipped (exit 0).
#   3. Only data comes from the commit: its deploy/k8s manifests and the backend files they read,
#      exported to a temp dir. The installed deploy.sh applies them, with older sha- tags it may fall
#      back to (DEPLOY_CANDIDATE_TAGS) for images the commit didn't rebuild.
#   4. Smoke checks through Traefik (127.0.0.1:8280, the env's Host header), retried for ~2 minutes:
#      /readyz 200, / 200, /api/auth/me 401, every app pod Ready, and no app container restarting.
# Prod only (#118):
#   - Staging gate: the release's dev side (<commit>^2 for a dev -> main merge commit, else the commit)
#     must already run on staging, i.e. be an ancestor of (or equal to) staging's deployed-commit.
#   - Records every app Deployment's images and the namespace's deployed-tag/-commit, then takes a
#     pre-deploy backup (a Job from cronjob/backup with BACKUP_PREFIX=pre-); a failed backup stops
#     before anything changes.
#   - Candidates: the dev side's first-parent history (same tree, so an image the merge didn't rebuild
#     comes from the newest dev build), then main's.
#   - A failed rollout or smoke check rolls the images back to the recorded ones (`kubectl set image`),
#     restores the annotations and re-runs the smoke checks; the run exits 1 either way. Migrations
#     are never rolled back (docs/k3s.md, "Rollback"); a failed migration has rolled nothing.
#
# stdout carries exactly one line, `deploy-summary: <JSON>` (env, outcome, tag, commit, dev_commit,
# previous_commit/_tag, images, smoke, backup, started_at, finished_at, message), printed on exit;
# deploy-report.sh in GitHub Actions turns it into PR comments. Everything else goes to stderr.
# Both streams are also written to a per-run log on the host (mode 0600, the newest 50 kept):
#   ~/.local/state/jobfinder/deploy-<env>-<UTC start>-<pid>.log
# A run ignores SIGHUP/SIGPIPE and keeps writing that log when the SSH session dies, so a cancelled
# or disconnected Actions run still finishes its deploy, smoke checks or rollback; read the outcome
# there (the last line is the summary).
# Outcomes: deployed, skipped, refused, backup_failed, migrate_failed, failed, rolled_back,
# rollback_failed.
#
# Test-only overrides (an SSH client can't set them under `restrict`): JOBFINDER_NS, JOBFINDER_BRANCH,
# JOBFINDER_SMOKE_HOST, JOBFINDER_STAGING_NS, JOBFINDER_DEPLOY_LOCK, JOBFINDER_DEPLOY_SH, JOBFINDER_REPO.
#
# Residual trust: kubectl here is cluster-admin, so a manifest on the branch could still ask for more
# than the namespace (the namespace enforces Pod Security "baseline", which refuses privileged and
# hostPath pods; a namespace-scoped kubeconfig would close the rest).
set -euo pipefail

env=${1-}
case $env in
  staging) branch=dev  ns=jobfinder-staging lock=/tmp/jobfinder-deploy-staging.lock host=staging.ehlops.com ;;
  prod)    branch=main ns=jobfinder         lock=/tmp/jobfinder-deploy-prod.lock    host=jobs.ehlops.com ;;
  *) echo "refused: unknown environment '${env}' (the forced command names staging or prod)" >&2; exit 2 ;;
esac
REPO=${JOBFINDER_REPO:-/home/opc/jobfinder}   # any clone of EhlOps/jobfinder with an `origin` remote
DEPLOY=${JOBFINDER_DEPLOY_SH:-$(dirname "$(readlink -f "$0")")/deploy.sh}
NS=${JOBFINDER_NS:-$ns}
BRANCH=${JOBFINDER_BRANCH:-$branch}
LOCK=${JOBFINDER_DEPLOY_LOCK:-$lock}
SMOKE_HOST=${JOBFINDER_SMOKE_HOST:-$host}
STAGING_NS=${JOBFINDER_STAGING_NS:-jobfinder-staging}
# deploy.sh's overlay follows the namespace, so a prod-mode test run against staging
# (JOBFINDER_NS=jobfinder-staging) applies the staging overlay, never prod's.
case $NS in
  jobfinder-staging) overlay=staging ;;
  jobfinder)         overlay=prod ;;
  *)                 overlay='' ;;   # refused below, once the summary line is set up
esac
SMOKE_URL=http://127.0.0.1:8280   # Traefik (deploy/k3s/manifests/traefik-config.yaml), behind Caddy
SMOKE_WINDOW=120                  # seconds the smoke checks may take to pass
SMOKE_SETTLE=30                   # ...and at least this long, so a crash-looping pod shows up
BACKUP_TIMEOUT=900
ROLLOUT_TIMEOUT=600s

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
started_at=$(now)

# A run must finish (rollback, summary) even when the SSH session dies under it: an Actions cancel or
# timeout, or a network drop (#118). HUP and PIPE are ignored (children inherit that), and all output
# goes through tee into a per-run log on the host, which keeps going when the client is gone; a
# write to a dead client is only a warning that tee swallows, never an error here.
trap '' HUP PIPE
LOG_DIR=${XDG_STATE_HOME:-$HOME/.local/state}/jobfinder
mkdir -p -m 0700 "$LOG_DIR"
RUN_LOG=$LOG_DIR/deploy-$env-${started_at//:/}-$$.log
(umask 077 && : > "$RUN_LOG")
# The newest 50 run logs are kept.
find "$LOG_DIR" -maxdepth 1 -name 'deploy-*.log' | sort -r | tail -n +51 | xargs -r rm -f --
# fd 4/5: the client's stdout/stderr. stdout (fd 3) is only for the summary line; deploy.sh's and
# kubectl's output go to stderr.
exec 4>&1 5>&2
exec 3> >(tee -a --output-error=warn "$RUN_LOG" >&4 2>/dev/null)
tee_out=$!
exec 2> >(tee -a --output-error=warn "$RUN_LOG" >&5 2>/dev/null) 1>&2
tee_err=$!

log() { printf '[deploy-ssh %s] %s\n' "$env" "$*" >&2 || true; }
log "run log: $RUN_LOG"

outcome='' message='' tag='' commit=unknown dev_commit=unknown
previous_commit=unknown previous_tag=unknown backup='' tree=''
images_json='{}' smoke_json='[]' rollback_smoke_json=''

# The summary line (#118, contract C1), always the last line on stdout. A run that stops on an
# unexpected error (set -e) still reports, as `failed`.
finish() {
  local rc=$?
  set +e
  [ -z "$tree" ] || rm -rf "$tree"
  if [ -z "$outcome" ]; then
    outcome=failed message="stopped unexpectedly (exit $rc); see the run log"
    [ "$rc" -ne 0 ] || rc=1
  fi
  local summary
  summary=$(jq -nc \
    --arg env "$env" --arg outcome "$outcome" --arg tag "${tag:0:80}" --arg commit "$commit" \
    --arg dev_commit "$dev_commit" --arg previous_commit "$previous_commit" \
    --arg previous_tag "$previous_tag" --argjson images "$images_json" --argjson smoke "$smoke_json" \
    --arg rollback_smoke "$rollback_smoke_json" --arg backup "$backup" \
    --arg started_at "$started_at" --arg finished_at "$(now)" --arg message "$message" \
    '{env: $env, outcome: $outcome, tag: $tag, commit: $commit, dev_commit: $dev_commit,
      previous_commit: $previous_commit, previous_tag: $previous_tag, images: $images,
      smoke: $smoke, backup: (if $backup == "" then null else $backup end),
      started_at: $started_at, finished_at: $finished_at, message: $message}
     + (if $rollback_smoke == "" then {} else {rollback_smoke: ($rollback_smoke | fromjson)} end)') \
    || summary=$(printf '{"env":"%s","outcome":"failed","message":"could not build the summary"}' "$env")
  log "$outcome (exit $rc)"
  # Drain stderr's tee first, so the summary is the last line of the log and of the client's output.
  exec 2>&- 1>&-
  wait "$tee_err" 2>/dev/null
  printf 'deploy-summary: %s\n' "$summary" >&3
  exec 3>&-
  wait "$tee_out" 2>/dev/null
  exit "$rc"
}
trap finish EXIT

# Ends the run: end_run <outcome> <exit code> <message>.
end_run() {
  outcome=$1 message=$3
  log "$1: $3"
  exit "$2"
}

[ -n "$overlay" ] || end_run refused 2 "no overlay deploys namespace '$NS'"
tag=${SSH_ORIGINAL_COMMAND-${2-}}
[[ $tag =~ ^sha-[0-9a-f]{7,40}$ ]] || end_run refused 2 "expected exactly sha-<7-40 lowercase hex>"
sha=${tag#sha-}

# A forced command gets a non-login shell: /etc/profile.d doesn't set KUBECONFIG, and k3s's bundled
# kubectl would otherwise read the root-only /etc/rancher/k3s/k3s.yaml.
if [ -z "${KUBECONFIG:-}" ]; then
  for k in "$HOME/.kube/k3s.yaml" "$HOME/.kube/config"; do
    if [ -r "$k" ]; then export KUBECONFIG=$k; break; fi
  done
fi

# One deploy (or restore) per environment at a time; a second one waits up to 20 minutes.
exec 9>"$LOCK"
flock -w 1200 9 || end_run failed 1 "another $env deploy or restore is still running"

# Only touches refs/remotes/origin/* and the object store: the checkout's files and HEAD stay as they
# are (someone may be working in this checkout). Prod also needs dev, for the staging gate.
fetch=("$BRANCH")
[ "$env" != prod ] || [ "$BRANCH" = dev ] || fetch+=(dev)
git -C "$REPO" fetch --quiet origin "${fetch[@]}" || end_run failed 1 "git fetch origin ${fetch[*]} failed in $REPO"
commit=$(git -C "$REPO" rev-parse --verify --quiet "${sha}^{commit}") \
  || { commit=unknown; end_run refused 1 "$sha is not a commit in $REPO after fetching $BRANCH"; }
git -C "$REPO" merge-base --is-ancestor "$commit" "refs/remotes/origin/$BRANCH" \
  || end_run refused 1 "$commit is not on origin/$BRANCH"
# The dev side of a release (#118): a dev -> main merge commit's second parent, else the commit.
dev_commit=$commit
if [ "$env" = prod ]; then
  dev_commit=$(git -C "$REPO" rev-parse --verify --quiet "${commit}^2") || dev_commit=$commit
fi

ns_annotation() {
  kubectl get namespace "$1" -o jsonpath="{.metadata.annotations.jobfinder\.io/$2}" 2>/dev/null || true
}
previous_commit=$(ns_annotation "$NS" deployed-commit)
previous_tag=$(ns_annotation "$NS" deployed-tag)
[ -n "$previous_commit" ] || previous_commit=unknown
[ -n "$previous_tag" ] || previous_tag=unknown

# Runs can finish out of order (images.yml builds only what changed). Never move an environment
# backwards: a commit that the deployed one already contains is skipped (exit 0, nothing to do).
if [[ $previous_commit =~ ^[0-9a-f]{40}$ ]] && [ "$previous_commit" != "$commit" ] \
   && git -C "$REPO" merge-base --is-ancestor "$commit" "$previous_commit" 2>/dev/null; then
  end_run skipped 0 "$NS already runs $previous_commit, which contains $commit"
fi

prior=''
if [ "$env" = prod ]; then
  # Staging gate (#118): prod only gets what staging already runs. A squash-merged release or a
  # main-only hotfix has no dev side on staging and is refused here.
  staged=$(ns_annotation "$STAGING_NS" deployed-commit)
  if ! [[ $staged =~ ^[0-9a-f]{40}$ ]] \
     || ! git -C "$REPO" merge-base --is-ancestor "$dev_commit" "$staged" 2>/dev/null; then
    end_run refused 1 "not on staging yet: $dev_commit is not in what $STAGING_NS runs (${staged:-nothing})"
  fi
  log "staging gate: $STAGING_NS runs $staged, which contains $dev_commit"

  # Prior state, for the rollback: one `<deployment> <container> <image>` line per container.
  prior=$(kubectl -n "$NS" get deployments -l jobfinder.io/rollout=app -o json \
          | jq -r '.items[] | .metadata.name as $d | .spec.template.spec.containers[]
                   | "\($d) \(.name) \(.image)"')
  log "running before the deploy ($previous_tag):"$'\n'"$prior"
fi

# sha-<7> tags of a first-parent history, newest first: sha_tags <rev-list options> <commit>.
sha_tags() {
  git -C "$REPO" rev-list --first-parent "$@" | cut -c1-7 | sed 's/^/sha-/' | tr '\n' ' '
}

tree=$(mktemp -d)
git -C "$REPO" archive "$commit" deploy/k8s backend/kustomization.yaml backend/.env.defaults \
  | tar -x -C "$tree"

if [ "$env" = prod ]; then
  # Pre-deploy backup (#118): its own pool (pre-db-<ts>.dump, pre-files-<ts>.tar.gz, the newest
  # BACKUP_PRE_KEEP kept by the CronJob's script). A failure stops before anything changes.
  job=backup-pre-${commit:0:7}
  kubectl -n "$NS" delete job "$job" --ignore-not-found --wait=true >/dev/null
  kubectl -n "$NS" create job --from=cronjob/backup "$job" --dry-run=client -o yaml \
    | kubectl set env --local -f - BACKUP_PREFIX=pre- -o yaml \
    | kubectl -n "$NS" apply -f - >/dev/null \
    || end_run backup_failed 1 "could not start the pre-deploy backup Job $job; nothing deployed"
  log "pre-deploy backup: Job $job (up to ${BACKUP_TIMEOUT}s)"
  deadline=$((SECONDS + BACKUP_TIMEOUT)) status=''
  while [ $SECONDS -lt $deadline ]; do
    status=$(kubectl -n "$NS" get job "$job" \
      -o jsonpath='{range .status.conditions[?(@.status=="True")]}{.type}{" "}{end}' 2>/dev/null || true)
    case " $status " in *" Complete "*|*" Failed "*) break ;; esac
    sleep 5
  done
  # Every attempt's pod (backoffLimit 1): `logs job/...` would show only one of them.
  joblog=$(kubectl -n "$NS" logs -l "batch.kubernetes.io/job-name=$job" --tail=200 2>/dev/null || true)
  if [[ " $status " != *" Complete "* ]]; then
    printf '%s\n' "$joblog" | tail -n 50 >&2
    end_run backup_failed 1 "pre-deploy backup Job $job did not complete (${status:-timed out}); nothing deployed"
  fi
  # "backup ok: /backups/<db file> (<size>) /backups/<files file> (<size>)"
  backup=$(sed -n 's|^backup ok: /backups/\([^ ]*\) .*|\1|p' <<<"$joblog" | tail -n1)
  kubectl -n "$NS" delete job "$job" --wait=false >/dev/null || true
  [ -n "$backup" ] || end_run backup_failed 1 "pre-deploy backup Job $job printed no 'backup ok' line; nothing deployed"
  log "pre-deploy backup: $backup"

  # Older images deploy.sh may fall back to, newest first: the dev side's first-parent history
  # (including itself), then main's.
  candidates="$(sha_tags --max-count=50 "$dev_commit") $(sha_tags --max-count=20 --skip=1 "$commit")"
else
  # Older images deploy.sh may fall back to, newest first: the commit's last 50 ancestors on dev.
  candidates=$(sha_tags --max-count=50 --skip=1 "$commit")
fi

# The pods of the app Deployments (they carry each Deployment's selector, not the rollout label),
# not terminating and not finished: [{name, ready, restarts}].
app_pods() {
  { kubectl -n "$NS" get deployments -l jobfinder.io/rollout=app -o json \
    && kubectl -n "$NS" get pods -o json; } | jq -cs '
    [.[0].items[].spec.selector.matchLabels // {}] as $sels
    | [.[1].items[]
       | select(.metadata.deletionTimestamp == null)
       | select(.status.phase != "Succeeded" and .status.phase != "Failed")
       | . as $pod
       | select(any($sels[]; to_entries | length > 0
                              and all(.[]; $pod.metadata.labels[.key] == .value)))
       | {name: .metadata.name,
          ready: any(.status.conditions[]?; .type == "Ready" and .status == "True"),
          restarts: ([.status.containerStatuses[]?.restartCount] | add // 0)}]'
}

# Smoke checks (#118); sets smoke_json, returns 0 when all pass. Retried for SMOKE_WINDOW seconds
# and watched for at least SMOKE_SETTLE; a restart since the start fails at once.
smoke() {
  local start=$SECONDS base pods checks spec path want got ok all restarted
  base=$(app_pods) || base='[]'
  while :; do
    checks='' all=true
    for spec in '/readyz 200' '/ 200' '/api/auth/me 401'; do
      path=${spec% *} want=${spec#* }
      got=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -H "Host: $SMOKE_HOST" "$SMOKE_URL$path" || true)
      [[ $got =~ ^[0-9]{3}$ ]] || got=000
      ok=false; [ "$got" = "$want" ] && ok=true
      $ok || all=false
      checks+=$(jq -nc --arg c "$path" --argjson w "$want" --argjson g "$((10#$got))" --argjson ok "$ok" \
                '{check: $c, want: $w, got: $g, ok: $ok}')$'\n'
    done
    if pods=$(app_pods); then
      checks+=$(jq -c '[.[] | select(.ready | not) | .name] as $n
                        | {check: "pods-ready", ok: ($n == [])}
                          + (if $n == [] then {} else {detail: ("not ready: " + ($n | join(", ")))} end)' \
                <<<"$pods")$'\n'
      restarted=$(jq -r --argjson base "$base" \
        '[.[] | . as $p | select(.restarts > ([$base[] | select(.name == $p.name) | .restarts] | first // 0))
         | .name] | join(", ")' <<<"$pods")
    else
      checks+='{"check":"pods-ready","ok":false,"detail":"kubectl get pods failed"}'$'\n'
      restarted=''
    fi
    jq -e 'select(.check == "pods-ready") | .ok' <<<"$checks" >/dev/null || all=false
    checks+=$(jq -nc --arg r "$restarted" \
              '{check: "no-restarts", ok: ($r == "")} + (if $r == "" then {} else {detail: ("restarted: " + $r)} end)')
    smoke_json=$(jq -sc . <<<"$checks")
    if [ -n "$restarted" ]; then
      log "smoke: failed, restarted: $restarted"; return 1
    fi
    if $all && [ $((SECONDS - start)) -ge $SMOKE_SETTLE ]; then
      log "smoke: ok ($(jq -r 'map("\(.check)=\(.got // .ok)") | join(" ")' <<<"$smoke_json"))"; return 0
    fi
    if [ $((SECONDS - start)) -ge $SMOKE_WINDOW ]; then
      log "smoke: failed after ${SMOKE_WINDOW}s ($(jq -r 'map(select(.ok | not) | "\(.check)=\(.got // .detail)") | join(" ")' <<<"$smoke_json"))"
      return 1
    fi
    sleep 5
  done
}

# Puts every recorded Deployment/container back on its recorded image and the namespace annotations
# back as they were; returns non-zero when a rollout didn't finish.
rollback() {
  local d c img rc=0 deps args
  deps=$(cut -d' ' -f1 <<<"$prior" | sort -u)
  for d in $deps; do
    args=()
    while read -r name c img; do [ "$name" != "$d" ] || args+=("$c=$img"); done <<<"$prior"
    kubectl -n "$NS" set image "deployment/$d" "${args[@]}" >/dev/null || rc=1
  done
  for d in $deps; do
    kubectl -n "$NS" rollout status "deployment/$d" --timeout="$ROLLOUT_TIMEOUT" || rc=1
  done
  local ann=()
  if [ "$previous_tag" = unknown ]; then ann+=(jobfinder.io/deployed-tag-); else ann+=("jobfinder.io/deployed-tag=$previous_tag"); fi
  if [ "$previous_commit" = unknown ]; then ann+=(jobfinder.io/deployed-commit-); else ann+=("jobfinder.io/deployed-commit=$previous_commit"); fi
  kubectl annotate namespace "$NS" --overwrite "${ann[@]}" >/dev/null || rc=1
  return $rc
}

pins="$tree/pins"
# deploy.sh replaces the migrate Job; one with another UID afterwards was created by this run.
old_migrate=$(kubectl -n "$NS" get job migrate --ignore-not-found -o jsonpath='{.metadata.uid}')
log "deploying $tag (commit $commit) to $NS"
rc=0
DEPLOY_K8S_DIR="$tree/deploy/k8s" DEPLOY_COMMIT="$commit" DEPLOY_CANDIDATE_TAGS="$candidates" \
  DEPLOY_PINS_OUT="$pins" "$DEPLOY" "$overlay" "$tag" || rc=$?

failure=''
if [ $rc -ne 0 ]; then
  # Did deploy.sh get past the migration? Only a migrate Job created by this run counts.
  mstatus=$(kubectl -n "$NS" get job migrate --ignore-not-found \
    -o jsonpath='{.metadata.uid}{" "}{range .status.conditions[?(@.status=="True")]}{.type}{" "}{end}')
  muid=${mstatus%% *}
  if [ -z "$muid" ] || [ "$muid" = "$old_migrate" ]; then
    end_run failed 1 "deploy.sh failed (exit $rc) before the migrate Job ran; no Deployment was touched"
  fi
  [[ " $mstatus " == *" Complete "* ]] \
    || end_run migrate_failed 1 "migration failed; no Deployment was rolled (see the migrate Job's log)"
  failure="rollout failed (deploy.sh exit $rc)"
fi
[ -s "$pins" ] && images_json=$(jq -Rnc '[inputs | select(length > 0) | split(" ") | {(.[0]): .[1]}] | add // {}' < "$pins")

if [ -z "$failure" ]; then
  smoke && end_run deployed 0 "deployed $tag to $NS; smoke checks passed"
  failure="smoke checks failed"
fi
[ "$env" = prod ] || end_run failed 1 "$failure; staging is not rolled back"

if [ -z "$prior" ]; then
  end_run rollback_failed 1 "$failure; nothing was recorded to roll back to: manual action needed (docs/k3s.md, \"Rollback\")"
fi
log "$failure; rolling the images back to $previous_tag"
saved_smoke=$smoke_json
if rollback; then
  healthy=false; smoke && healthy=true
  rollback_smoke_json=$smoke_json smoke_json=$saved_smoke
  if $healthy; then
    end_run rolled_back 1 "$failure; rolled back to $previous_tag ($previous_commit), smoke checks pass again"
  fi
  why="the rollback to $previous_tag is unhealthy"
else
  why="the rollback to $previous_tag did not finish"
fi
log "the images before the deploy were:"$'\n'"$prior"
end_run rollback_failed 1 "$failure; $why: manual action needed (docs/k3s.md, \"Rollback\"; pre-deploy backup $backup)"
