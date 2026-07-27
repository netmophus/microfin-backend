"""Épargne — rattachement comptable du produit (compte de dette du plan).

Chaque produit d'épargne pointe vers le COMPTE DU PLAN qui porte la dette de l'IMF envers le
membre (classe 3, sens crédit) : épargne à vue -> 3111 (membres), DAT -> 3121, programmée -> 3131.
C'est le compte CRÉDITÉ au dépôt (et débité au retrait). Ce rattachement est une DONNÉE
PROVISOIRE, à valider par l'expert-comptable SFD (voir docs/conformite-comptable.md) ; nullable
tant qu'il n'est pas validé/renseigné.

Première pierre du pont comptable (E1) : le moteur d'écriture (E3) lira ce compte pour poser la
pièce équilibrée d'un dépôt/retrait. La distinction membre/client (3111 vs 3112) reste à trancher
et sera portée par les schémas d'écriture (E1).

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("compte_epargne_id", UUID, nullable=True),
        schema="epargne",
    )
    op.create_foreign_key(
        "fk_products_compte_epargne",
        "products",
        "accounts",
        ["compte_epargne_id"],
        ["id"],
        source_schema="epargne",
        referent_schema="comptabilite",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_products_compte_epargne", "products", schema="epargne", type_="foreignkey"
    )
    op.drop_column("products", "compte_epargne_id", schema="epargne")
