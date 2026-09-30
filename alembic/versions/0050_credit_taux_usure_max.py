"""Crédit — plafond de taux paramétrable par produit (taux d'usure).

`credit.products.taux_usure_max_bp` (points de base, NULLABLE, défaut NULL = pas de plafond
appliqué, comportement actuel inchangé). Prépare le terrain pour le jour où le taux d'usure
BCEAO officiel sera connu par produit, SANS inventer de chiffre ici — aucune valeur codée en
dur, aucun seed ne la renseigne. Vérifié en création/modification par le service applicatif
(gestion_produits.py), pas par un CHECK SQL : la valeur elle-même est une donnée métier
(peut changer), pas une règle structurelle comme le découvert nul de l'épargne (migration 0049).

Revision ID: 0050
Revises: 0049
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("taux_usure_max_bp", sa.Integer(), nullable=True),
        schema="credit",
    )


def downgrade() -> None:
    op.drop_column("products", "taux_usure_max_bp", schema="credit")
