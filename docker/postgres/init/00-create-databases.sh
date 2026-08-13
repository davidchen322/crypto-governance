#!/bin/bash
# Runs once, on an empty data directory. Creates the catalog and Airflow databases
# alongside POSTGRES_DB, and installs pgvector into the vector database.
#
# `docker compose down -v` wipes the volume, so this re-runs on the next `up` — which
# is what makes the stack reproducible from zero instead of dependent on manual setup.
set -euo pipefail

CATALOG_DB="${POSTGRES_CATALOG_DB:-iceberg_catalog}"
AIRFLOW_DB="${POSTGRES_AIRFLOW_DB:-airflow}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE DATABASE "${CATALOG_DB}";
    CREATE DATABASE "${AIRFLOW_DB}";
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE EXTENSION IF NOT EXISTS vector;
EOSQL

echo "created databases: ${CATALOG_DB}, ${AIRFLOW_DB}; pgvector enabled on ${POSTGRES_DB}"
