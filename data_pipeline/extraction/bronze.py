"""Bronze layer: immutable, content-addressed raw API responses.

Keys are derived from a hash of the payload, never from the harvest date. Two consequences
that the rest of the pipeline depends on:

  * Re-harvesting unchanged content resolves to a key that already exists, so the write is
    skipped. Harvests are idempotent no matter how often they run.
  * When a document *is* edited, the new content hashes differently and lands beside the old
    version rather than overwriting it. Bronze therefore accumulates full version history for
    free, which is what Phase 3's SCD2 silver tables are built from and what makes an
    embedding-model change replayable.

Nothing here interprets the payload. Bronze stores exactly what the API returned.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

import botocore.exceptions

# Anything outside this set is collapsed, so an entity id carrying slashes or spaces
# (Discourse slugs, proposal links) cannot invent extra key prefixes.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _drop(node, parts: list[str]) -> None:
    if not isinstance(node, dict) or not parts:
        return
    head, rest = parts[0], parts[1:]

    if head.endswith("[]"):
        # `posts[].reads` — descend into every element of the list at `posts`.
        seq = node.get(head[:-2])
        if isinstance(seq, list) and rest:
            for item in seq:
                _drop(item, rest)
        return

    if not rest:
        node.pop(head, None)
        return
    _drop(node.get(head), rest)


def prune(payload: dict, exclude: Iterable[str] = ()) -> dict:
    """Return a copy with the given paths removed. Used for hashing only, never for storage.

    Paths are dotted from the root. A `[]` suffix traverses a list, so
    `post_stream.posts[].reads` drops the read counter from every post in a thread.
    """
    if not exclude:
        return payload
    pruned = copy.deepcopy(payload)
    for path in exclude:
        _drop(pruned, path.split("."))
    return pruned


def canonical_hash(payload: dict, exclude: Iterable[str] = ()) -> str:
    """SHA-256 over a canonical JSON encoding of the *content projection*.

    `sort_keys` matters: without it a re-serialised payload with different dict ordering
    would hash differently and every harvest would look like an edit.

    `exclude` matters for the same reason at a different layer. Discourse decorates every
    topic response with request-scoped noise — a randomised `suggested_topics` sidebar, a
    `views` counter, a `message_bus_last_id` cursor — none of which is governance content.
    Hashing the raw response made two fetches of an unedited topic seconds apart look like
    two distinct versions. Identity is computed from the content; the full response is
    still what gets stored.
    """
    encoded = json.dumps(
        prune(payload, exclude), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe(component: str) -> str:
    cleaned = _UNSAFE.sub("-", component).strip("-")
    return cleaned or "unknown"


def bronze_key(source: str, space: str, entity_id: str, content_hash: str) -> str:
    """`<source>/space=<space>/<entity>/<hash>.json`

    Prefix-navigable in the MinIO console, and prefix-listable for Phase 3, while the
    filename carries the version identity.
    """
    return f"{_safe(source)}/space={_safe(space)}/{_safe(entity_id)}/{content_hash[:32]}.json"


@dataclass(frozen=True)
class WriteResult:
    key: str
    content_hash: str
    written: bool  # False means an identical object was already present


@dataclass
class HarvestStats:
    written: int = 0
    skipped: int = 0
    errors: int = 0

    def record(self, result: WriteResult) -> None:
        if result.written:
            self.written += 1
        else:
            self.skipped += 1

    def as_dict(self) -> dict:
        return {"written": self.written, "skipped": self.skipped, "errors": self.errors}


class BronzeWriter:
    def __init__(self, s3_client, bucket: str):
        self.s3 = s3_client
        self.bucket = bucket

    def _exists(self, key: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except botocore.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def put(
        self,
        source: str,
        space: str,
        entity_id: str,
        payload: dict,
        hash_exclude: Iterable[str] = (),
    ) -> WriteResult:
        """`hash_exclude` names request-scoped noise to leave out of the identity hash.
        The body written to S3 is always the complete, unmodified response."""
        digest = canonical_hash(payload, hash_exclude)
        key = bronze_key(source, space, entity_id, digest)

        if self._exists(key):
            return WriteResult(key=key, content_hash=digest, written=False)

        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
            Metadata={
                "content-hash": digest,
                "observed-at": datetime.now(UTC).isoformat(),
                "source": source,
            },
        )
        return WriteResult(key=key, content_hash=digest, written=True)

    def write_manifest(self, run_id: str, body: dict) -> str:
        """Harvest-run bookkeeping. Manifests are the one place a timestamp belongs —
        keeping it out of the data keys is what preserves idempotency."""
        key = f"_manifests/{run_id}.json"
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(body, indent=2, sort_keys=True, default=str).encode("utf-8"),
            ContentType="application/json",
        )
        return key
