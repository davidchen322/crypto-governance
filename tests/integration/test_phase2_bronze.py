"""Phase 2 acceptance: bronze writes against real MinIO.

The Phase 2 exit criterion — "running the harvest twice produces zero duplicate bronze
objects" — is asserted here by counting objects, not by trusting the writer's return value.
"""

from __future__ import annotations

import uuid

import pytest

from data_pipeline.extraction.bronze import BronzeWriter

pytestmark = pytest.mark.integration


@pytest.fixture
def writer(s3_client, bucket) -> BronzeWriter:
    return BronzeWriter(s3_client, bucket)


@pytest.fixture
def space() -> str:
    """Unique per test so runs do not collide with each other or with real harvests."""
    return f"probe-{uuid.uuid4().hex[:12]}.eth"


def _count(s3_client, bucket, prefix: str) -> int:
    paginator = s3_client.get_paginator("list_objects_v2")
    return sum(
        len(page.get("Contents", [])) for page in paginator.paginate(Bucket=bucket, Prefix=prefix)
    )


def test_first_write_lands_in_minio(writer, s3_client, bucket, space):
    result = writer.put("snapshot", space, "0xabc", {"id": "0xabc", "title": "Raise LTV"})
    assert result.written is True
    assert _count(s3_client, bucket, f"snapshot/space={space}/") == 1


def test_reharvesting_identical_content_writes_nothing(writer, s3_client, bucket, space):
    """The exit criterion, asserted by object count rather than by the writer's own report."""
    payload = {"id": "0xabc", "title": "Raise LTV", "scores": [1.0, 2.0]}

    first = writer.put("snapshot", space, "0xabc", payload)
    after_first = _count(s3_client, bucket, f"snapshot/space={space}/")

    # Re-serialised with different key ordering, as a real API response would be.
    reordered = {"scores": [1.0, 2.0], "title": "Raise LTV", "id": "0xabc"}
    second = writer.put("snapshot", space, "0xabc", reordered)
    after_second = _count(s3_client, bucket, f"snapshot/space={space}/")

    assert second.written is False
    assert first.key == second.key
    assert after_first == after_second == 1, "a re-harvest created a duplicate object"


def test_edited_content_lands_beside_the_original(writer, s3_client, bucket, space):
    """Version history for free — the property Phase 3's SCD2 build depends on."""
    writer.put("snapshot", space, "0xabc", {"id": "0xabc", "body": "v1"})
    writer.put("snapshot", space, "0xabc", {"id": "0xabc", "body": "v2 — edited"})

    assert _count(s3_client, bucket, f"snapshot/space={space}/0xabc/") == 2, (
        "an edit must not overwrite the previous version"
    )


def test_many_repeated_harvests_stay_at_one_object(writer, s3_client, bucket, space):
    payload = {"id": "0xrepeat", "title": "unchanged"}
    for _ in range(5):
        writer.put("snapshot", space, "0xrepeat", payload)
    assert _count(s3_client, bucket, f"snapshot/space={space}/") == 1


def test_object_metadata_records_provenance(writer, s3_client, bucket, space):
    result = writer.put("discourse", space, "12345", {"id": 12345, "title": "Temp check"})
    head = s3_client.head_object(Bucket=bucket, Key=result.key)
    meta = head["Metadata"]
    assert meta["content-hash"] == result.content_hash
    assert meta["source"] == "discourse"
    assert meta["observed-at"]


def test_path_hostile_entity_ids_do_not_create_phantom_prefixes(writer, s3_client, bucket, space):
    """Discourse slugs contain slashes; unescaped they would fragment the key space."""
    result = writer.put("discourse", space, "t/some-slug/999", {"id": 999})
    suffix = result.key.split(f"space={space}/", 1)[1]
    assert suffix.count("/") == 1, f"entity id leaked separators: {result.key}"


def test_manifest_is_written_outside_the_data_prefixes(writer, s3_client, bucket):
    run_id = f"probe-{uuid.uuid4().hex[:8]}"
    key = writer.write_manifest(run_id, {"run_id": run_id, "snapshot": {"written": 3}})
    assert key.startswith("_manifests/")
    body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
    assert run_id.encode() in body
