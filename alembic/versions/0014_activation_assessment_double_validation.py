"""Gate d'activation réel (T3c) — lien vers l'évaluation KYC + paramètre quatre-yeux.

Deux ajouts, aucune table :

1. tiers.tiers.activation_assessment_id (FK -> tiers.risk_assessments) : l'évaluation KYC EXACTE
   sur laquelle une activation s'est appuyée. Un inspecteur remonte de « activé le 12 mars » au
   snapshot de risque précis (score, règles, barème, version, provisoire ou non). NULL tant que la
   fiche n'est pas activée.

2. parameters.agencies.double_validation_kyc (bool, défaut TRUE) : le principe des quatre yeux
   (l'activateur n'est pas celui qui a vérifié la pièce), EXIGÉ par défaut, assouplissable PAR
   AGENCE — une petite agence rurale à agent unique serait sinon incapable d'activer. Toute
   activation en auto-validation (agence assouplie, même personne) est tracée dans l'audit.

DOWNGRADE : retire les deux colonnes.

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.add_column(
        "tiers",
        sa.Column("activation_assessment_id", UUID, nullable=True),
        schema="tiers",
    )
    op.create_foreign_key(
        "fk_tiers_activation_assessment_id_risk_assessments",
        "tiers",
        "risk_assessments",
        ["activation_assessment_id"],
        ["id"],
        source_schema="tiers",
        referent_schema="tiers",
    )
    op.add_column(
        "agencies",
        sa.Column(
            "double_validation_kyc", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        schema="parameters",
    )


def downgrade() -> None:
    op.drop_column("agencies", "double_validation_kyc", schema="parameters")
    op.drop_constraint(
        "fk_tiers_activation_assessment_id_risk_assessments",
        "tiers",
        schema="tiers",
        type_="foreignkey",
    )
    op.drop_column("tiers", "activation_assessment_id", schema="tiers")
