#!/usr/bin/env bash
#
# Phase 0 + Phase 1 acceptance harness.
#
# Exit code is the verdict: 0 means both phases are genuinely built. Nothing here
# inspects `docker compose ps` — every check exercises a real code path.
#
# ISOLATION: this script is deliberately destructive — it destroys volumes to prove the
# stack rebuilds from nothing. It therefore runs under its OWN Compose project, with its
# own volumes and its own host ports, so it can never touch harvested data in the dev
# stack. Re-fetching a large backfill from rate-limited public APIs because someone ran
# the test suite is not an acceptable failure mode.
#
# Sequence:
#   1. Phase 0 repo hygiene (no docker)
#   2. Destroy volumes and rebuild from zero  -> proves reproducibility, not just "it runs"
#   3. Functional probes + seed an Iceberg table
#   4. `down` keeping volumes, then `up`      -> proves state lives in volumes
#   5. Re-assert the seeded state survived
#
set -Eeuo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# --- isolation -------------------------------------------------------------
# A distinct project name gives distinct volumes and containers; distinct host ports
# let the harness run even while the dev stack is up.
export COMPOSE_PROJECT_NAME=crypto-gov-verify
export POSTGRES_PORT=55433
export MINIO_PORT=9010
export MINIO_CONSOLE_PORT=9011
export ICEBERG_REST_PORT=8182
# Trino and Airflow joined docker-compose.yml after this isolation list was last touched;
# neither was overridden here, so this harness collided with a dev stack's own services on
# 8090/8082 the first time both ran at once — caught by actually running both together.
export TRINO_PORT=8091
export AIRFLOW_WEB_PORT=8083

# The pytest fixtures read these, so probes hit the harness stack rather than the dev one.
export MINIO_ENDPOINT="http://localhost:${MINIO_PORT}"
export ICEBERG_REST_URI="http://localhost:${ICEBERG_REST_PORT}"

VENV_PY=".venv/bin/python"
PYTEST=".venv/bin/pytest"

if [[ ! -x "$PYTEST" ]]; then
  echo "error: $PYTEST not found. Run 'make install' first." >&2
  exit 1
fi

step=0
say() {
  step=$((step + 1))
  printf '\n\033[1;36m[%d/5] %s\033[0m\n' "$step" "$1"
}
ok()   { printf '\033[0;32m  ok  %s\033[0m\n' "$1"; }
fail() { printf '\033[0;31m FAIL %s\033[0m\n' "$1" >&2; }

on_error() {
  fail "verification aborted at step $step"
  echo "  the harness stack is still up for inspection:" >&2
  echo "    COMPOSE_PROJECT_NAME=$COMPOSE_PROJECT_NAME docker compose logs" >&2
  echo "    COMPOSE_PROJECT_NAME=$COMPOSE_PROJECT_NAME docker compose down -v" >&2
}
trap on_error ERR

if ! docker info >/dev/null 2>&1; then
  fail "docker daemon is not running"
  exit 1
fi

printf '\033[0;90mharness project: %s (ports %s/%s/%s) — the dev stack is untouched\033[0m\n' \
  "$COMPOSE_PROJECT_NAME" "$POSTGRES_PORT" "$MINIO_PORT" "$ICEBERG_REST_PORT"

say "Phase 0 — repository hygiene"
"$PYTEST" tests/test_phase0_repo.py -q
ok "repo layout, secret hygiene and .env.example are consistent"

say "Phase 1 — rebuild the stack from zero (volumes destroyed)"
docker compose down -v --remove-orphans >/dev/null 2>&1 || true
# --wait blocks on healthchecks and exits non-zero if any service never becomes healthy,
# which is what catches missing depends_on/service_healthy ordering.
docker compose up -d --wait
ok "every service reached a healthy state from an empty volume set"

say "Phase 1 — functional probes and Iceberg seed"
"$PYTEST" -m "integration and not persistence and not live" -v
ok "postgres, pgvector, minio, catalog and spark all exercised end to end"

say "Phase 1 — cycle containers, retain volumes"
docker compose down
docker compose up -d --wait
ok "stack came back after 'docker compose down'"

say "Phase 1 — confirm state survived"
"$PYTEST" -m "integration and persistence" -v
ok "catalog metadata, table data, extension and bucket all persisted"

trap - ERR
say_done() { printf '\n\033[1;32mPASS — Phases 0 and 1 verified.\033[0m\n'; }

# Leave the machine clean on success; on failure the trap above keeps it up for debugging.
docker compose down -v --remove-orphans >/dev/null 2>&1 || true
say_done
