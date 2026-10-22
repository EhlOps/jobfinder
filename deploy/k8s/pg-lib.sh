# shellcheck shell=bash
# Shared by backup-restore.sh and staging-restore.sh (#47); source it, don't run it.
# Exact per-table row counts, as `table|count` lines sorted by table name; emptying the task streams;
# pausing the workers' autoscalers.

# One count(*) per base table of schema public, in a single query (query_to_xml runs each count).
PG_ROW_COUNTS_SQL="select table_name || '|' || (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from public.%I', table_name), false, true, '')))[1]::text from information_schema.tables where table_schema = 'public' and table_type = 'BASE TABLE'"

# k8s_row_counts <namespace> <database>: counts in that namespace's postgres-0 (local socket, no
# password; POSTGRES_USER of base/postgres is jobfinder).
k8s_row_counts() {
  printf '%s\n' "$PG_ROW_COUNTS_SQL" \
    | kubectl -n "$1" exec -i postgres-0 -- psql -U jobfinder -d "$2" -XAtq -v ON_ERROR_STOP=1 -f - \
    | LC_ALL=C sort
}

# compare_row_counts <expected-label> <expected-file> <actual-label> <actual-file>
# Prints one line per table and returns 0 only when every table matches.
compare_row_counts() {
  awk -F'|' -v la="$1" -v lb="$3" '
    FILENAME == ARGV[1] { a[$1] = $2; t[$1] = 1; next }
    { b[$1] = $2; t[$1] = 1 }
    END {
      printf "%-40s %12s %12s  %s\n", "table", la, lb, ""
      bad = 0; n = 0
      for (k in t) keys[++n] = k
      # insertion sort: awk without asort
      for (i = 2; i <= n; i++) { v = keys[i]; for (j = i - 1; j > 0 && keys[j] > v; j--) keys[j + 1] = keys[j]; keys[j + 1] = v }
      for (i = 1; i <= n; i++) {
        k = keys[i]; note = ""
        if (!(k in a)) { note = "MISSING in " la; bad++ }
        else if (!(k in b)) { note = "MISSING in " lb; bad++ }
        else if (a[k] + 0 != b[k] + 0) { note = "MISMATCH"; bad++ }
        printf "%-40s %12s %12s  %s\n", k, (k in a) ? a[k] : "-", (k in b) ? b[k] : "-", note
      }
      if (n == 0) { print "no tables counted"; bad = 1 }
      printf "%d tables, %d mismatched\n", n, bad
      exit bad > 0
    }' "$2" "$4"
}

# k8s_trim_task_streams <namespace>: empties the task streams (#48) in that namespace's valkey-0, keeping
# the consumer groups. After a database restore their ledger ids name rows of the old database, which
# a worker must not claim; the scheduler's relay re-sends the restored database's queued rows.
k8s_trim_task_streams() {
  local q
  for q in ai io render; do
    kubectl -n "$1" exec valkey-0 -- valkey-cli XTRIM "jobfinder:$q" MAXLEN 0 >/dev/null || return 1
  done
}

# KEDA owns every worker's replica count (#52, #124: base/app/worker-*-scaledobject.yaml), so a script
# that needs them at 0 pauses the ScaledObjects too: `kubectl scale --replicas=0` alone is undone within
# a polling interval. keda_pause <namespace> <who> pauses every ScaledObject in the namespace at 0 (call
# it before the scale-down, so an HPA can't bring a pod back in between); keda_resume <namespace> hands
# each one a script paused back to KEDA. A pause that isn't ours (no jobfinder.io/paused-by: set by
# hand) is left alone by both, object by object. Both are no-ops where there is no ScaledObject (or no
# KEDA), and fail on any other error, so a script never scales down believing the workers are paused
# when they aren't. deploy.sh also lifts marked pauses after its rollout, so a script that failed in
# between recovers with the redeploy.
KEDA_PAUSED=autoscaling.keda.sh/paused-replicas
# _keda_objects <namespace>: one line per ScaledObject, "<name>|<paused-replicas>|<paused-by>"; nothing
# when KEDA or the objects are absent.
_keda_objects() {
  local err
  if ! err=$(kubectl get crd scaledobjects.keda.sh -o name 2>&1); then
    [[ $err == *NotFound* ]] && return 0
    printf '%s\n' "$err" >&2
    return 1
  fi
  kubectl -n "$1" get scaledobject -o jsonpath='{range .items[*]}{.metadata.name}|{.metadata.annotations.autoscaling\.keda\.sh/paused-replicas}|{.metadata.annotations.jobfinder\.io/paused-by}{"\n"}{end}'
}
keda_pause() {
  local ns=$1 who=$2 objects name paused by
  objects=$(_keda_objects "$ns") || return 1
  while IFS='|' read -r name paused by; do
    [ -n "$name" ] || continue                     # no ScaledObject
    [ -z "$paused" ] || [ -n "$by" ] || continue   # paused, not by a script: not ours
    kubectl -n "$ns" annotate scaledobject "$name" --overwrite "$KEDA_PAUSED=0" "jobfinder.io/paused-by=$who" \
      >/dev/null || return 1
  done <<< "$objects"
}
keda_resume() {
  local ns=$1 objects name by
  objects=$(_keda_objects "$ns") || return 1
  while IFS='|' read -r name _ by; do
    [ -n "$name" ] && [ -n "$by" ] || continue     # not paused by a script
    kubectl -n "$ns" annotate scaledobject "$name" "$KEDA_PAUSED-" jobfinder.io/paused-by- >/dev/null || return 1
  done <<< "$objects"
}
