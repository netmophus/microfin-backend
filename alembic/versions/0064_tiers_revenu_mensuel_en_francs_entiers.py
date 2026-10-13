"""Tiers — le revenu mensuel estimé d'une personne physique passe en francs entiers (BIGINT).

`individual_profiles.monthly_income_estimate` était NUMERIC(18,2) : règle projet, un montant est un
entier de francs, jamais de décimales. C'est le dernier montant en NUMERIC de la base (le capital
social est passé en BIGINT en 0063).

Contrairement à 0063, les valeurs existantes sont ARRONDIES au franc : un revenu est une estimation
déclarative, pas une valeur comptable. Le downgrade revient à NUMERIC(18,2) sans perte (un entier
est représentable) ; les arrondis faits à l'upgrade, eux, ne se retrouvent pas.

Revision ID: 0064
Revises: 0063
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0064"
down_revision: str | None = "0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "individual_profiles",
        "monthly_income_estimate",
        existing_type=sa.Numeric(18, 2),
        type_=sa.BigInteger(),
        existing_nullable=True,
        postgresql_using="round(monthly_income_estimate)::bigint",
        schema="tiers",
    )


def downgrade() -> None:
    op.alter_column(
        "individual_profiles",
        "monthly_income_estimate",
        existing_type=sa.BigInteger(),
        type_=sa.Numeric(18, 2),
        existing_nullable=True,
        postgresql_using="monthly_income_estimate::numeric(18,2)",
        schema="tiers",
    )
