"""Documents — compteur des numéros de pièce propres aux documents (arrêté de caisse).

Schéma `documents`, table `numerotation` : un rang par (préfixe, année de la journée comptable).
Alloué par une seule instruction INSERT ... ON CONFLICT DO UPDATE (verrou de ligne) : deux arrêtés
simultanés ne reçoivent jamais le même numéro. Aucune donnée existante n'est touchée.

Revision ID: 0068
Revises: 0067
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0068"
down_revision: str | None = "0067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("numerotation", schema="documents"):
        return  # rejouable : la table existe déjà
    op.create_table(
        "numerotation",
        sa.Column("prefixe", sa.String(10), nullable=False),
        sa.Column("annee", sa.Integer(), nullable=False),
        sa.Column("last_value", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("prefixe", "annee"),
        sa.CheckConstraint("last_value > 0", name="ck_numerotation_positive"),
        schema="documents",
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS documents.numerotation")
