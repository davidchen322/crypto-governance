# Phases 0 and 1 — Build Results

**Status:** Complete and verified · **Date:** 13 Aug 2026 · **Harness:** `make verify` → exit 0

Repository skeleton and the local infrastructure stack are built. The acceptance harness was
written *before* the infrastructure, so the compose file was shaped by what could actually be
checked rather than the reverse.

---

## Verdict

```
[1/5] Phase 0 — repository hygiene                    ok
[2/5] Phase 1 — rebuild the stack from zero           ok
[3/5] Phase 1 — functional probes and Iceberg seed    ok
[4/5] Phase 1 — cycle containers, retain volumes      ok
[5/5] Phase 1 — confirm state survived                ok

PASS — Phases 0 and 1 verified.
```

31 tests: 18 repo-hygiene, 9 infrastructure probes, 4 persistence assertions.

---

## What was built

### Phase 0

Git repository initialized (the directory was not one), PyCharm boilerplate removed, and the
package layout from the implementation plan created. Toolchain is `ruff` for lint and format,
`pytest` for tests, a `Makefile` as the entry point, and a two-job GitHub Actions workflow.

### Phase 1

| Service | Image | Host port | Notes |
| --- | --- | --- | --- |
| postgres | `pgvector/pgvector:pg16` | 55432 | Three databases; `vector` extension and `document_embeddings` applied at init |
| minio | `minio/minio` | 9000 / 9001 | Named volume for data |
| minio-init | `minio/mc` | — | One-shot; creates the `warehouse` bucket and exits 0 |
| iceberg-rest | `crypto-gov/iceberg-rest:1.9.2` | 8181 | Custom build; JDBC catalog backed by Postgres |
| spark | `crypto-gov/spark-iceberg:3.5.3` | — | Local mode; Iceberg 1.9.2 jars baked in |

Two images are built locally rather than pulled. Both reasons are documented in their
Dockerfiles and summarized below.

---

## Deviations from the plan

Four things differed from what the implementation plan specified. All four were discovered by
running the thing, not by reading it.

### 1. `apache/iceberg-rest-fixture` publishes no tag below 1.8.1

The plan named `1.6.0` to match Iceberg 1.6.1 Spark jars. That tag does not exist — the Apache
image starts at 1.8.1. Moved both sides to **1.9.2** so the catalog and the Spark runtime stay
version-matched.

Note the plan's original advice still held: `tabulario/iceberg-rest:1.6.0` *does* exist, but
it is the unmaintained Tabular image. The Apache one is the right long-term choice.

### 2. The catalog image has no Postgres JDBC driver

The headline Phase 1 requirement — catalog metadata surviving a restart — needs the catalog
backed by Postgres rather than its default `jdbc:sqlite::memory:`. The stock image bundles only
the SQLite driver:

```
java.sql.SQLException: No suitable driver found for jdbc:postgresql://postgres:5432/iceberg_catalog
```

Adding the driver is not enough on its own: the image's `CMD` is `java -jar
iceberg-rest-adapter.jar`, and `-jar` makes the JVM ignore both `-cp` and `$CLASSPATH`. So
`docker/iceberg-rest/Dockerfile` adds the driver **and** replaces `CMD` with an explicit
classpath invocation of the same main class.

### 3. A file mount inside a read-only directory mount fails at container init

The first attempt mounted `storage/vector_schema.sql` to
`/docker-entrypoint-initdb.d/01-vector-schema.sql`, inside a directory already mounted `:ro`:

```
error mounting ... create mountpoint for /docker-entrypoint-initdb.d/01-vector-schema.sql
mount: read-only file system
```

`storage/` is now mounted at `/opt/storage:ro` and the init script applies the DDL with
`psql -f`. This keeps `storage/` the single source of truth rather than duplicating schema.

### 4. `apache/spark` leaves `/opt/spark/bin` off `PATH`

`docker compose exec spark spark-sql` fails with *executable file not found*. Fixed in the
image with an `ENV PATH` line rather than hardcoding `/opt/spark/bin/spark-sql` in the test —
the container should behave the way someone typing into it would expect.

---

## The negative control

A harness that always passes is worthless, so the suite was checked against a deliberately
broken stack: the catalog reverted to the source blueprint's ephemeral SQLite backing, with
everything else unchanged.

| Stage | Result |
| --- | --- |
| `docker compose up --wait` | **passed** — all five services healthy |
| Functional probes and Iceberg seed | **passed** — table created, row inserted, Parquet in MinIO |
| `docker compose down` then `up --wait` | **passed** — stack healthy again |
| Persistence assertions | **FAILED** — `TABLE_OR_VIEW_NOT_FOUND: gov.demo.smoke` |

This is exactly the failure mode the plan flagged, and it demonstrates the point that motivated
the harness design: **the broken stack reported healthy at every stage and passed every probe
that did not involve a restart.** Nothing short of tearing the containers down and asserting on
what came back would have caught it.

---

## What the checks actually assert

None of them read `docker compose ps`.

**Phase 0 (18 tests).** Git is initialized; `.env` is not tracked (a `.gitignore` entry added
after staging does not untrack a file); every `os.getenv` key in application code appears in
`.env.example`; every `${VAR}` in `docker-compose.yml` is documented; required files exist;
`scripts/verify.sh` is executable.

**Phase 1 probes (9 tests).** Postgres, MinIO and the catalog are probed **from the host**, so
a broken port mapping fails. Spark is probed **inside its container**, since it reaches MinIO
and the catalog over the compose network — the two views catch different misconfigurations.

- `pg_isready` is treated as liveness only. The real assertion runs pgvector's cosine operator
  and builds an HNSW index, which is what Phase 4 will depend on.
- MinIO gets a write/read/delete round trip, not a `list_buckets` call.
- The catalog is asserted via `GET /v1/config`, the actual REST spec endpoint. An open TCP port
  is not a working catalog.
- Spark creates a namespace and table, inserts, and selects — then MinIO is inspected directly
  to confirm real `.parquet` and Iceberg metadata landed under `warehouse/demo/smoke/`. That
  last check guards the case where the catalog records a table but `S3FileIO` wrote elsewhere.

**Persistence (4 tests).** Run only after `docker compose down` — not `restart`. `restart` never
removes containers, so it cannot distinguish state in a named volume from state in a container's
writable layer.

---

## Verified artifacts

Contents of the warehouse bucket after a passing run:

```
demo/smoke/data/00000-0-27c9e7f5-...-00001.parquet                   799B
demo/smoke/metadata/00000-438c4901-....metadata.json                 730B
demo/smoke/metadata/00001-afefbbb0-....metadata.json                1.7KiB
demo/smoke/metadata/9e778123-...-m0.avro                            6.9KiB
demo/smoke/metadata/snap-8479762697888376370-1-....avro             4.3KiB
```

---

## Known gaps

**CI is unverified.** `.github/workflows/ci.yml` is written but has never run — there is no
GitHub remote yet. The workflow is unproven until the repo is pushed.

**Healthchecks are liveness, not correctness — by design.** The `iceberg-rest` healthcheck is a
`/dev/tcp` port probe and the `spark` healthcheck only confirms the Iceberg jars exist in the
image. They sequence startup; `tests/integration/` is what proves the services work. This split
is deliberate and documented in the compose file, but it does mean `docker compose ps` alone
should never be read as evidence.

**Memory headroom is thin.** Docker Desktop reports 8.3 GB on this machine and
`spark.driver.memory` is set to 2g. Phase 7 adds Airflow to the same host; that is where the
budget gets tight, and where a memory bump or compose profiles may become necessary.

**Two images build from source.** First `make verify` on a clean machine pulls ~2.5 GB and
builds two images. The Spark base image alone is 1.56 GB and took several minutes here.

---

## Reproducing

```bash
make install
make verify     # ~3 minutes once images are cached
```

The exit code is the verdict. `make verify-fast` runs the probes only, against an
already-running stack.

---

## Next

Phase 2 — ingestion. Snapshot GraphQL against `https://hub.snapshot.org/graphql` and Discourse
REST clients writing raw responses to bronze in MinIO, with content-hash keys so re-runs are
no-ops. The one decision worth settling first is the embedding model, since its dimension is
already baked into `document_embeddings.embedding` as `VECTOR(1536)`.
