#!/usr/bin/env bash
# Build and (re)start Savepoint. Run from ~/apps/savepoint after `git pull`.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
    echo "No .env found. Copy .env.example to .env and set SAVEPOINT_ADMIN_PASSWORD." >&2
    exit 1
fi
mkdir -p data rclone

docker compose build --pull
docker compose up -d --remove-orphans
docker image prune -f >/dev/null

echo "Waiting for health check..."
for _ in $(seq 1 30); do
    status="$(docker inspect -f '{{.State.Health.Status}}' savepoint 2>/dev/null || true)"
    if [ "$status" = "healthy" ]; then
        echo "Savepoint is up."
        exit 0
    fi
    sleep 2
done
echo "Savepoint did not become healthy. Recent logs:" >&2
docker compose logs --tail 50 savepoint >&2
exit 1
