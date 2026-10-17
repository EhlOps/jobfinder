#!/usr/bin/env bash
# Restore drill for the nightly backups (#47):
#
#   deploy/k8s/backup-restore.sh <namespace> [db-<timestamp>.dump]
#
# Restores the newest dump on PVC `backups` (or the one named) into a scratch database next to the
# live one in that namespace's postgres-0, compares every table's row count with the live database,
# checks that the matching files-<timestamp>.tar.gz reads back, and drops the scratch database. The
# live database and the app are never touched.
#
# Exit status 0 when every count matches. The live database keeps changing after 03:15, so run it
# right after a backup (kubectl -n <ns> create job --from=cronjob/backup backup-now) for an exact
# comparison; tables written since the dump show up as MISMATCH and exit 1.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=deploy/k8s/pg-lib.sh
. "$here/pg-lib.sh"

usage() { echo "usage: $0 <namespace> [db-<timestamp>.dump]" >&2; exit 2; }
if [ $# -lt 1 ] || [ $# -gt 2 ]; then usage; fi
NS=$1 dump=${2-}
[ -z "$dump" ] || [[ $dump =~ ^db-[0-9]{8}-[0-9]{6}\.dump$ ]] || usage
HELPER=backup-restore-drill
scratch="restore_drill_$(date -u +%Y%m%d%H%M%S)"

log() { printf '[backup-restore %s] %s\n' "$NS" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

kubectl -n "$NS" get pvc backups >/dev/null || die "PVC backups not found in $NS"
kubectl -n "$NS" get pod postgres-0 >/dev/null || die "postgres-0 not found in $NS"

work=$(mktemp -d)
cleanup() {
  kubectl -n "$NS" exec postgres-0 -- dropdb -U jobfinder --if-exists --force "$scratch" >/dev/null 2>&1 \
    || log "WARNING: could not drop $scratch; drop it by hand"
  kubectl -n "$NS" delete pod "$HELPER" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

# Reads the backups as the CronJob's uid (the files are 0600), read-only, and restores from inside the
# cluster: streaming a large dump out through `kubectl exec` stdout breaks off (websocket EOF).
kubectl -n "$NS" delete pod "$HELPER" --ignore-not-found --wait=true >/dev/null
kubectl -n "$NS" apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: $HELPER
  labels:
    app.kubernetes.io/name: $HELPER
    jobfinder.io/postgres-client: "true"
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  terminationGracePeriodSeconds: 0
  securityContext:
    runAsNonRoot: true
    runAsUser: 10001
    runAsGroup: 10001
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: drill
      image: postgres:16.15
      command: ["sleep", "3600"]
      env:
        - {name: HOME, value: /tmp}
        - name: PGPASSWORD
          valueFrom:
            secretKeyRef: {name: postgres-credentials, key: POSTGRES_PASSWORD}
      resources:
        requests: {cpu: 10m, memory: 32Mi}
        limits: {memory: 512Mi}
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities:
          drop: ["ALL"]
      volumeMounts:
        - {name: backups, mountPath: /backups, readOnly: true}
        - {name: tmp, mountPath: /tmp}
  volumes:
    - name: tmp
      emptyDir: {sizeLimit: 64Mi}
    - name: backups
      persistentVolumeClaim: {claimName: backups, readOnly: true}
EOF
kubectl -n "$NS" wait --for=condition=Ready "pod/$HELPER" --timeout=180s >/dev/null

if [ -z "$dump" ]; then
  dump=$(kubectl -n "$NS" exec "$HELPER" -- sh -c 'cd /backups && ls -1 db-*.dump 2>/dev/null | sort | tail -n 1')
  [ -n "$dump" ] || die "no db-*.dump on PVC backups"
fi
kubectl -n "$NS" exec "$HELPER" -- test -f "/backups/$dump" || die "/backups/$dump not found"
log "drilling $dump ($(kubectl -n "$NS" exec "$HELPER" -- du -h "/backups/$dump" | cut -f1))"

start=$SECONDS
kubectl -n "$NS" exec postgres-0 -- createdb -U jobfinder -O jobfinder "$scratch"
# kube-router admits a new pod's IP a few seconds after it starts.
kubectl -n "$NS" exec "$HELPER" -- sh -c \
  'i=0; until pg_isready -q -h postgres -U jobfinder -t 2; do i=$((i+1)); [ $i -lt 60 ] || exit 1; sleep 2; done' \
  || die "postgres unreachable from the drill pod"
kubectl -n "$NS" exec "$HELPER" -- pg_restore -h postgres -U jobfinder -d "$scratch" \
  --no-owner --no-privileges --exit-on-error --single-transaction "/backups/$dump"
log "restored into scratch database $scratch in $((SECONDS - start))s"

k8s_row_counts "$NS" "$scratch" > "$work/restored"
k8s_row_counts "$NS" jobfinder > "$work/live"
rc=0
compare_row_counts restored "$work/restored" live "$work/live" >&2 || rc=1

files="files-${dump#db-}"
files="${files%.dump}.tar.gz"
# Listed to a file first: sh has no pipefail, so `tar | wc` would hide a corrupt archive.
if n=$(kubectl -n "$NS" exec "$HELPER" -- sh -c "tar -tzf '/backups/$files' > /tmp/list && wc -l < /tmp/list"); then
  log "$files reads back: $n entries"
else
  log "ERROR: $files is missing or unreadable"
  rc=1
fi

if [ $rc -eq 0 ]; then
  log "drill passed: $dump restores and matches the live database"
else
  log "drill FAILED (see above; rows written since the dump also count as a mismatch)"
fi
exit $rc
