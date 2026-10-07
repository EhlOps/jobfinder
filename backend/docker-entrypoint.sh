#!/bin/sh
# Only the api image runs migrations; the worker does not.
set -e
echo "[entrypoint] running alembic upgrade head..."
alembic upgrade head
echo "[entrypoint] starting: $*"
exec "$@"
