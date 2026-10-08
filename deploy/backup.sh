#!/bin/sh
# Back up everything that matters: the Postgres database and the `store` + `claude` data dirs
# (uploaded documents, fetched link text, and the saved Claude token). Run from the repo root:
#   deploy/backup.sh                 # writes to ./backups, keeps the newest 14 of each
#   BACKUP_DIR=/mnt/b BACKUP_KEEP=30 deploy/backup.sh
# Cron (nightly at 03:15):  15 3 * * *  cd /path/to/jobfinder && mkdir -p backups && deploy/backup.sh >> backups/backup.log 2>&1
# Copy the backups off this machine too. Caddy's certificates are not backed up (they are re-issued).
set -eu

cd "$(dirname "$0")/.."
BACKUP_DIR="${BACKUP_DIR:-./backups}"
BACKUP_KEEP="${BACKUP_KEEP:-14}"

# POSTGRES_USER / POSTGRES_DB come from the root .env, as docker compose reads them.
# (Read just these two rather than sourcing the file: other values there may contain shell characters.)
# Strips CR and one pair of surrounding quotes, the way docker compose reads .env.
envval() {
	sed -n "s/^$1=//p" .env 2>/dev/null | tail -n 1 | tr -d '\r' | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}
POSTGRES_USER="${POSTGRES_USER:-$(envval POSTGRES_USER)}"
POSTGRES_DB="${POSTGRES_DB:-$(envval POSTGRES_DB)}"
: "${POSTGRES_USER:?set POSTGRES_USER in .env}"
: "${POSTGRES_DB:?set POSTGRES_DB in .env}"

mkdir -p "$BACKUP_DIR"
umask 077
ts=$(date +%Y%m%d-%H%M%S)
db="$BACKUP_DIR/db-$ts.dump"
files="$BACKUP_DIR/files-$ts.tar.gz"
trap 'rm -f "$db.part" "$files.part"' EXIT   # a failed run leaves no partial files

# Write to a .part file and rename on success, so a failed run never leaves a truncated "backup".
docker compose exec -T db pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB" > "$db.part"
mv "$db.part" "$db"
docker compose exec -T api tar -C /data -czf - store claude > "$files.part"
mv "$files.part" "$files"

# Keep the newest BACKUP_KEEP of each kind (timestamped names sort oldest-first).
for pat in "db-*.dump" "files-*.tar.gz"; do
	ls -1 "$BACKUP_DIR"/$pat 2>/dev/null | sort -r | tail -n +"$((BACKUP_KEEP + 1))" | while read -r old; do
		rm -f -- "$old"
	done
done

echo "backup ok: $db $files"
