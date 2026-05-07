"""
Append-only, hash-chained audit log.

Every state change in the system passes through here. Each entry carries
the SHA-256 hash of the previous entry, so any tampering invalidates the
chain from that point forward. A production deployment also anchors the
chain head to a public timestamp authority daily.

This is the load-bearing piece that distinguishes a civilian platform
from a black-box military one. An oversight board with read access can
verify the full operational history.

Phase 1 adds Postgres persistence. The chain is now enforced at three
layers:
  1. SHA-256 hash linking (any modified entry breaks recompute).
  2. UNIQUE(prev_hash) — the chain cannot fork even with concurrent writers.
  3. UNIQUE(self_hash) — every entry is unique by content.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from sqlalchemy import select

from models import AuditEntry


GENESIS_HASH = "0" * 64


def _canonical(payload: dict[str, Any]) -> str:
    """Stable JSON serialization for hashing."""
    return json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))


def _hash_entry(seq: int, t: datetime, actor: str, event_type: str,
                payload: dict[str, Any], prev_hash: str) -> str:
    h = hashlib.sha256()
    h.update(str(seq).encode())
    h.update(t.isoformat().encode())
    h.update(actor.encode())
    h.update(event_type.encode())
    h.update(_canonical(payload).encode())
    h.update(prev_hash.encode())
    return h.hexdigest()


class _InMemoryAuditLog:
    """Fallback used when DATABASE_URL is not set (e.g. local dev, CI)."""

    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []
        self._lock = Lock()

    def append(self, *, actor: str, event_type: str,
               payload: dict[str, Any]) -> AuditEntry:
        with self._lock:
            seq = len(self._entries)
            t = datetime.now(timezone.utc)
            prev_hash = self._entries[-1].self_hash if self._entries else GENESIS_HASH
            self_hash = _hash_entry(seq, t, actor, event_type, payload, prev_hash)
            entry = AuditEntry(
                seq=seq, t=t, actor=actor, event_type=event_type,
                payload=payload, prev_hash=prev_hash, self_hash=self_hash,
            )
            self._entries.append(entry)
            return entry

    def all(self) -> list[AuditEntry]:
        return list(self._entries)

    def head(self) -> str:
        return self._entries[-1].self_hash if self._entries else GENESIS_HASH

    def verify(self) -> tuple[bool, int | None]:
        prev_hash = GENESIS_HASH
        for e in self._entries:
            recomputed = _hash_entry(
                e.seq, e.t, e.actor, e.event_type, e.payload, prev_hash
            )
            if recomputed != e.self_hash or e.prev_hash != prev_hash:
                return False, e.seq
            prev_hash = e.self_hash
        return True, None


class _PostgresAuditLog:
    """DB-backed implementation. Hash chain integrity backed by UNIQUE constraints."""

    def __init__(self) -> None:
        # In-process lock to avoid hot retries when one FastAPI worker writes
        # rapidly. Cross-process safety still relies on UNIQUE(prev_hash).
        self._lock = Lock()

    def append(self, *, actor: str, event_type: str,
               payload: dict[str, Any]) -> AuditEntry:
        # Imported lazily so importing audit.py without DATABASE_URL doesn't
        # try to construct an engine.
        from db import models as dbm
        from db.session import session_scope

        with self._lock, session_scope() as s:
            last = s.execute(
                select(dbm.AuditEntryRow)
                .order_by(dbm.AuditEntryRow.seq.desc())
                .limit(1)
            ).scalar_one_or_none()

            seq = (last.seq + 1) if last else 0
            prev_hash = last.self_hash if last else GENESIS_HASH
            t = datetime.now(timezone.utc)
            self_hash = _hash_entry(seq, t, actor, event_type, payload, prev_hash)

            row = dbm.AuditEntryRow(
                seq=seq, t=t, actor=actor, event_type=event_type,
                payload=payload, prev_hash=prev_hash, self_hash=self_hash,
            )
            s.add(row)
            # session_scope commits on context exit
            return AuditEntry(
                seq=seq, t=t, actor=actor, event_type=event_type,
                payload=payload, prev_hash=prev_hash, self_hash=self_hash,
            )

    def all(self) -> list[AuditEntry]:
        from db import models as dbm
        from db.session import session_scope

        with session_scope() as s:
            rows = s.execute(
                select(dbm.AuditEntryRow).order_by(dbm.AuditEntryRow.seq.asc())
            ).scalars().all()
            return [
                AuditEntry(
                    seq=r.seq, t=r.t, actor=r.actor, event_type=r.event_type,
                    payload=r.payload, prev_hash=r.prev_hash, self_hash=r.self_hash,
                )
                for r in rows
            ]

    def head(self) -> str:
        from db import models as dbm
        from db.session import session_scope

        with session_scope() as s:
            last = s.execute(
                select(dbm.AuditEntryRow.self_hash)
                .order_by(dbm.AuditEntryRow.seq.desc())
                .limit(1)
            ).scalar_one_or_none()
            return last or GENESIS_HASH

    def verify(self) -> tuple[bool, int | None]:
        from db import models as dbm
        from db.session import session_scope

        with session_scope() as s:
            rows = s.execute(
                select(dbm.AuditEntryRow).order_by(dbm.AuditEntryRow.seq.asc())
            ).scalars().all()
            prev_hash = GENESIS_HASH
            for r in rows:
                recomputed = _hash_entry(
                    r.seq, r.t, r.actor, r.event_type, r.payload, prev_hash
                )
                if recomputed != r.self_hash or r.prev_hash != prev_hash:
                    return False, r.seq
                prev_hash = r.self_hash
            return True, None


def _build_audit_log():
    """Pick implementation based on environment.

    DATABASE_URL set → Postgres. Otherwise → in-memory (CI, local dev w/o DB).
    """
    if os.environ.get("DATABASE_URL"):
        return _PostgresAuditLog()
    return _InMemoryAuditLog()


# Global singleton — same name as before so callers don't change.
audit_log = _build_audit_log()
