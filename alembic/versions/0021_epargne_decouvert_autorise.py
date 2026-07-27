"""Épargne E3 — paramètre produit « découvert autorisé ».

Le plancher d'un retrait est PARAMÉTRABLE par produit, pas en dur :
  - min_balance (déjà là, 0018) : solde minimum à garder pour maintenir le compte ouvert ;
  - decouvert_autorise (ici) : de combien le solde peut passer SOUS zéro (0 par défaut).
Disponible au retrait = solde − min_balance + decouvert_autorise.

Épargne à vue standard : les deux à 0 (le solde ne descend pas sous zéro). Certaines IMF encadrent
un petit découvert sur certains produits (comptes « découverts autorisés » 3021/3022 du plan) :
l'expert dira lesquels. Valeur PROVISOIRE (défaut 0, sûr).

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-27
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column(
            "decouvert_autorise", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        schema="epargne",
    )
    op.create_check_constraint(
        "decouvert_positif", "products", "decouvert_autorise >= 0", schema="epargne"
    )


def downgrade() -> None:
    op.drop_constraint("decouvert_positif", "products", schema="epargne", type_="check")
    op.drop_column("products", "decouvert_autorise", schema="epargne")
