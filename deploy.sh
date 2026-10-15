#!/usr/bin/env bash
# Update the production deployment from git in one step.
# Requires a clean tree because db.sqlite3 and media/ are intentionally tracked.
set -euo pipefail
cd "$(dirname "$0")"

if [[ -n "$(git status --porcelain)" ]]; then
    echo "ERROR: Working tree is not clean. Commit or preserve local data changes before deploying." >&2
    exit 1
fi

previous_head=$(git rev-parse HEAD)

restore_previous_release() {
    local exit_code=$?
    trap - EXIT
    echo "==> Deployment failed; stopping services before recovery" >&2
    docker compose stop --timeout 30 web worker >/dev/null 2>&1 || true
    if [[ "$(git rev-parse HEAD)" != "$previous_head" ]]; then
        if ! docker compose run --rm --no-deps web python -c \
            "import sqlite3; db=sqlite3.connect('db.sqlite3'); result=db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone(); db.close(); assert result[0] == 0" \
            >/dev/null 2>&1; then
            echo "ERROR: SQLite checkpoint failed during rollback." >&2
            echo "The stack is stopped and the checkout/data are preserved for recovery." >&2
            exit "$exit_code"
        fi
        if [[ -n "$(git status --porcelain -- db.sqlite3 media/)" ]] || \
           ! git diff --quiet "$previous_head" HEAD -- db.sqlite3 media/ tracker/migrations/; then
            echo "ERROR: The failed release touched tracked data or migrations." >&2
            echo "The stack is stopped and the checkout/data are preserved for recovery." >&2
            exit "$exit_code"
        fi
        echo "==> Restoring previous code and data snapshot"
        if ! git reset --hard "$previous_head"; then
            echo "ERROR: Could not restore the previous checkout." >&2
            echo "The stack is stopped and the checkout/data are preserved for recovery." >&2
            exit "$exit_code"
        fi
        if ! docker compose build; then
            echo "ERROR: Could not rebuild the previous release." >&2
            echo "The stack is stopped and the checkout/data are preserved for recovery." >&2
            exit "$exit_code"
        fi
    fi
    if ! docker compose up -d; then
        echo "ERROR: Could not start the recovered release." >&2
        echo "The stack is stopped; inspect the deployment before restarting it." >&2
        exit "$exit_code"
    fi
    exit "$exit_code"
}
trap restore_previous_release EXIT

echo "==> Stopping web and worker before updating tracked SQLite data"
docker compose stop --timeout 30 web worker

echo "==> Checkpointing SQLite and confirming tracked data is ready"
docker compose run --rm --no-deps web python -c \
    "import sqlite3; db=sqlite3.connect('db.sqlite3'); result=db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone(); db.close(); assert result[0] == 0"
if [[ -n "$(git status --porcelain)" ]]; then
    echo "ERROR: SQLite has local writes that are not committed. Commit db.sqlite3 before deploying." >&2
    exit 1
fi

echo "==> Pulling latest code"
git pull --ff-only

echo "==> Building images"
docker compose build

echo "==> Starting services"
docker compose up -d --remove-orphans

echo "==> Waiting for web and worker health"
web_status=starting
worker_status=starting
for _ in $(seq 1 60); do
    web_status=$(docker inspect -f '{{.State.Health.Status}}' groceries-web 2>/dev/null || echo "starting")
    worker_status=$(docker inspect -f '{{.State.Health.Status}}' groceries-worker 2>/dev/null || echo "starting")
    if [[ "$web_status" == "healthy" && "$worker_status" == "healthy" ]]; then
        break
    fi
    sleep 2
done

docker compose ps
if [[ "$web_status" != "healthy" || "$worker_status" != "healthy" ]]; then
    echo "ERROR: web or worker did not become healthy (web=$web_status, worker=$worker_status)" >&2
    exit 1
fi

trap - EXIT
echo "==> Deploy complete"
