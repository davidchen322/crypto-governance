"""Harvest orchestration and CLI.

Deliberately a plain script, not an Airflow DAG. Scheduling arrives in Phase 7, wrapping
code that already works — debugging DAG plumbing and extraction logic at the same time is
how you end up unable to tell which one is broken.

    python -m data_pipeline.harvest --protocol aave --proposals 25
    python -m data_pipeline.harvest --all --proposals 100 --topics 50
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import boto3

from config.protocols import Protocol, resolve
from config.settings import Settings
from data_pipeline.extraction.bronze import BronzeWriter, HarvestStats
from data_pipeline.extraction.discourse_client import VOLATILE_FIELDS, DiscourseClient
from data_pipeline.extraction.http import HttpClient, TokenBucket
from data_pipeline.extraction.snapshot_client import SnapshotClient

log = logging.getLogger("harvest")

# Conservative defaults. Snapshot is a shared public endpoint; each Discourse instance is
# run by the protocol's own community and anonymous limits there are typically ~60/min.
SNAPSHOT_RATE_PER_MIN = 60
DISCOURSE_RATE_PER_MIN = 30


@dataclass
class HarvestReport:
    run_id: str
    started_at: datetime
    snapshot: HarvestStats = field(default_factory=HarvestStats)
    discourse: HarvestStats = field(default_factory=HarvestStats)
    protocols: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "protocols": self.protocols,
            "snapshot": self.snapshot.as_dict(),
            "discourse": self.discourse.as_dict(),
            "failures": self.failures,
        }


def build_writer(settings: Settings) -> BronzeWriter:
    s3 = boto3.client(
        "s3",
        endpoint_url=settings.minio_endpoint,
        aws_access_key_id=settings.minio_access_key,
        aws_secret_access_key=settings.minio_secret_key,
        region_name=settings.aws_region,
    )
    return BronzeWriter(s3, settings.minio_bucket)


def harvest_snapshot(
    protocol: Protocol,
    client: SnapshotClient,
    writer: BronzeWriter,
    limit: int,
    report: HarvestReport,
) -> None:
    proposals = client.fetch_proposals(protocol.snapshot_space, limit=limit)
    for proposal in proposals:
        result = writer.put("snapshot", protocol.snapshot_space, proposal["id"], proposal)
        report.snapshot.record(result)
    log.info(
        "  snapshot/%-24s %3d written, %3d unchanged",
        protocol.snapshot_space,
        report.snapshot.written,
        report.snapshot.skipped,
    )


def harvest_discourse(
    protocol: Protocol,
    client: DiscourseClient,
    writer: BronzeWriter,
    limit: int,
    report: HarvestReport,
    with_posts: bool,
) -> None:
    topics = client.fetch_topics(limit=limit)
    for topic in topics:
        payload = topic
        if with_posts:
            # Full topic including post bodies — one extra request per topic.
            try:
                payload = client.topic(topic["id"])
            except Exception as exc:  # noqa: BLE001 — one bad topic must not kill the run
                report.discourse.errors += 1
                report.failures.append(f"{protocol.discourse_host} topic {topic['id']}: {exc}")
                log.warning("  topic %s failed: %s", topic["id"], exc)
                continue
        result = writer.put(
            "discourse",
            protocol.discourse_host,
            str(topic["id"]),
            payload,
            hash_exclude=VOLATILE_FIELDS,
        )
        report.discourse.record(result)


def run(
    protocols: list[Protocol],
    settings: Settings,
    proposals: int,
    topics: int,
    with_posts: bool,
) -> HarvestReport:
    report = HarvestReport(
        run_id=f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}",
        started_at=datetime.now(UTC),
        protocols=[p.name for p in protocols],
    )
    writer = build_writer(settings)

    snapshot_http = HttpClient(TokenBucket(SNAPSHOT_RATE_PER_MIN))
    snapshot = SnapshotClient(snapshot_http)

    for protocol in protocols:
        log.info("harvesting %s", protocol.name)
        if proposals:
            try:
                harvest_snapshot(protocol, snapshot, writer, proposals, report)
            except Exception as exc:  # noqa: BLE001 — continue to the next protocol
                report.snapshot.errors += 1
                report.failures.append(f"snapshot {protocol.snapshot_space}: {exc}")
                log.error("  snapshot failed for %s: %s", protocol.name, exc)

        if topics:
            # A fresh bucket per host: these are independent servers, so one forum's
            # budget should not be consumed by traffic to another.
            client = DiscourseClient(
                protocol.discourse_host, HttpClient(TokenBucket(DISCOURSE_RATE_PER_MIN))
            )
            try:
                harvest_discourse(protocol, client, writer, topics, report, with_posts)
            except Exception as exc:  # noqa: BLE001
                report.discourse.errors += 1
                report.failures.append(f"discourse {protocol.discourse_host}: {exc}")
                log.error("  discourse failed for %s: %s", protocol.name, exc)

    key = writer.write_manifest(report.run_id, report.as_dict())
    log.info("manifest written to s3://%s/%s", settings.minio_bucket, key)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Harvest governance data into the bronze layer")
    parser.add_argument(
        "--protocol",
        action="append",
        dest="protocols",
        help="protocol name; repeatable. Default: all",
    )
    parser.add_argument("--all", action="store_true", help="harvest every configured protocol")
    parser.add_argument(
        "--proposals", type=int, default=25, help="max Snapshot proposals per protocol (0 to skip)"
    )
    parser.add_argument(
        "--topics", type=int, default=25, help="max forum topics per protocol (0 to skip)"
    )
    parser.add_argument(
        "--with-posts",
        action="store_true",
        help="fetch full topic bodies (one extra request per topic)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    selected = resolve(None if args.all else args.protocols)
    report = run(selected, Settings.from_env(), args.proposals, args.topics, args.with_posts)

    print()
    print(f"run {report.run_id}")
    print(
        f"  snapshot   {report.snapshot.written:5d} written  "
        f"{report.snapshot.skipped:5d} unchanged  {report.snapshot.errors:3d} errors"
    )
    print(
        f"  discourse  {report.discourse.written:5d} written  "
        f"{report.discourse.skipped:5d} unchanged  {report.discourse.errors:3d} errors"
    )
    for failure in report.failures[:10]:
        print(f"  ! {failure}")

    return 1 if (report.snapshot.errors or report.discourse.errors) else 0


if __name__ == "__main__":
    sys.exit(main())
