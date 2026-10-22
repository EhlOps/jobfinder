#!/usr/bin/env bash
# Replaces staging's database with a copy of production's (#43):
#
#   deploy/k8s/staging-restore.sh [--yes]
#
# pg_dump (custom format) in prod's pod postgres-0 (namespace jobfinder, #53), streamed straight
# into pg_restore in staging's postgres-0: no dump file is left on disk, and prod is only read. Staging's app is scaled to zero for the restore and back afterwards.
# Staging's own data is DROPPED.
#
# Afterwards the schema is at prod's alembic revision. If staging runs a newer image, its api pods
# stay unready (/readyz names both revisions) until the next deploy migrates; run "deploy-staging"
# from GitHub Actions, or deploy/k8s/deploy.sh staging sha-<commit> on the host.
#
# Only the database: documents in prod's `store` volume are not copied, so files uploaded on prod
# are missing on staging. Staging email goes to mailpit only (overlays/staging), so the real
# addresses in the copy never receive anything.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=deploy/k8s/pg-lib.sh
. "$here/pg-lib.sh"

NS=${NS:-jobfinder-staging}
SRC_NS=${SRC_NS:-jobfinder}
SRC_POD=${SRC_POD:-postgres-0}
# Every app Deployment that touches the database, scaled to 0 and back.
APP_DEPLOYMENTS=(api renderer scheduler worker-priority worker-ai worker-io)

log() { printf '[staging-restore] %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

yes=false
case ${1-} in
  --yes) yes=true ;;
  "") ;;
  *) echo "usage: $0 [--yes]" >&2; exit 2 ;;
esac

[ "$NS" != jobfinder ] || die "refusing to restore into the prod namespace"
[ "$NS" != "$SRC_NS" ] || die "source and target are the same namespace ($NS)"

# The staging deploy lock (deploy-ssh.sh staging takes the same file): a deploy during the restore
# would migrate the empty database under pg_restore and scale the app back up.
exec 9>"${JOBFINDER_DEPLOY_LOCK:-/tmp/jobfinder-deploy-staging.lock}"
flock -n 9 || die "a staging deploy is running; try again when it has finished"
kubectl -n "$SRC_NS" get pod "$SRC_POD" >/dev/null || die "$SRC_POD not found in $SRC_NS"
kubectl -n "$NS" get pod postgres-0 >/dev/null || die "postgres-0 not found in $NS"

if ! $yes; then
  read -r -p "Drop staging's database in $NS and replace it with a dump of $SRC_NS/$SRC_POD? [y/N] " answer
  [[ $answer =~ ^[Yy]$ ]] || die "aborted"
fi
# Scale the app down (remembering the counts) so nothing holds connections or writes mid-restore. For
# the workers, which KEDA scales, the saved count is only where they restart: keda_resume then hands
# them back to their autoscalers.
declare -A replicas=()
for d in "${APP_DEPLOYMENTS[@]}"; do
  if n=$(kubectl -n "$NS" get deployment "$d" -o jsonpath='{.spec.replicas}' 2>/dev/null); then
    replicas[$d]=$n
  fi
done
restore_replicas() {
  for d in "${!replicas[@]}"; do
    kubectl -n "$NS" scale deployment "$d" --replicas="${replicas[$d]}" >/dev/null || true
  done
  keda_resume "$NS" || log "WARNING: the workers' autoscalers may still be paused; the next deploy.sh resumes them"
  log "app scaled back: $(for d in "${!replicas[@]}"; do printf '%s=%s ' "$d" "${replicas[$d]}"; done)"
}
if [ ${#replicas[@]} -gt 0 ]; then
  trap restore_replicas EXIT
  log "scaling ${!replicas[*]} to 0"
  # The workers' autoscalers (#52, #124) would scale them back up mid-restore.
  keda_pause "$NS" staging-restore
  kubectl -n "$NS" scale deployment "${!replicas[@]}" --replicas=0 >/dev/null
  # 240s: a worker-ai (or worker-priority) pod may use its whole 200s grace to finish a matching slice.
  kubectl -n "$NS" wait --for=delete pod -l "app.kubernetes.io/name in ($(IFS=,; echo "${APP_DEPLOYMENTS[*]}"))" --timeout=240s \
    >/dev/null 2>&1 || true
fi

# In the pod, the image's POSTGRES_USER/POSTGRES_DB env (jobfinder/jobfinder) name the target; the
# local socket needs no password. POSTGRES_USER is the cluster's superuser, so it can drop and recreate
# the database, which leaves no tables of staging's own behind.
log "recreating the staging database"
# shellcheck disable=SC2016  # expanded inside the pod
kubectl -n "$NS" exec postgres-0 -- sh -c \
  'dropdb -U "$POSTGRES_USER" --if-exists --force "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" -O "$POSTGRES_USER" "$POSTGRES_DB"'

log "streaming pg_dump from $SRC_NS/$SRC_POD into pg_restore"
# --no-owner/--no-privileges: prod's role names need not match; everything belongs to jobfinder.
# shellcheck disable=SC2016  # expanded inside the pods
kubectl -n "$SRC_NS" exec "$SRC_POD" -- sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' \
  | kubectl -n "$NS" exec -i postgres-0 -- sh -c \
      'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges --exit-on-error --single-transaction'

# shellcheck disable=SC2016  # expanded inside the pod
rev=$(kubectl -n "$NS" exec postgres-0 -- sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select version_num from alembic_version"' || true)
# The task streams hold ids of the dropped database (#48).
k8s_trim_task_streams "$NS" || log "WARNING: could not empty the task streams; check valkey-0"
log "restored; schema at alembic revision ${rev:-unknown}"
log "if staging's image is newer, redeploy to migrate (see the header of this script)"
