# Extends the official image rather than rebuilding one, following Airflow's own documented
# pattern (install extra packages against the release's own constraints file, so they can't
# silently drag Airflow's pinned dependency graph out from under it).
#
# Pinned to whatever Amazon MWAA currently supports, not "whatever Airflow just released" —
# this image's whole point is being a faithful local stand-in for Phase 11's MWAA target.
# Verify against AWS's supported-version list before that cutover; it changes over time and
# a number written here can go stale.
ARG AIRFLOW_VERSION=2.10.3
ARG PYTHON_VERSION=3.11

FROM apache/airflow:${AIRFLOW_VERSION}-python${PYTHON_VERSION}

# `docker` is the CLI client only — no daemon — so the scheduler container can run
# `docker exec` against the sibling `spark` service. `curl` backs the webserver healthcheck.
# Neither is present in the stock image.
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends docker.io curl \
    && rm -rf /var/lib/apt/lists/*

# Deliberately NOT `pyspark`. The plan's source blueprint assumed DAG tasks would run Spark
# in-process; this build's Spark step instead shells into the already-built
# `crypto-gov/spark-iceberg` image the same way `make silver` does (see dags/gov_common.py),
# so the only Python this image needs is what harvest.py and embeddings.py themselves import:
# requests (HTTP), boto3 (bronze writes), psycopg (pgvector), tiktoken (chunk sizing).
# psycopg2-binary is separate and unrelated to any of that — it is the driver Airflow's own
# metadata-database connection uses, independent of the psycopg (v3) the project's own code
# uses for pgvector.
ARG AIRFLOW_VERSION
ARG PYTHON_VERSION
USER airflow
# pytest is here so tests/test_phase7_dags.py can run against the real Airflow install
# this image ships (`make verify-airflow`) — it is a test dependency, not a DAG runtime one.
RUN pip install --no-cache-dir \
    "requests>=2.31" \
    "boto3>=1.34" \
    "psycopg[binary]>=3.1" \
    "tiktoken>=0.7" \
    "psycopg2-binary>=2.9" \
    "pytest>=8.0" \
    --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt"
