"""add sar_detections.entity_id (cross-scene track continuity)

Revision ID: e6f0b8a91c4d
Revises: c84d1e9b6f30
Create Date: 2026-05-08 22:00:00.000000+00:00

Phase 4.x cross-scene track viz: every SAR detection — AIS-matched or
dark — has a corresponding entity in the maritime engine. matched_entity_id
is set ONLY for cooperative (AIS) targets; for dark vessels it stays null
so the frontend renders red. To draw multi-pass tracks for dark vessels
we need the underlying entity_id regardless of match type. This column
is set always at fusion time; nullable only for back-compat with existing
rows that haven't been re-fused.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e6f0b8a91c4d"
down_revision: Union[str, Sequence[str], None] = "c84d1e9b6f30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sar_detections",
        sa.Column("entity_id", sa.String(), nullable=True),
    )
    op.create_foreign_key(
        "sar_detections_entity_id_fkey",
        "sar_detections", "entities",
        ["entity_id"], ["entity_id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_sar_detections_entity_id", "sar_detections", ["entity_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_sar_detections_entity_id", table_name="sar_detections")
    op.drop_constraint("sar_detections_entity_id_fkey", "sar_detections",
                       type_="foreignkey")
    op.drop_column("sar_detections", "entity_id")
