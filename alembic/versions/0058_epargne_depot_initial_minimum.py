"""Épargne — paramètre produit « dépôt initial minimum ».

Montant minimum du PREMIER dépôt sur un compte jamais mouvementé, en francs entiers. Défaut 0 =
aucune contrainte (comportement d'avant). L'ouverture du compte reste à solde 0 : la règle est
portée par `guichet.deposer()`, pas par `ouvrir_compte()`.

Le « solde plancher » demandé en même temps N'AJOUTE AUCUNE colonne : il existe déjà
(`min_balance`, migration 0018, CHECK >= 0, appliqué par `guichet.retirer()` et par le
prélèvement automatique du crédit). Ce lot ne fait que l'exposer à l'API et à l'écran.

Revision ID: 0058
Revises: 0057
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0058"
down_revision: str | None = "0057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column(
            "depot_initial_minimum", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        schema="epargne",
    )
    op.create_check_constraint(
        "depot_initial_minimum_positif",
        "products",
        "depot_initial_minimum >= 0",
        schema="epargne",
    )


def downgrade() -> None:
    op.drop_constraint(
        "depot_initial_minimum_positif", "products", schema="epargne", type_="check"
    )
    op.drop_column("products", "depot_initial_minimum", schema="epargne")
