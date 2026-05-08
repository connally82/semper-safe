"""add sar_detections.vv_vh_ratio_db (multi-pol discrimination)

Revision ID: c84d1e9b6f30
Revises: a712b30d44e5
Create Date: 2026-05-08 21:00:00.000000+00:00

Phase 4.x: VH-pol amplitude is sampled at each detection centroid and
the VV/VH ratio (in dB) is persisted alongside the detection. Vessels
have high VV-low VH (ratio > ~6 dB); biological clutter (slicks, wind
roughness) has near-equal VV and VH so the ratio is < ~3 dB. The new
column is nullable for backward compatibility — existing rows retain
NULL and the frontend treats absent ratios as "single-pol scene".
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c84d1e9b6f30"
down_revision: Union[str, Sequence[str], None] = "a712b30d44e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sar_detections",
        sa.Column("vv_vh_ratio_db", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sar_detections", "vv_vh_ratio_db")
