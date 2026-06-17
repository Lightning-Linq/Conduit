#!/bin/sh
set -e

# Bring the database schema to head before the app starts, so a container never
# serves against a stale schema. Fail fast if a migration fails.
echo "[entrypoint] applying database migrations: alembic upgrade head"
alembic upgrade head

exec "$@"
