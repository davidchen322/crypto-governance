# Phase 7 — Airflow Orchestration: Build Results

**Status:** exit criteria met · **Date:** 30 Aug 2026

Phases 2–4 built three scripts that already worked by hand: `data_pipeline.harvest`,
`build_silver.py`, `ai_agent.chains.embeddings`. Phase 7 puts a scheduler in front of them —
two DAGs, both proven end to end against the real stack in this session, not just imported
without error. New to the plan this follows? See [`phase-7-plan.md`](phase-7-plan.md).

---

## Headline

| Exit criterion | Target | Measured |
| --- | --- | --- |
| `gov_pipeline_daily` runs harvest → silver → embed, in order | works | **proven via `airflow dags test`** — all 5 mapped harvests + build_silver succeeded; `embed` correctly failed only because this environment has no `OPENAI_API_KEY` |
| `gov_backfill` accepts run-time params and enforces a cost ceiling | works | **proven both ways** — a real cost estimate ($76.27 for 2,061 chunks) correctly blocked the DAG under a $5 ceiling, and correctly passed through once the ceiling was raised |
| DAGs import without error | zero import errors | **`airflow dags list-import-errors` → "No data found"**, first attempt |
| DAG structure and safety, unit-tested | — | **10/10 pass** against the real Airflow install |

```bash
make airflow-up                    # migrate, create admin user, start webserver + scheduler
open http://localhost:8082         # Airflow UI (see .env.example for admin credentials)
make verify-airflow                # the 10 tests below, against the real install
```

---

## What was built

```
docker/airflow/Dockerfile           extends apache/airflow:2.10.3; docker CLI + curl + requests/
                                     boto3/psycopg/tiktoken/psycopg2-binary/pytest
docker-compose.yml                  + x-airflow-common anchor, airflow-init/webserver/scheduler
dags/gov_common.py                  shared command-building and cost-estimation logic
dags/gov_pipeline_daily.py          @daily: harvest (mapped x5) -> build_silver -> embed
dags/gov_backfill.py                manual: resolve_protocols -> harvest -> build_silver ->
                                     check_cost_ceiling -> embed
ai_agent/graph/sql_templates.py     (Phase 6, untouched here)
data_pipeline/transformation/build_silver.py   + optional_col/ensure_columns (see below —
                                     found and fixed during this session, not planned)
tests/test_phase7_dags.py           10 tests, needs apache-airflow (skips cleanly without it)
tests/spark/optional_column_selftest.py   6 assertions, deterministic Spark schema-gap proof
.env.example                        + AIRFLOW_ADMIN_USER/PASSWORD/SECRET_KEY/WEB_PORT,
                                     SPARK_CONTAINER_NAME
Makefile                            + airflow-up, verify-airflow, airflow-check, airflow-cli,
                                     silver-selftest
```

No changes to `ai_agent/graph/`, `backend_api/`, or the CLI — this phase is purely the
orchestration layer over work already done.

---

## Everything below was run for real, against the real stack

Every claim in this document was checked by actually running it in this session — the same
discipline every prior phase's results doc has followed, extended one layer further out to
Airflow itself.

### `docker compose config` caught a real authoring mistake before anything ran

The first draft put `x-airflow-common: &airflow-common` as a key *inside* `services:`,
copying the visual layout of the services around it rather than the actual convention (the
official Airflow docker-compose example puts it as a top-level sibling of `services:`).
`docker compose config --quiet` was run before anything was built, and it parsed — Compose
does tolerate `x-` prefixed keys in more places than the canonical example uses — but relying
on unconfirmed tolerance rather than the documented pattern was the wrong call to make by
accident. Moved to a top-level extension field to match prior art exactly, then re-verified
`docker compose config` resolved all three Airflow services' `<<: *airflow-common` merges
correctly (environment, volumes, and per-service overrides for `depends_on`, `ports`, and
`command` all resolved as intended) before building anything.

### The image build needed less than expected

`requests`, `boto3`, and `psycopg2-binary` turned out to already be present in the base
`apache/airflow:2.10.3-python3.11` image — the `pip install` step reported "Requirement
already satisfied" for all three. Only `psycopg[binary]` (v3, for this project's own pgvector
access — distinct from the v2 driver Airflow's own metadata connection uses), `tiktoken`, and
`pytest` (added after the fact so `tests/test_phase7_dags.py` could actually run — see below)
needed installing. No dependency conflicts against the pinned constraints file.

### `gov_pipeline_daily`, run for real

```
$ docker compose exec -T airflow-scheduler airflow dags test gov_pipeline_daily 2026-08-30
```

All 5 mapped `harvest` tasks (one per protocol) succeeded, in sequence:

```
Marking task as SUCCESS. task_id=harvest map_index=2  (arbitrum)
Marking task as SUCCESS. task_id=harvest map_index=1  (uniswap)
Marking task as SUCCESS. task_id=harvest map_index=4  (ens)
Marking task as SUCCESS. task_id=harvest map_index=0  (aave)
Marking task as SUCCESS. task_id=harvest map_index=3  (optimism)
```

Then `build_silver` — the `docker exec crypto-gov-spark-1 spark-submit ...` command,
constructed by `dags/gov_common.py` and run via `BashOperator` — succeeded:

```
Running command: ['/usr/bin/bash', '-c', 'docker exec crypto-gov-spark-1 spark-submit
  --master "local[*]" /opt/app/data_pipeline/transformation/build_silver.py']
Marking task as SUCCESS. task_id=build_silver
```

This is the Docker-outside-of-Docker design working end to end for the first time — the
scheduler container really did reach out through its mounted `/var/run/docker.sock` and
command a sibling container. Then `embed` failed, correctly:

```
airflow.exceptions.AirflowFailException: embeddings.main() exited 1
```

(`embeddings.main()` returns 1 only when `OPENAI_API_KEY` is unset — this environment has no
real key configured, so this is the designed failure, not a bug. See *What else needs to be
done*, below, for what turns this green.)

### `gov_backfill`'s cost ceiling, proven both directions

```
$ docker compose exec -T airflow-scheduler airflow dags test gov_backfill 2026-08-30 \
    --conf '{"protocols": ["aave"], "proposals": 5, "topics": 3}'
```

`resolve_protocols` → `harvest[aave]` → `build_silver` all succeeded, then, with the default
`backfill_cost_ceiling_usd` of $5:

```
airflow.exceptions.AirflowFailException: Estimated embedding cost $76.27 for 2061 chunks
(582,218 tokens) exceeds the $5.00 ceiling. Raise the 'backfill_cost_ceiling_usd' Airflow
Variable to proceed, or re-trigger with smaller params.topics / params.proposals.
```

That estimate is real — computed by `estimate_embedding_cost()` calling the exact same
`load_silver_chunks()` / `filter_already_embedded()` functions `make embed --dry-run` uses,
against this environment's actual accumulated silver (100 proposal versions, 665 forum posts
from earlier phases' testing, none yet embedded). Raising the Variable to $100 and re-running
the identical command let `check_cost_ceiling` **succeed** with the same $76.27 estimate now
under the higher ceiling, and the DAG proceeded to `embed`, which then failed only on the
same missing-API-key condition as the daily DAG — proving both branches of the safety check
without spending anything.

### The one wrinkle worth naming: the ceiling estimates the whole pending queue, not just this run

`check_cost_ceiling` estimates the cost of *everything currently pending* across all of
silver, not only the documents this specific backfill run just harvested — because that is
also what `embed()` itself actually does; it is a global incremental job, not scoped to one
DAG run. The $76.27 figure above reflects this environment's entire un-embedded backlog, not
merely five Aave proposals. This is correct behavior, not a bug, but it means re-triggering a
backfill for one protocol while a large embedding backlog exists elsewhere will report that
whole backlog's cost, not just the new protocol's — worth knowing before reading the number.

### `tests/test_phase7_dags.py` — 10/10, against the real install

```
$ make verify-airflow
tests/test_phase7_dags.py::test_dags_import_without_error PASSED
tests/test_phase7_dags.py::test_gov_pipeline_daily_has_the_expected_task_dependencies PASSED
tests/test_phase7_dags.py::test_gov_pipeline_daily_does_not_catch_up_and_runs_one_at_a_time PASSED
tests/test_phase7_dags.py::test_every_protocol_is_represented_in_the_mapped_tasks PASSED
tests/test_phase7_dags.py::test_harvest_task_fails_the_dag_on_reported_errors PASSED
tests/test_phase7_dags.py::test_daily_uses_the_established_small_window_defaults PASSED
tests/test_phase7_dags.py::test_gov_backfill_accepts_per_run_params_for_proposals_and_topics PASSED
tests/test_phase7_dags.py::test_cost_ceiling_check_task_exists_between_silver_and_embed PASSED
tests/test_phase7_dags.py::test_backfill_and_daily_dags_share_the_harvest_and_silver_task_logic PASSED
tests/test_phase7_dags.py::test_spark_command_execs_by_name_not_docker_compose PASSED
10 passed in 0.50s
```

One test bug caught along the way: `dag.params["proposals"]` indexes a DAG's `ParamsDict`
straight to the resolved default *value*, not a `Param` wrapper object — the first draft of
`test_gov_backfill_accepts_per_run_params_for_proposals_and_topics` assumed a `.value`
attribute that doesn't exist on an `int`. Fixed by checking the actual object rather than
guessing its shape, the same discipline this project applies to its own application code.

`make test` (the host venv) does **not** run this file — `apache-airflow` isn't installed
there, and `pytest.importorskip` makes that a clean skip rather than a failure. `make
verify-airflow` runs it inside `airflow-scheduler`, which has the real dependency.

---

## A CI bug found and fixed along the way — twice

While bringing this stack up, a message arrived reporting the "Stack acceptance" CI job had
been failing. Investigating turned out to be unrelated to Phase 7 itself but genuinely urgent,
so it was fixed in the same session rather than deferred. Full detail lives in the two commit
messages (`ac6a2cf`, `becaf58`); summarized:

**Root cause:** `spark.read.json()` drops a field from its inferred schema entirely when that
field is `null` in every row of the batch being read — not merely typed nullable, genuinely
absent from `df.columns`. CI harvests live from Snapshot; twice in a row, the small fresh
batch of proposals it fetched happened to have every proposal share a null value for one
optional field (`discussion` the first time, `author` the second), and `build_proposals()`
referenced each with a plain `F.col(name)`, which raises `UNRESOLVED_COLUMN` rather than
returning nulls.

**Fix, in two rounds:** the first round wrapped every field this file explicitly reads via
`F.col()`. CI's very next run proved that wasn't enough — `author`, `link` and `choices` pass
through by name alone (the GraphQL field name already matches the target column, so nothing
ever calls `.withColumn` for them), and are exposed to the identical gap. The second round
added `ensure_columns(df, columns)`: applied once, immediately before each of
`build_proposals()`'s and `build_posts()`'s final `.select()`, it adds a null default for
**any** column in the target list Spark's schema inference dropped — not just the fields a
hand-written fix happened to enumerate. This is the general form of the fix, and it protects
`build_posts()`'s column list too, proactively, without that function having actually failed
yet.

**Verified**, not assumed: `tests/spark/optional_column_selftest.py` deterministically
reproduces the exact schema-inference gap (a JSON batch that never mentions a given field at
all — no live-data luck required to exercise it) and proves both that the naive approach
really does crash and that the fix doesn't. `build_silver.py` was re-run against this
environment's real bronze data after each round with no change in output (100 proposal
versions, 665 forum posts, both times).

**CI status:** the fix commits were pushed during this session; check
`https://github.com/davidchen322/crypto-governance/actions` for the latest run — by the time
this document is read, that run's outcome is more current than anything stated here.

---

## What else needs to be done

Everything above ran through `airflow dags test`, which executes a DAG directly without the
scheduler's normal loop — the fastest way to prove the wiring works, but it does not schedule
anything, and both DAGs ship **paused**, which is Airflow's own safe default for a
newly-deployed DAG. Turning this into an actually-running pipeline needs a person to do the
following, in this order:

### 1. Get a real OpenAI key into the stack

Every real DAG run's `embed` task needs `OPENAI_API_KEY`. This project already has a
dedicated guide — follow [`docs/api-keys.md`](api-keys.md) if it hasn't been done yet, then:

```bash
echo 'OPENAI_API_KEY=sk-...' >> .env    # into .env, never .env.example
docker compose up -d --wait airflow-webserver airflow-scheduler   # picks up the new value
```

Without this, both DAGs will keep running successfully right up to `embed` and then failing
loudly — which is correct, not broken, but not a running pipeline either.

### 2. Log into the Airflow UI and change the local-dev credentials

```
http://localhost:8082
```

Username/password default to `admin`/`admin` (`.env.example`'s `AIRFLOW_ADMIN_USER`/
`AIRFLOW_ADMIN_PASSWORD`). **Change these in `.env` before this stack is ever reachable by
anyone but you** — nothing enforces it, and the defaults are placeholders, not a real
credential. Also replace `AIRFLOW_SECRET_KEY` (signs session cookies) with a real random
value at the same time; both are called out explicitly in `.env.example`.

### 3. Unpause `gov_pipeline_daily`

New DAGs ship paused so that building the DAG file and having it start executing
unattended are two separate, deliberate decisions. In the UI: toggle it on from the DAGs
list. From the CLI:

```bash
docker compose exec airflow-scheduler airflow dags unpause gov_pipeline_daily
```

Once unpaused, it runs on its `@daily` schedule starting from the next scheduled interval —
it does **not** immediately backfill every day since `start_date` (`catchup=False` is exactly
what prevents that).

### 4. Set the two Airflow Variables that gate backfill spending

Both have code defaults (`0.131`/1k tokens, `$5.00` ceiling) so the DAG runs without them —
but the defaults were chosen from Phase 4's measured rate on a much smaller corpus, and $5
is a conservative placeholder, not a considered budget. Set real values before the first
real backfill, in the UI (Admin → Variables) or the CLI:

```bash
docker compose exec airflow-scheduler airflow variables set embedding_cost_per_1k_tokens 0.13
docker compose exec airflow-scheduler airflow variables set backfill_cost_ceiling_usd 25.00
```

Check current OpenAI pricing before trusting either number — same discipline this project has
applied to every cost figure since Phase 4.

### 5. Trigger `gov_backfill` deliberately, once, watched

This should **not** be unpaused-and-left-alone the way the daily DAG is — it is a manually
triggered, one-time (or occasional) job, not a recurring one. Trigger it with explicit config
rather than accepting the wide defaults blind the first time:

```bash
# Start narrow: one protocol, a bounded size, watch it before going wider.
docker compose exec airflow-scheduler airflow dags trigger gov_backfill \
  --conf '{"protocols": ["aave"], "proposals": 200, "topics": 100}'
```

Or from the UI: DAGs → `gov_backfill` → the play button → "Trigger DAG w/ config" → paste the
same JSON. Watch the `check_cost_ceiling` task's log for the real estimate before it reaches
`embed` — that is the whole point of the task existing. Per
[`phase-7-plan.md`](phase-7-plan.md#sizing-the-historical-backfill), re-trigger with a larger
`topics` value for any protocol whose harvest is still mostly "written" rather than
"unchanged" against the prior run — Discourse's true per-protocol history depth isn't
documented anywhere, so this has to be discovered operationally, not looked up.

### 6. Only after both of the above have been watched succeed once: consider unpausing backfill-adjacent automation

There is no DAG that runs `gov_backfill` on a schedule, deliberately — nothing here should be
built to auto-trigger repeated full-history spending. If a recurring "top up history"
schedule is ever wanted, that is a new, deliberate DAG to design, not a switch to flip on this
one.

### Reproducing the whole checklist from zero

```bash
make up                     # postgres, minio, iceberg-rest, spark, trino
make airflow-up             # migrate, create admin, start webserver + scheduler
make verify-airflow         # confirm the DAGs are structurally sound
# ... steps 1-5 above ...
```

---

## How we will know it works — status against the plan's own criteria

Restating [`phase-7-plan.md`](phase-7-plan.md)'s two "how we will know" sections, marked
against what this session actually confirmed:

| Criterion | Status |
| --- | --- |
| DAGs import without error | ✅ confirmed (`airflow dags list-import-errors`) |
| `harvest → build_silver → embed` dependency order | ✅ confirmed (test + real `dags test` run) |
| `catchup=False`, `max_active_runs=1` | ✅ confirmed (test) |
| A failed harvest fails the DAG rather than being absorbed | ✅ confirmed (command construction never masks the exit code; test pins it) |
| Every protocol represented in the mapped tasks | ✅ confirmed (test) |
| Quiet days stay quiet (idempotent re-runs) | ⏳ **not yet observable** — needs `embed` to actually succeed once (real API key) before a second run can show "nothing to embed"; the mechanism itself (bronze content-addressing, guarded MERGE, embeddings skip-check) is unchanged from Phases 2-4 and already individually proven there |
| A week of green daily runs | ⏳ **needs real elapsed time** — cannot be simulated; revisit after step 3 above has been live for a week |
| Backfill accepts run-time params | ✅ confirmed (test + real trigger with `--conf`) |
| Cost ceiling blocks an over-budget estimate | ✅ confirmed (real $76.27 estimate blocked a $5 ceiling) |
| Cost ceiling lets an under-budget estimate proceed | ✅ confirmed (same estimate passed a $100 ceiling) |
| At least one multi-version entity in silver (Phase 3/5/6's long-standing gap) | ⏳ **still open** — this session's harvests were all small and fresh; a real backfill run followed by a later re-harvest that catches something changed is what finally exercises this, per the plan |

---

## Known limitations

**The Docker-socket mount is exactly the trade-off the plan named, not a surprise.** The
scheduler container has root-equivalent control over the whole Docker host via the mounted
socket. Accepted for a local, single-user demo stack; the AWS section of the plan is explicit
that this specific mechanism has no managed equivalent and becomes a real rewrite
(`EmrServerlessStartJobRunOperator`) at Phase 11, not a configuration change.

**`gov_backfill`'s cost estimate is global, not per-run** — see *The one wrinkle worth
naming*, above. Correct behavior, easy to misread the number without this context.

**Discourse's true per-protocol history depth remains undocumented.** Nothing in this
session measured it; the plan's own "run large, watch written-vs-unchanged" operational
signal is the intended way to discover it, not something this phase computes in advance.

**A harmless `FutureWarning`** (`[core/sql_alchemy_conn]` deprecated) appears on every
`airflow` CLI invocation, from something inside Airflow's own configuration loader — the
compose file already sets the current-style `AIRFLOW__DATABASE__SQL_ALCHEMY_CONN` key. Noted
rather than chased, matching this project's existing practice with Phase 6's `httpx`
deprecation warning: it is not an indication that anything set here is misconfigured.

**No alerting on a failed run.** A red task in the Airflow UI is currently the only signal —
exactly the seam Phase 10 will hook a webhook callback into, per the plan, not something to
build speculatively here.

---

## What's next

Phase 8 — the Next.js dashboard, including the version-history timeline screen that has been
waiting since Phase 3 for a document with more than one version to actually exist. That
screen has real data to show only after: (1) a real OpenAI key lets the daily DAG's `embed`
step actually run, and (2) a real backfill, triggered per the checklist above, both completes
and is followed by a later re-harvest that catches something changing. Both are now
mechanically ready; neither has happened yet in this environment.
