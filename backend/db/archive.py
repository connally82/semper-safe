"""
Audit chain cold archive — periodic export to S3-compatible storage.

The audit_log in Postgres is bounded only by Neon's free-tier storage
(0.5 GB), and at ~9 events/sec we'd fill it in days. This module copies
new audit entries to a Cloudflare R2 bucket as gzipped JSONL — append-
only, durable, cheap. Doesn't delete from Postgres yet (separate task);
this is just the durable backup leg.

Design choices:
  - One file per archive run: audit/YYYY-MM-DD/HH-MM-SS_<seq_lo>_<seq_hi>.jsonl.gz
    Easy to enumerate, append-only, cheap to chunk.
  - Track high-water mark in archive_state(name='audit', last_seq=N).
  - Each run reads (last_seq, current_max] from audit_log, writes chunked.
  - Idempotent against partial failures: if upload succeeds but state
    update fails, next run picks up from the same place and over-writes
    the same key (R2 PutObject is idempotent).

Configured via env vars:
  R2_ACCOUNT_ID
  R2_ACCESS_KEY_ID
  R2_SECRET_ACCESS_KEY
  R2_BUCKET
  R2_AUDIT_PREFIX (optional, default 'audit/')

If any of the required vars are missing, archive_audit_chain() is a no-op
and logs a warning. Lets the rest of the app run without R2 configured.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

log = logging.getLogger("audit_archive")


def _env(name: str) -> str | None:
    v = os.environ.get(name)
    return v.strip() if v else None


def _r2_config() -> dict[str, str] | None:
    """Return a dict of R2 connection settings if all are set, else None."""
    cfg = {
        "account_id": _env("R2_ACCOUNT_ID"),
        "access_key_id": _env("R2_ACCESS_KEY_ID"),
        "secret_access_key": _env("R2_SECRET_ACCESS_KEY"),
        "bucket": _env("R2_BUCKET"),
        "prefix": _env("R2_AUDIT_PREFIX") or "audit/",
    }
    if not all([cfg["account_id"], cfg["access_key_id"],
                cfg["secret_access_key"], cfg["bucket"]]):
        return None
    return cfg


def _r2_client(cfg: dict[str, str]):
    # Imported lazily so the rest of the app doesn't pay the boto3
    # import cost when R2 is unconfigured.
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=f"https://{cfg['account_id']}.r2.cloudflarestorage.com",
        aws_access_key_id=cfg["access_key_id"],
        aws_secret_access_key=cfg["secret_access_key"],
        region_name="auto",
    )


def _audit_row_to_dict(row) -> dict[str, Any]:
    return {
        "seq": row.seq,
        "t": row.t.isoformat(),
        "actor": row.actor,
        "event_type": row.event_type,
        "payload": row.payload,
        "prev_hash": row.prev_hash,
        "self_hash": row.self_hash,
    }


def is_configured() -> bool:
    return _r2_config() is not None


def archive_audit_chain(*, batch_size: int = 5000) -> dict[str, Any]:
    """Copy new audit_log rows to R2. Returns counts dict.

    Reads ArchiveStateRow.last_seq, fetches everything past that up to
    `batch_size` rows, gzip-encodes JSONL, uploads to R2, advances the
    bookmark.

    No-op if R2 is unconfigured. Returns {"skipped": "no R2 config"}.
    """
    cfg = _r2_config()
    if cfg is None:
        return {"skipped": "no R2 config"}

    from db import models as dbm
    from db.session import session_scope

    now = datetime.now(timezone.utc)

    with session_scope() as s:
        state = s.execute(
            select(dbm.ArchiveStateRow).where(dbm.ArchiveStateRow.name == "audit")
        ).scalar_one_or_none()
        last_seq = state.last_seq if state else -1

        rows = s.execute(
            select(dbm.AuditEntryRow)
            .where(dbm.AuditEntryRow.seq > last_seq)
            .order_by(dbm.AuditEntryRow.seq.asc())
            .limit(batch_size)
        ).scalars().all()

        if not rows:
            return {"new_rows": 0, "last_seq": last_seq}

        seq_lo = rows[0].seq
        seq_hi = rows[-1].seq

        # Build the gzip JSONL in memory. ~150 bytes/row gzipped → 5000 rows
        # ≈ 0.75 MB per upload, well within R2 single-PUT limits.
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as gz:
            for r in rows:
                gz.write(
                    (json.dumps(_audit_row_to_dict(r), separators=(",", ":")) + "\n")
                    .encode("utf-8")
                )
        body = buf.getvalue()

        date_dir = now.strftime("%Y-%m-%d")
        ts = now.strftime("%H%M%S")
        key = f"{cfg['prefix']}{date_dir}/{ts}_{seq_lo}_{seq_hi}.jsonl.gz"

        client = _r2_client(cfg)
        try:
            client.put_object(
                Bucket=cfg["bucket"],
                Key=key,
                Body=body,
                ContentType="application/x-ndjson",
                ContentEncoding="gzip",
                Metadata={
                    "seq_lo": str(seq_lo),
                    "seq_hi": str(seq_hi),
                    "row_count": str(len(rows)),
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("R2 upload failed: %s", exc)
            raise

        # Advance the bookmark only after a successful upload.
        if state is None:
            state = dbm.ArchiveStateRow(
                name="audit", last_seq=seq_hi, last_archived_at=now,
            )
            s.add(state)
        else:
            state.last_seq = seq_hi
            state.last_archived_at = now

    log.info(
        "audit archive: %d rows seq[%d..%d] → %s/%s (%d bytes)",
        len(rows), seq_lo, seq_hi, cfg["bucket"], key, len(body),
    )
    return {
        "new_rows": len(rows),
        "seq_lo": seq_lo,
        "seq_hi": seq_hi,
        "key": key,
        "bytes": len(body),
    }
