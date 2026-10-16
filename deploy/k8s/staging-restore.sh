#!/usr/bin/env bash
# Replaces staging's database with a copy of production's (#43):
#
#   deploy/k8s/staging-restore.sh [--yes]
#
# pg_dump (custom format) from the compose container jobfinder-db-1, streamed straight into
# pg_restore in the staging pod postgres-0: no dump file is left on disk. The app is scaled to zero
# for the restore and back afterwards. Staging's own data is DROPPED.
#
# Afterwards the schema is at prod's alembic revision. If staging runs a newer image, its api pods
# stay unready (/readyz names both revisions) until the next deploy migrates; run "deploy-staging"
# from GitHub Actions, or deploy/k8s/deploy.sh staging sha-<commit> on the host.
#
# Only the database: documents in compose's `store` volume are not copied, so files uploaded on prod
# are missing on staging. Staging email goes to mailpit only (overlays/staging), so the real
# addresses in the copy never receive anything.
set -euo pipefail

NS=${NS:-jobfinder-staging}
SRC=${SRC_CONTAINER:-jobfinder-db-1}
APP_DEPLOYMENTS=(api worker renderer)

log() { printf '[staging-restore] %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

yes=false
case ${1-} in
  --yes) yes=true ;;
  "") ;;
  *) echo "usage: $0 [--yes]" >&2; exit 2 ;;
esac

[ "$NS" != jobfinder ] || die "refusing to restore into the prod namespace"
docker inspect "$SRC" >/dev/null 2>&1 || die "compose container $SRC not found"
kubectl -n "$NS" get pod postgres-0 >/dev/null || die "postgres-0 not found in $NS"

if ! $yes; then
  read -r -p "Drop staging's database in $NS and replace it with a dump of $SRC? [y/N] " answer
  [[ $answer =~ ^[Yy]$ ]] || die "aborted"
fi

# Scale the app down (remembering the counts) so nothing holds connections or writes mid-restore.
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
  log "app scaled back: $(for d in "${!replicas[@]}"; do printf '%s=%s ' "$d" "${replicas[$d]}"; done)"
}
if [ ${#replicas[@]} -gt 0 ]; then
  trap restore_replicas EXIT
  log "scaling ${!replicas[*]} to 0"
  kubectl -n "$NS" scale deployment "${!replicas[@]}" --replicas=0 >/dev/null
  kubectl -n "$NS" wait --for=delete pod -l 'app.kubernetes.io/name in (api,worker,renderer)' --timeout=180s \
    >/dev/null 2>&1 || true
fi

# In the pod, the image's POSTGRES_USER/POSTGRES_DB env (jobfinder/jobfinder) name the target; the
# local socket needs no password. POSTGRES_USER is the cluster's superuser, so it can drop and recreate
# the database, which leaves no tables of staging's own behind.
log "recreating the staging database"
# shellcheck disable=SC2016  # expanded inside the pod
kubectl -n "$NS" exec postgres-0 -- sh -c \
  'dropdb -U "$POSTGRES_USER" --if-exists --force "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" -O "$POSTGRES_USER" "$POSTGRES_DB"'

log "streaming pg_dump from $SRC into pg_restore"
# --no-owner/--no-privileges: compose's role names need not match; everything belongs to jobfinder.
# shellcheck disable=SC2016  # expanded inside the containers
docker exec "$SRC" sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' \
  | kubectl -n "$NS" exec -i postgres-0 -- sh -c \
      'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges --exit-on-error --single-transaction'

# shellcheck disable=SC2016  # expanded inside the pod
rev=$(kubectl -n "$NS" exec postgres-0 -- sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select version_num from alembic_version"' || true)
log "restored; schema at alembic revision ${rev:-unknown}"
log "if staging's image is newer, redeploy to migrate (see the header of this script)"
