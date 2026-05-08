"""add s2_scenes (Sentinel-2 optical catalog, phase 4.x)

Revision ID: a712b30d44e5
Revises: 99fec3a702aa
Create Date: 2026-05-08 17:30:00.000000+00:00

Companion to sar_scenes: parallel catalog table for Sentinel-2 MSI L2A
optical scenes. Same OData / Copernicus account, separate state machine
(discovered → downloaded → processed → failed). Used initially just for
discovery so we can render footprints alongside the SAR layer; Phase 4.y
adds download + chip extraction for visual confirmation of SAR detections.
"""
from __future__ import annotations

from typing import Sequence, Union

import geoalchemy2  # noqa: F401  (Geometry types referenced below)
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a712b30d44e5"
down_revision: Union[str, Sequence[str], None] = "99fec3a702aa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "s2_scenes",
        sa.Column("scene_id", sa.String(), nullable=False),
        sa.Column("platform", sa.String(length=8), nullable=False),       # S2A/S2B/S2C
        sa.Column("product_type", sa.String(length=16), nullable=False),  # MSIL2A
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "footprint",
            geoalchemy2.types.Geometry(
                geometry_type="POLYGON",
                srid=4326,
                dimension=2,
                from_text="ST_GeomFromEWKT",
                name="geometry",
                nullable=False,
            ),
            nullable=False,
        ),
        sa.Column("cloud_cover_pct", sa.Float(), nullable=True),
        sa.Column("raw_url", sa.String(), nullable=True),
        sa.Column("source_url", sa.String(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("failure_reason", sa.String(), nullable=True),
        sa.Column("attrs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "state IN ('discovered','downloaded','processed','failed')",
            name="s2_scenes_state_check",
        ),
        sa.PrimaryKeyConstraint("scene_id"),
    )
    # GeoAlchemy2 auto-creates idx_s2_scenes_footprint via CREATE TABLE.
    op.create_index("ix_s2_scenes_acquired_at", "s2_scenes", ["acquired_at"], unique=False)
    op.create_index("ix_s2_scenes_state", "s2_scenes", ["state"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_s2_scenes_state", table_name="s2_scenes")
    op.drop_index("ix_s2_scenes_acquired_at", table_name="s2_scenes")
    # idx_s2_scenes_footprint dropped automatically by drop_table.
    op.drop_table("s2_scenes")
