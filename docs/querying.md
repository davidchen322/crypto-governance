# Querying the Data Locally

Three layers hold data, each with its own front door.

| Layer | What's in it | How to query |
| --- | --- | --- |
| **Silver** (Iceberg) | Typed, versioned proposals and forum posts | `make sql` — Trino (fast), or `make spark-sql` |
| **Bronze** (MinIO) | Raw API responses, content-addressed | MinIO console at `localhost:9001`, or boto3 |
| **Postgres** | Vectors (empty until Phase 4), Iceberg catalog, Airflow metadata | `psql` on port 55432 |

Every example below was run against the live stack.

---

## Silver — two engines, one set of tables

**Trino is the one to reach for.** It answers in about a second; Spark pays ~20 seconds of
JVM startup per invocation, which is fine for batch jobs and miserable for exploring.

```bash
make sql            # Trino  — interactive, fast
make spark-sql      # Spark  — slower, but exactly what the jobs run
```

Both read the *same* tables through the *same* Iceberg REST catalog. Nothing is copied or
synced — this is the engine interoperability the Iceberg format exists to provide, and it is
a large part of why the lakehouse is worth its complexity here.

Table names differ only in catalog prefix:

| Engine | Reference |
| --- | --- |
| Trino | `iceberg.silver.proposal_versions` |
| Spark | `gov.silver.proposal_versions` |

One-off queries without the REPL:

```bash
docker compose exec -T trino trino --execute "SELECT count(*) FROM iceberg.silver.proposal_versions"
docker compose exec -T spark spark-sql --silent -e "SELECT count(*) FROM gov.silver.proposal_versions;"
```

```sql
SHOW TABLES FROM iceberg.silver;
DESCRIBE iceberg.silver.proposal_versions;
```

### Connecting a GUI client over JDBC

Trino listens on **port 8090**. Point DBeaver, DataGrip, or Superset at:

```
jdbc:trino://localhost:8090/iceberg/silver?user=trino
```

**A username is mandatory even though authentication is disabled.** Without one Trino
returns `401 Basic authentication or X-Trino-Original-User or X-Trino-User must be sent`.
Any value works — it is an identity label used for query attribution and resource-group
routing, not a credential. **Leave the password empty.**

In DataGrip specifically, fill the **User** field (`trino` is fine) and leave **Password**
blank; the driver sends it as the `X-Trino-User` header.

Including `/iceberg/silver` in the URL pre-selects the catalog and schema, so
`SELECT * FROM proposal_versions` works without fully qualifying every table.

### The examples below use Spark's `gov.` prefix

Swap `gov.` for `iceberg.` to run any of them in Trino. Two syntax differences worth knowing:

- Iceberg metadata tables are quoted in Trino: `iceberg.silver."proposal_versions$snapshots"`
  versus Spark's `gov.silver.proposal_versions.snapshots`.
- Time travel is `FOR VERSION AS OF` / `FOR TIMESTAMP AS OF` in Trino, `VERSION AS OF` in Spark.

### Always filter on `is_current`

Silver is a version history, not a current-state table. Without `is_current` you get every
version of everything, which silently inflates counts and double-counts in aggregates.

```sql
-- current state only
SELECT count(*) FROM gov.silver.proposal_versions WHERE is_current;

-- every version ever observed
SELECT count(*) FROM gov.silver.proposal_versions;
```

### What's live right now

```sql
SELECT protocol_name, substr(title, 1, 44) AS title, vote_count, round(scores_total) AS score
FROM gov.silver.proposal_versions
WHERE is_current AND proposal_state = 'active'
ORDER BY vote_count DESC;
```

```
aave    [ARFC] Low Adoption Asset Deprecation on Aav    58    306151.0
aave    [ARFC] Oracle Deprecation for Long-tail Asse    56    305984.0
```

### Busiest forum threads

```sql
SELECT protocol_name, substr(topic_title, 1, 42) AS topic, count(*) AS posts
FROM gov.silver.forum_posts
WHERE is_current
GROUP BY protocol_name, topic_title
ORDER BY posts DESC LIMIT 10;
```

```
optimism   PGov - Delegate Communication Thread          20
uniswap    Uniswap-Arbitrum Delegate Program (UADP) C     20
ens        Endowment Monthly Reports                     20
```

### Joining a proposal to its forum debate

The point of the whole platform: `discussion_url` on a Snapshot proposal points at the
Discourse thread where it was argued about. The topic id is the trailing number.

```sql
WITH p AS (
  SELECT protocol_name, title, discussion_url, vote_count,
         regexp_extract(discussion_url, '/t/[^/]+/([0-9]+)', 1) AS topic_id
  FROM gov.silver.proposal_versions
  WHERE is_current AND discussion_url IS NOT NULL AND discussion_url <> ''
)
SELECT p.protocol_name, substr(p.title, 1, 38) AS proposal,
       p.vote_count AS votes, count(f.post_id) AS forum_posts
FROM p
JOIN gov.silver.forum_posts f
  ON f.topic_id = cast(p.topic_id AS bigint) AND f.is_current
GROUP BY p.protocol_name, p.title, p.vote_count
ORDER BY forum_posts DESC;
```

```
uniswap    [Temp Check] - Four for V4    118    8
```

**Only one match, and that is expected.** The harvester pulls the *N most recent proposals*
and the *N most recent forum topics* independently, and those two sets barely overlap — a
proposal from three weeks ago points at a thread that has long since dropped off the
front page. Deeper harvests (`--topics 100`) close the gap. It is a coverage limit, not a
join defect.

### Point-in-time — what was true on a given date

This is what the SCD2 columns are for. No special syntax, just a predicate:

```sql
SELECT title, proposal_state, vote_count
FROM gov.silver.proposal_versions
WHERE proposal_id = '0x...'
  AND valid_from <= TIMESTAMP '2026-08-14 21:00:00'
  AND (valid_to > TIMESTAMP '2026-08-14 21:00:00' OR valid_to IS NULL);
```

Swap the timestamp and you get whatever was true then. Returns exactly one row per entity,
or none if the entity had not been observed yet.

### Finding version history

```sql
-- entities that changed at least once
SELECT proposal_id, count(*) AS versions
FROM gov.silver.proposal_versions
GROUP BY proposal_id HAVING versions > 1
ORDER BY versions DESC;
```

Currently returns nothing: every entity is single-version, because no vote landed and no
post was edited between harvests. Re-run it after the harvester has been going for a while.

### Text vs record identity

```sql
-- did the text change, or only the tally?
SELECT proposal_id,
       count(DISTINCT record_hash)  AS record_versions,
       count(DISTINCT content_hash) AS text_versions
FROM gov.silver.proposal_versions
GROUP BY proposal_id HAVING record_versions > 1;
```

`record_versions > text_versions` means the proposal was re-observed with a moved vote tally
but unchanged wording — the case Phase 4 must *not* re-embed.

### Iceberg metadata

Every Iceberg table exposes its own history as queryable tables:

```sql
SELECT operation, summary['total-records'] AS records, committed_at
FROM gov.silver.proposal_versions.snapshots ORDER BY committed_at;

SELECT * FROM gov.silver.proposal_versions.history;
SELECT file_path, record_count, file_size_in_bytes
FROM gov.silver.proposal_versions.files;
```

Time travel to a past snapshot — note this is *table* time travel, a different thing from
the row-level validity windows above:

```sql
SELECT count(*) FROM gov.silver.proposal_versions VERSION AS OF <snapshot_id>;
SELECT count(*) FROM gov.silver.proposal_versions TIMESTAMP AS OF '2026-08-14 20:45:00';
```

---

## Bronze — raw API responses

**MinIO console:** <http://localhost:9001>, login `minioadmin` / `minioadmin`, bucket
`warehouse`. Navigate `snapshot/space=aavedao.eth/<proposal_id>/<hash>.json`. JSON previews
in the browser.

**From Python:**

```python
import boto3, json

s3 = boto3.client(
    "s3",
    endpoint_url="http://localhost:9000",
    aws_access_key_id="minioadmin",
    aws_secret_access_key="minioadmin",
    region_name="us-east-1",
)

keys = s3.list_objects_v2(Bucket="warehouse", Prefix="snapshot/space=aavedao.eth/")["Contents"]
doc = json.loads(s3.get_object(Bucket="warehouse", Key=keys[0]["Key"])["Body"].read())
print(doc["title"], doc["state"], doc["votes"])
```

**Also queryable from Spark** without going through silver, which is useful when you suspect
the transformation rather than the source:

```sql
SELECT id, title, state, votes
FROM json.`s3a://warehouse/snapshot/space=aavedao.eth/`
LIMIT 5;
```

Ignore the `space=probe-*` prefixes — those are integration-test fixtures, and the silver
build filters them out.

---

## Postgres

```bash
psql -h localhost -p 55432 -U engineer -d gov_vectors    # password: localdev
```

Or without a local psql:

```bash
docker compose exec -it postgres psql -U engineer -d gov_vectors
```

Three databases: `gov_vectors` (the `document_embeddings` table, empty until Phase 4),
`iceberg_catalog` (the catalog's own bookkeeping — don't edit by hand), and `airflow`
(unused until Phase 7).

```sql
\dt
SELECT count(*) FROM document_embeddings;   -- 0 until Phase 4
```

---

## Gotchas

**Forgetting `is_current`** is the big one. Silver is append-only version history; every
aggregate needs the filter unless you specifically want history.

**Spark SQL startup is ~20 seconds per invocation.** For exploration use `make sql` once and
stay in the shell rather than firing repeated `-e` one-liners.

**`body_html` is large.** `SELECT *` on `forum_posts` will flood the terminal — project the
columns you want, or use `substr(body_text, 1, 200)`.

**The catalog is Postgres-backed**, so tables survive `docker compose down`. They do not
survive `make nuke`, which destroys volumes by design.
