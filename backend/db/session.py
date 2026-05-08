"""
SQLAlchemy 2.0 engine + session factory.

DATABASE_URL is read from the environment. Locally:
  export DATABASE_URL='postgresql+psycopg://user:pass@host:5432/db?sslmode=require'

In production (Fly.io) we set this via `flyctl secrets set DATABASE_URL=...`.

Notes:
  - We use SQLAlchemy 2.0 style throughout (DeclarativeBase, Mapped, etc.).
  - psycopg3 is the driver — not psycopg2. Connection URL prefix matters:
      postgresql+psycopg   → psycopg3 (correct)
      postgresql+psycopg2  → psycopg2 (legacy, not installed)
      postgresql           → SQLAlchemy default → psycopg2 → ImportError
    We normalize the URL on import so a plain `postgresql://` string from
    Neon/Fly/Supabase still works.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlparse, urlunparse

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session


def _normalize_db_url(raw: str) -> str:
    """Force the psycopg3 driver into the URL.

    Connection strings from Neon, Fly, Supabase, and Heroku all start with
    `postgresql://` (or `postgres://`). SQLAlchemy needs an explicit driver
    suffix to pick psycopg3 over psycopg2.
    """
    parsed = urlparse(raw)
    scheme = parsed.scheme
    if scheme == "postgres":
        # Some providers (Heroku-style) still emit postgres://; SQLAlchemy
        # rejects this scheme. Rewrite to postgresql.
        scheme = "postgresql"
    if scheme == "postgresql":
        scheme = "postgresql+psycopg"
    return urlunparse(parsed._replace(scheme=scheme))


def _resolve_database_url() -> str:
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        raise RuntimeError(
            "DATABASE_URL is not set. Set it locally with `export DATABASE_URL=...` "
            "or in Fly with `flyctl secrets set DATABASE_URL=...`."
        )
    return _normalize_db_url(raw)


class Base(DeclarativeBase):
    """Declarative base for ORM models. Imported by db/models.py."""


# Engine + sessionmaker are created lazily so importing this module without
# DATABASE_URL set (e.g. for ruff lint, simple unit tests) doesn't error.
_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        url = _resolve_database_url()
        # Pool sized for the AIS-ingest load profile:
        #   - AISStream pushes ~3 messages/sec at peak.
        #   - Each ingest does ~4 DB ops (put_observation + put_entity +
        #     2 audit_log.append calls), each its own session_scope.
        #   - That's ~12 short-lived connection acquires/sec sustained.
        #   - + the gap sweeper + retention loop + HTTP request handlers.
        #
        # SQLAlchemy default pool_size=5 + max_overflow=10 was too small
        # under load: requests piled up waiting for connections, /health
        # eventually hung, Fly's health check tripped, machine got marked
        # unhealthy. Bumping to 20+30 = up to 50 concurrent connections.
        # Neon's free tier allows 100 concurrent connections, so we're
        # well within budget.
        #
        # pool_timeout = 10s: after that, raise instead of waiting forever.
        # That converts a deadlock into a 500 the operator can see.
        # pool_recycle = 1800: Neon kills idle connections after a while;
        # recycle proactively so we don't trip pool_pre_ping mid-request.
        _engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_size=20,
            max_overflow=30,
            pool_timeout=10,
            pool_recycle=1800,
            future=True,
        )
        _SessionLocal = sessionmaker(
            bind=_engine, autoflush=False, autocommit=False, expire_on_commit=False,
        )
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """Yield a transactional session. Commits on success, rolls back on error."""
    get_engine()  # ensures _SessionLocal is initialized
    assert _SessionLocal is not None
    s = _SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
