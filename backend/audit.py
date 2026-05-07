"""
Append-only, hash-chained audit log.

Every state change in the system passes through here. Each entry carries
the SHA-256 hash of the previous entry, so any tampering invalidates the
chain from that point forward. A production deployment also anchors the
chain head to a public timestamp authority daily.

This is the load-bearing piece that distinguishes a civilian platform
from a black-box military one. An oversight board with read access can
verify the full operational history.
"""

from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone
from threading import Lock
from typing import Any

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


class AuditLog:
    """In-memory chain. Production = append-only DB + nightly external anchor."""

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
        """Recompute every hash. Returns (ok, first_bad_seq_or_None)."""
        prev_hash = GENESIS_HASH
        for e in self._entries:
            recomputed = _hash_entry(
                e.seq, e.t, e.actor, e.event_type, e.payload, prev_hash
            )
            if recomputed != e.self_hash or e.prev_hash != prev_hash:
                return False, e.seq
            prev_hash = e.self_hash
        return True, None


# Global singleton for the MVP. Production = injected dependency.
audit_log = AuditLog()
