"""Deterministic hashing of the raw Jobg8 payload stored for each record.

A staged job needs a stable fingerprint so that later sync logic can detect
whether a record has actually changed since the last import, without
comparing every column value by hand.

The hash fingerprints the RAW provider payload -- the feed's own element names
and values exactly as sent -- and deliberately NOT the normalized, validated
field values. This is what keeps the fingerprint honest: the same dict that is
written to ``job_staging.raw_payload`` and promoted into ``jobs.source_payload``
is the dict that is hashed, so the stored payload and its hash can never drift
apart.

Hashing the normalized values instead would create a real staleness hole. Two
raw values that normalize to the same thing -- ``"US Dollar . USD"`` and
``"USD"`` both becoming ``USD`` -- would produce an identical hash, promotion
would classify the record as unchanged, and ``source_payload`` would keep
serving the superseded raw value indefinitely.

Plain ``json.dumps`` does not guarantee a canonical form on its own: dict key
order is preserved by default, and the feed is free to reorder its XML elements
between snapshots. To make the hash canonical, this module always serializes
with ``sort_keys=True`` and a fixed, minimal separator style, so that element
ordering and incidental whitespace can never change the resulting hash. Only
the actual raw values affect the digest.
"""

from __future__ import annotations

import hashlib
import json

from app.imports.schemas import JobFeedRecord


def compute_payload_hash(record: JobFeedRecord) -> str:
    """Compute a deterministic SHA-256 hash of a record's raw feed payload.

    The hashed dict is ``record.source_record``: the provider's original
    element names and values, captured before any coercion or normalization,
    including any unmapped elements the schema does not model. This is the
    exact dict the importer stores as ``raw_payload``, so a change to either
    one always moves the other.

    The dict is serialized to a canonical JSON string:

    - ``sort_keys=True`` guarantees the same key order every time, so a feed
      that reorders its XML elements does not register as an update.
    - ``separators=(",", ":")`` removes all incidental whitespace, so two
      calls never differ only because of formatting.
    - ``default=str`` is a safety net for any non-JSON-native value. The XML
      parser only ever produces ``str`` or ``None``, so this does not engage
      on the feed path; it keeps the function total for hand-built records.

    Args:
        record: A validated JobFeedRecord parsed from a single <Job> element.

    Returns:
        The lowercase hexadecimal SHA-256 digest of the canonical JSON
        representation of the raw feed payload.
    """
    canonical_json = json.dumps(
        record.source_record,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(canonical_json.encode("utf-8"))
    return digest.hexdigest()
