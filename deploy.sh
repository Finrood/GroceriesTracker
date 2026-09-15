#!/usr/bin/env bash
# Update the production deployment from git in one step.
# Usage: ./deploy.sh  (run inside the deployed clone, e.g. /opt/appdata/groceries)
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Pulling latest code"
git pull --ff-only

echo "==> Building images"
docker compose build

echo "==> Starting services"
docker compose up -d

echo "==> Waiting for groceries-web to become healthy"
for _ in $(seq 1 60); do
    status=$(docker inspect -f '{{.State.Health.Status}}' groceries-web 2>/dev/null || echo "starting")
    [ "$status" = "healthy" ] && break
    sleep 2
done

docker ps --filter name=groceries --format '{{.Names}}: {{.Status}}'
[ "$status" = "healthy" ] || { echo "ERROR: container did not become healthy"; exit 1; }
echo "==> Deploy complete"