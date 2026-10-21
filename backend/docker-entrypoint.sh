#!/bin/sh
# Only the api image runs migrations; the worker does not.
# A bare `docker run` migrates here, on every api start (MIGRATE_ON_START unset = on). On k3s a single
# alembic Job migrates before the rollout (#43), so the api pods set MIGRATE_ON_START=false: two replicas must not
# race to migrate, and /readyz keeps them out of rotation until the schema is at this image's head.
set -e
if [ "${MIGRATE_ON_START:-true}" = "true" ]; then
  echo "[entrypoint] running alembic upgrade head..."
  alembic upgrade head
else
  echo "[entrypoint] MIGRATE_ON_START=${MIGRATE_ON_START}: skipping migrations"
fi
echo "[entrypoint] starting: $*"
exec "$@"
