"""
Postgres persistence layer (Phase 1).

The package wraps three concerns:
  - session.py     SQLAlchemy engine + session factory + DATABASE_URL handling
  - models.py      ORM tables that mirror the Pydantic types in ../models.py
  - audit.py       Hash-chained audit log on Postgres (replaces in-memory)

The Pydantic types in ../models.py remain the API contract; the ORM tables
are an implementation detail. Helpers convert between the two.
"""
