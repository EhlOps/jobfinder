# shellcheck shell=bash
# Shared by prod-cutover.sh and backup-restore.sh (#47); source it, don't run it.
# Exact per-table row counts, as `table|count` lines sorted by table name.

# One count(*) per base table of schema public, in a single query (query_to_xml runs each count).
PG_ROW_COUNTS_SQL="select table_name || '|' || (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from public.%I', table_name), false, true, '')))[1]::text from information_schema.tables where table_schema = 'public' and table_type = 'BASE TABLE'"

# k8s_row_counts <namespace> <database>: counts in that namespace's postgres-0 (local socket, no
# password; POSTGRES_USER of base/postgres is jobfinder).
k8s_row_counts() {
  printf '%s\n' "$PG_ROW_COUNTS_SQL" \
    | kubectl -n "$1" exec -i postgres-0 -- psql -U jobfinder -d "$2" -XAtq -v ON_ERROR_STOP=1 -f - \
    | LC_ALL=C sort
}

# compose_row_counts <container>: counts in the compose database (its POSTGRES_USER/POSTGRES_DB env).
compose_row_counts() {
  # shellcheck disable=SC2016  # expanded inside the container
  printf '%s\n' "$PG_ROW_COUNTS_SQL" \
    | docker exec -i "$1" sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -XAtq -v ON_ERROR_STOP=1 -f -' \
    | LC_ALL=C sort
}

# compare_row_counts <expected-label> <expected-file> <actual-label> <actual-file> [<expected-after-file>]
# Prints one line per table and returns 0 only when every table matches. With an "after" file (the
# source counted again after the dump), a table whose source count moved during the dump matches when
# the actual count lies between the two (written while pg_dump ran; rehearsals only) and is flagged.
compare_row_counts() {
  local after=${5:-/dev/null}
  awk -F'|' -v la="$1" -v lb="$3" -v has_after="${5:+1}" '
    FILENAME == ARGV[1] { a[$1] = $2; t[$1] = 1; next }
    FILENAME == ARGV[2] { b[$1] = $2; t[$1] = 1; next }
    { a2[$1] = $2; t[$1] = 1 }
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
        else if (a[k] + 0 != b[k] + 0) {
          lo = a[k] + 0; hi = lo
          if (has_after && (k in a2)) { if (a2[k] + 0 < lo) lo = a2[k] + 0; if (a2[k] + 0 > hi) hi = a2[k] + 0 }
          if (lo != hi && b[k] + 0 >= lo && b[k] + 0 <= hi) note = "ok (written during the dump: " a[k] " -> " a2[k] ")"
          else { note = "MISMATCH"; bad++ }
        }
        printf "%-40s %12s %12s  %s\n", k, (k in a) ? a[k] : "-", (k in b) ? b[k] : "-", note
      }
      if (n == 0) { print "no tables counted"; bad = 1 }
      printf "%d tables, %d mismatched\n", n, bad
      exit bad > 0
    }' "$2" "$4" "$after"
}
