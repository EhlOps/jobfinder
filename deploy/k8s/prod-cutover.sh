#!/usr/bin/env bash
# Moves production's data from the compose stack into k3s and deploys the app there (#47):
#
#   deploy/k8s/prod-cutover.sh --tag sha-<commit> [--yes]                                  # the cutover
#   deploy/k8s/prod-cutover.sh --namespace jobfinder-staging --rehearsal --tag sha-<commit> # rehearsal
#
#   1. preflight     compose db running, writers (compose api/worker) stopped, the images exist
#   2. data tier     applies only the overlay's objects labelled jobfinder.io/tier=data (namespace,
#                    SealedSecrets, postgres, valkey, PVCs, backup CronJob) and waits for them
#   3. scale down    api/worker/renderer in the namespace, if they exist, so nothing writes
#   4. dump          pg_dump -Fc from the compose container to a file (counted before and after)
#   5. restore       drops and recreates the k3s database, pg_restore --no-owner --no-privileges
#   6. row counts    every table, compose vs k3s; any mismatch stops here
#   7. files         compose volumes <project>_store and <project>_claude-auth -> PVCs store and
#                    claude-auth through a temporary helper pod, checksums compared
#   8. deploy        deploy.sh <overlay> <tag>: the migrate Job (0018 -> head), then the rollout
#   9. smoke         /healthz, / and /api/auth/me through Traefik with the environment's Host header
#
# Every step can be re-run: the script starts over from step 1 and replaces what it copied. It never
# stops or starts compose and never edits host Caddy; the runbook (docs/k3s.md, "Production cutover")
# does those by hand around it.
#
# --rehearsal runs the same steps against jobfinder-staging and deploys the staging overlay (its own
# secrets, mailpit, host staging.ehlops.com). It doesn't require compose's writers to be stopped, so
# tables written while pg_dump ran may differ from the counts afterwards; those are flagged, not failed.
# The real run refuses a namespace that deploy.sh has already deployed (annotation
# jobfinder.io/deployed-tag): restoring then would overwrite live prod data with compose's.
#
# Needs kubectl (KUBECONFIG for the cluster), docker, flock, curl and git (the tag's ancestors become
# deploy.sh's DEPLOY_CANDIDATE_TAGS, so images a commit didn't rebuild resolve to older builds).
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=deploy/k8s/pg-lib.sh
. "$here/pg-lib.sh"

usage() {
  sed -n '2,5p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

NS=jobfinder TAG="" REHEARSAL=false YES=false
while [ $# -gt 0 ]; do
  case $1 in
    --namespace) NS=${2:?}; shift 2 ;;
    --namespace=*) NS=${1#*=}; shift ;;
    --tag) TAG=${2:?}; shift 2 ;;
    --tag=*) TAG=${1#*=}; shift ;;
    --rehearsal) REHEARSAL=true; shift ;;
    --yes) YES=true; shift ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done
[[ $TAG =~ ^sha-[0-9a-f]{7,40}$ ]] || { echo "--tag sha-<7-40 hex> is required" >&2; usage; }

case $NS in
  jobfinder)         overlay=prod    host=jobs.ehlops.com    lock=/tmp/jobfinder-deploy-prod.lock ;;
  jobfinder-staging) overlay=staging host=staging.ehlops.com lock=${JOBFINDER_DEPLOY_LOCK:-/tmp/jobfinder-deploy-staging.lock} ;;
  *) echo "--namespace must be jobfinder or jobfinder-staging" >&2; exit 2 ;;
esac
if $REHEARSAL; then
  [ "$overlay" = staging ] || { echo "--rehearsal runs against jobfinder-staging only" >&2; exit 2; }
else
  [ "$overlay" = prod ] || { echo "jobfinder-staging is only a rehearsal: add --rehearsal" >&2; exit 2; }
fi

SRC=${SRC_CONTAINER:-jobfinder-db-1}
PROJECT=${COMPOSE_PROJECT:-jobfinder}
# Every compose service that writes to the database, the task platform's (profile "ledger", #48) too.
WRITERS=("${PROJECT}-api-1" "${PROJECT}-worker-1" "${PROJECT}-renderer-1"
         "${PROJECT}-scheduler-1" "${PROJECT}-worker-ai-1" "${PROJECT}-worker-io-1")
# The compose db image: already on the host, and it has tar, find and md5sum.
TOOL_IMAGE=${TOOL_IMAGE:-postgres:16}
DUMP_DIR=${CUTOVER_DUMP_DIR:-}   # default: a fresh mktemp -d (step 4)
TRAEFIK=${TRAEFIK_URL:-http://127.0.0.1:8280}
# Every app Deployment that touches the database; the task platform's three (#48) are at 0 where
# TASKS_BACKEND=legacy, and scaled to 0 and back like the rest.
APP_DEPLOYMENTS=(api worker renderer scheduler worker-ai worker-io)
HELPER=cutover-files

log() { printf '[cutover %s %s] %s\n' "$NS" "$(date -u +%H:%M:%S)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

timings=()
step_name="" step_start=0
step() {
  [ -z "$step_name" ] || timings+=("$(printf '%-12s %4ss' "$step_name" $((SECONDS - step_start)))")
  step_name=$1 step_start=$SECONDS
  [ "$1" = finished ] || log "== step: $1"
}
total_start=$SECONDS

work=$(mktemp -d)
cleanup() {
  local rc=$?
  kubectl -n "$NS" delete pod "$HELPER" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  rm -rf "$work"
  [ -z "${dump:-}" ] || rm -f "$dump.part"
  [ -z "${restore_on_exit:-}" ] || restore_replicas
  if [ $rc -ne 0 ]; then
    if [ "$step_name" = smoke ]; then
      log "FAILED in the smoke step: deploy.sh has finished, so a re-run of the real cutover is refused. Check by hand (docs/k3s.md, \"Production cutover\", step 4); redeploy with deploy.sh $overlay $TAG"
    else
      log "FAILED in step '$step_name' after $((SECONDS - total_start))s; fix and re-run (every step repeats safely)"
    fi
    [ -z "${app_left_down:-}" ] || log "the app in $NS stays scaled to 0: its database was replaced and isn't migrated yet"
  fi
}
trap cleanup EXIT

# --- 1. preflight -----------------------------------------------------------------------------------
step preflight
for c in kubectl docker flock curl git; do command -v "$c" >/dev/null || die "$c not found"; done
exec 9>"$lock"
flock -n 9 || die "another deploy or restore holds $lock"
kubectl version >/dev/null || die "kubectl can't reach the cluster (KUBECONFIG?)"

[ "$(docker inspect -f '{{.State.Running}}' "$SRC" 2>/dev/null)" = true ] || die "compose container $SRC is not running"
for v in store claude-auth; do
  docker volume inspect "${PROJECT}_$v" >/dev/null 2>&1 || die "compose volume ${PROJECT}_$v not found"
done
# shellcheck disable=SC2016  # expanded inside the container
src_rev=$(docker exec "$SRC" sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -XAtc "select version_num from alembic_version"')
log "compose database at alembic revision $src_rev"

for w in "${WRITERS[@]}"; do
  if [ "$(docker inspect -f '{{.State.Running}}' "$w" 2>/dev/null)" = true ]; then
    $REHEARSAL || die "compose $w is running: stop the writers first (docker compose stop worker api)"
    log "rehearsal: compose $w still runs, so this copy is a snapshot of a live database"
  fi
done

if ! $REHEARSAL && kubectl get namespace "$NS" >/dev/null 2>&1; then
  deployed=$(kubectl get namespace "$NS" -o jsonpath='{.metadata.annotations.jobfinder\.io/deployed-tag}')
  [ -z "$deployed" ] || die "$NS already runs $deployed (deploy.sh finished there): refusing to overwrite live data with compose's. Deploy with deploy.sh prod <tag> instead."
fi

# The tag's commit and ancestors, for deploy.sh (images.yml only rebuilds what changed).
repo=$(git -C "$here" rev-parse --show-toplevel)
git -C "$repo" fetch --quiet origin 2>/dev/null || log "WARNING: git fetch failed; using local refs"
DEPLOY_COMMIT=unknown DEPLOY_CANDIDATE_TAGS=""
if commit=$(git -C "$repo" rev-parse --verify --quiet "${TAG#sha-}^{commit}"); then
  DEPLOY_COMMIT=$commit
  DEPLOY_CANDIDATE_TAGS=$(git -C "$repo" rev-list --first-parent --max-count=50 --skip=1 "$commit" \
    | cut -c1-7 | sed 's/^/sha-/' | tr '\n' ' ')
else
  log "WARNING: ${TAG#sha-} is not a commit in $repo; deploy.sh gets no older candidate tags"
fi

# Every image must resolve now, not after the data copy (same lookup order as deploy.sh: the tag, then
# its ancestors; what the namespace already runs is not relied on here).
ghcr_has() {
  local repo="ehlops/jobfinder-$1" code
  if [ "${token_for:-}" != "$1" ]; then   # one token per image
    token=$(curl -fsS --max-time 20 "https://ghcr.io/token?scope=repository:${repo}:pull" \
      | sed -n 's/.*"token":"\([^"]*\)".*/\1/p') || die "ghcr.io token request failed"
    [ -n "$token" ] || die "no token from ghcr.io for $repo"
    token_for=$1
  fi
  code=$(curl -sS --max-time 20 -o /dev/null -I -w '%{http_code}' -H "Authorization: Bearer $token" \
    -H "Accept: application/vnd.oci.image.index.v1+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.docker.distribution.manifest.v2+json" \
    "https://ghcr.io/v2/${repo}/manifests/$2") || die "ghcr.io unreachable"
  case $code in 200) return 0 ;; 404) return 1 ;; *) die "ghcr.io answered HTTP $code for $repo:$2" ;; esac
}
for name in api worker renderer web; do
  found=""
  for t in "$TAG" $DEPLOY_CANDIDATE_TAGS; do
    if ghcr_has "$name" "$t"; then found=$t; break; fi
  done
  [ -n "$found" ] || die "no image for $name at $TAG or its last 50 ancestors"
  log "image $name: $found"
done

if ! $YES; then
  if $REHEARSAL; then
    read -r -p "Replace $NS's database and files with compose's, then deploy $overlay at $TAG? [y/N] " a
    [[ $a =~ ^[Yy]$ ]] || die "aborted"
  else
    read -r -p "Restore compose's data into $NS and deploy prod at $TAG. Type the namespace to go on: " a
    [ "$a" = "$NS" ] || die "aborted"
  fi
fi

# --- 2. data tier -----------------------------------------------------------------------------------
step data-tier
kubectl kustomize "$here/overlays/$overlay" > "$work/rendered.yaml"
kubectl apply -f "$work/rendered.yaml" -l jobfinder.io/tier=data >/dev/null
for s in $(kubectl -n "$NS" get sealedsecrets -o jsonpath='{.items[*].metadata.name}'); do
  for _ in $(seq 60); do kubectl -n "$NS" get secret "$s" >/dev/null 2>&1 && break; sleep 2; done
  kubectl -n "$NS" get secret "$s" >/dev/null 2>&1 || die "SealedSecret $s was not unsealed (sealed for another namespace or key?)"
done
kubectl -n "$NS" rollout status sts/postgres --timeout=300s >/dev/null
kubectl -n "$NS" rollout status sts/valkey --timeout=300s >/dev/null
kubectl -n "$NS" wait --for=condition=Ready pod/postgres-0 --timeout=120s >/dev/null
log "data tier ready (postgres, valkey, PVCs, secrets)"

# --- 3. scale down ----------------------------------------------------------------------------------
step scale-down
declare -A replicas=()
for d in "${APP_DEPLOYMENTS[@]}"; do
  if n=$(kubectl -n "$NS" get deployment "$d" -o jsonpath='{.spec.replicas}' 2>/dev/null); then replicas[$d]=$n; fi
done
restore_replicas() {
  for d in "${!replicas[@]}"; do
    kubectl -n "$NS" scale deployment "$d" --replicas="${replicas[$d]}" >/dev/null 2>&1 || true
  done
  keda_resume "$NS" || log "WARNING: worker-io's autoscaler is still paused; the next deploy.sh resumes it"
  log "app scaled back: $(for d in "${!replicas[@]}"; do printf '%s=%s ' "$d" "${replicas[$d]}"; done)"
}
if [ ${#replicas[@]} -gt 0 ]; then
  restore_on_exit=1
  # worker-io's autoscaler (#52) would scale it back up; resumed with the replicas, or by step 8's deploy.
  keda_pause "$NS" prod-cutover
  kubectl -n "$NS" scale deployment "${!replicas[@]}" --replicas=0 >/dev/null
  kubectl -n "$NS" wait --for=delete pod -l "app.kubernetes.io/name in ($(IFS=,; echo "${APP_DEPLOYMENTS[*]}"))" --timeout=180s \
    >/dev/null 2>&1 || true
  log "scaled ${!replicas[*]} to 0"
else
  log "no app Deployments in $NS yet"
fi

# --- 4. dump ----------------------------------------------------------------------------------------
step dump
if [ -n "$DUMP_DIR" ]; then mkdir -p "$DUMP_DIR"; else DUMP_DIR=$(mktemp -d -t jobfinder-cutover.XXXXXX); fi
dump="$DUMP_DIR/compose-$(date -u +%Y%m%d-%H%M%S).dump"
compose_row_counts "$SRC" > "$work/src-before"
(
  umask 077
  # shellcheck disable=SC2016  # expanded inside the container
  docker exec "$SRC" sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' > "$dump.part"
)
mv "$dump.part" "$dump"
compose_row_counts "$SRC" > "$work/src-after"
if ! $REHEARSAL && ! cmp -s "$work/src-before" "$work/src-after"; then
  die "compose's row counts changed during the dump: something still writes (compose api/worker/renderer?)"
fi
log "dump $dump ($(du -h "$dump" | cut -f1))"

# --- 5. restore -------------------------------------------------------------------------------------
step restore
# From here on a failure leaves the app down: scaling it back up would run it on compose's older,
# unmigrated schema. A re-run (or deploy.sh) brings it back.
if [ -n "${restore_on_exit:-}" ]; then restore_on_exit="" app_left_down=1; fi
# A fresh database rather than pg_restore --clean: a namespace whose schema is newer (staging) would
# keep tables the dump doesn't have, and the migrate Job would then trip over them.
# shellcheck disable=SC2016  # expanded inside the pod
kubectl -n "$NS" exec postgres-0 -- sh -c \
  'dropdb -U "$POSTGRES_USER" --if-exists --force "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" -O "$POSTGRES_USER" "$POSTGRES_DB"'
# --no-owner/--no-privileges: compose's roles need not exist here; everything belongs to jobfinder.
# shellcheck disable=SC2016  # expanded inside the pod
kubectl -n "$NS" exec -i postgres-0 -- sh -c \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges --exit-on-error --single-transaction' \
  < "$dump"
# shellcheck disable=SC2016  # expanded inside the pod
dst_rev=$(kubectl -n "$NS" exec postgres-0 -- sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -XAtc "select version_num from alembic_version"')
[ "$dst_rev" = "$src_rev" ] || die "restored revision $dst_rev differs from compose's $src_rev"
log "restored at alembic revision $dst_rev"
# The task streams hold ids of the dropped database (#48).
k8s_trim_task_streams "$NS" || log "WARNING: could not empty the task streams; check valkey-0"

# --- 6. row counts ----------------------------------------------------------------------------------
step row-counts
k8s_row_counts "$NS" jobfinder > "$work/dst"
if $REHEARSAL; then
  compare_row_counts compose "$work/src-before" k3s "$work/dst" "$work/src-after" >&2 \
    || die "row counts differ (see above)"
else
  compare_row_counts compose "$work/src-before" k3s "$work/dst" >&2 || die "row counts differ (see above)"
fi

# --- 7. files ---------------------------------------------------------------------------------------
step files
# Root, so tar keeps the files' owner (uid 10001) and modes; Pod Security "baseline" allows it.
kubectl -n "$NS" delete pod "$HELPER" --ignore-not-found --wait=true >/dev/null
kubectl -n "$NS" apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: $HELPER
  labels:
    app.kubernetes.io/name: $HELPER
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  terminationGracePeriodSeconds: 0
  securityContext:
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: files
      image: postgres:16.15
      command: ["sleep", "3600"]
      resources:
        requests: {cpu: 10m, memory: 32Mi}
        limits: {memory: 256Mi}
      volumeMounts:
        - {name: store, mountPath: /dst/store}
        - {name: claude-auth, mountPath: /dst/claude-auth}
  volumes:
    - name: store
      persistentVolumeClaim: {claimName: store}
    - name: claude-auth
      persistentVolumeClaim: {claimName: claude-auth}
EOF
kubectl -n "$NS" wait --for=condition=Ready "pod/$HELPER" --timeout=180s >/dev/null
# shellcheck disable=SC2016  # expanded in the container
sums='cd "$1" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0r md5sum'
for v in store claude-auth; do
  kubectl -n "$NS" exec "$HELPER" -- find "/dst/$v" -mindepth 1 -delete
  docker run --rm --network none -v "${PROJECT}_$v:/src:ro" --entrypoint tar "$TOOL_IMAGE" \
      --numeric-owner -C /src -cf - . \
    | kubectl -n "$NS" exec -i "$HELPER" -- tar --numeric-owner -C "/dst/$v" -xf -
  docker run --rm --network none -v "${PROJECT}_$v:/src:ro" --entrypoint sh "$TOOL_IMAGE" -c "$sums" sh /src \
    > "$work/files-src-$v"
  kubectl -n "$NS" exec "$HELPER" -- sh -c "$sums" sh "/dst/$v" > "$work/files-dst-$v"
  if ! cmp -s "$work/files-src-$v" "$work/files-dst-$v"; then
    $REHEARSAL || die "$v: checksums differ after the copy"
    log "rehearsal: $v differs after the copy ($(diff "$work/files-src-$v" "$work/files-dst-$v" | grep -c '^[<>]') lines; compose still writes)"
  else
    log "$v: $(wc -l < "$work/files-src-$v") files copied, checksums match"
  fi
done
kubectl -n "$NS" delete pod "$HELPER" --wait=true >/dev/null

# --- 8. deploy --------------------------------------------------------------------------------------
step deploy
# deploy.sh applies the Deployments with their manifest replicas, which also undoes step 3, and lifts
# step 3's pause of worker-io's autoscaler once the app is up (on any later deploy.sh too, if this fails).
# The manifests are this checkout's (deploy/k8s next to this script), not the tag's: the prod overlay
# only exists from #47 on. Check `git diff <tag> -- deploy/k8s backend/.env.defaults` first.
app_left_down=""
DEPLOY_COMMIT=$DEPLOY_COMMIT DEPLOY_CANDIDATE_TAGS=$DEPLOY_CANDIDATE_TAGS "$here/deploy.sh" "$overlay" "$TAG"

# --- 9. smoke ---------------------------------------------------------------------------------------
step smoke
check() {  # <path> <expected HTTP code>
  local code
  code=$(curl -s -o /dev/null -m 10 -w '%{http_code}' -H "Host: $host" "$TRAEFIK$1" || true)
  [ "$code" = "$2" ] || die "GET $1 (Host: $host) answered $code, expected $2"
  log "GET $1 (Host: $host) -> $code"
}
check /healthz 200
check / 200
check /api/auth/me 401   # the api answers (no session cookie)
kubectl -n "$NS" exec deploy/api -- python -c \
  "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8000/readyz').read().decode())" >&2

step finished
if $REHEARSAL; then rm -f "$dump"; rmdir "$DUMP_DIR" 2>/dev/null || true; fi
log "timings:"
for t in "${timings[@]}"; do log "  $t"; done
log "  total        $((SECONDS - total_start))s"
if $REHEARSAL; then
  log "rehearsal done: $NS runs $TAG on compose's data"
else
  log "done: $NS serves $host on Traefik. Next (runbook): smoke tests by hand, then switch host Caddy to 127.0.0.1:8280. Dump kept at $dump"
fi
