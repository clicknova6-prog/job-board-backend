---
name: add-feed-field
description: Use when adding, removing, or changing how a Jobg8 XML feed field is mapped into the database (new element, new column, changed validation/fallback rules). Covers all touch points so none get missed.
---

A single Jobg8 field change can touch five files. Missing one makes staging and promotion silently diverge.

1. **app/imports/schemas.py** — add the field to `JobFeedRecord` with its XML `alias`. Decide: does a bad value need fallback coercion (via `apply_field_fallbacks`), or is the automatic blank-string-to-`None` normalization enough? If you coerce or drop a supplied value, add the field name to `fallback_fields` — that's the only signal `ImportService` uses to report `field_fallback_warnings`.
2. **app/db/models.py** — add the column to `JobStaging`, and to `Job` too if it belongs in the canonical table. Provider/commercial-only fields (like `sell_price`) get no dedicated column — they stay in `raw_payload`/`source_payload` only.
3. **app/db/repositories.py** — map the field in `JobRepository.stage_job()`, and in `_staged_field_values()` if it's a `Job` column (shared by `create_job`/`update_job_from_staged`, so both insert and update paths stay in sync automatically). `AnyHttpUrl` fields must be `str()`-cast before assigning to a `Text` column.
4. **Migration** — see the `db-migration` skill.
5. **docs/feed_spec.md** — update if the field changes an assumption documented there (optionality, format, etc.).

Gotchas:
- `hashing.py` hashes `record.source_record` (the raw, pre-coercion payload) — never switch it to hash normalized fields, or change-detection breaks silently. See that file's docstring for why.
- `JobFeedRecord.model_config` uses `extra="allow"`, so an unmapped element never rejects a record — it already surfaces via `ImportRun.unmapped_fields`. Don't build separate handling for that case.
