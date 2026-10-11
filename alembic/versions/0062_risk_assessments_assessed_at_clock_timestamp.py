"""Archives de risque — `assessed_at` prend l'horloge réelle, pas celle de la transaction.

`NOW()` est figé au début de la transaction : deux évaluations d'une même fiche dans une même
transaction (une personne liée dont le niveau change, puis la PM qui en hérite ; un test) recevaient
le même horodatage, et « la dernière archive » dépendait d'un tri sur un UUID aléatoire.
`clock_timestamp()` donne l'instant réel de l'insertion : l'ordre est déterministe.

Pas de backfill : les archives existantes gardent leur horodatage (seule la valeur par défaut des
FUTURES lignes change ; la table est append-only).

Revision ID: 0062
Revises: 0061
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0062"
down_revision: str | None = "0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "risk_assessments",
        "assessed_at",
        existing_type=sa.TIMESTAMP(timezone=True),
        existing_nullable=False,
        server_default=sa.text("clock_timestamp()"),
        schema="tiers",
    )


def downgrade() -> None:
    op.alter_column(
        "risk_assessments",
        "assessed_at",
        existing_type=sa.TIMESTAMP(timezone=True),
        existing_nullable=False,
        server_default=sa.text("NOW()"),
        schema="tiers",
    )
