#!/usr/bin/env bash
#
# Phase 0 + Phase 1 acceptance harness.
#
# Exit code is the verdict: 0 means both phases are genuinely built. Nothing here
# inspects `docker compose ps` — every check exercises a real code path.
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

trap 'fail "verification aborted at step $step"' ERR

if ! docker info >/dev/null 2>&1; then
  fail "docker daemon is not running"
  exit 1
fi

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
"$PYTEST" -m "integration and not persistence" -v
ok "postgres, pgvector, minio, catalog and spark all exercised end to end"

say "Phase 1 — cycle containers, retain volumes"
docker compose down
docker compose up -d --wait
ok "stack came back after 'docker compose down'"

say "Phase 1 — confirm state survived"
"$PYTEST" -m "integration and persistence" -v
ok "catalog metadata, table data, extension and bucket all persisted"

printf '\n\033[1;32mPASS — Phases 0 and 1 verified.\033[0m\n'
