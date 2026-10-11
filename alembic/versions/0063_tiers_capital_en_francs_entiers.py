"""Tiers — le capital social d'une personne morale passe en francs entiers (BIGINT).

`legal_entity_profiles.capital_amount` était NUMERIC(18,2) : règle projet, un montant est un
entier de francs, jamais de décimales (comme l'épargne, le crédit et la caisse). Une valeur
« 1000000.00 » rechargée dans un formulaire d'édition n'est pas un entier.

Si une ligne portait de vrais centimes, la conversion en arrondirait la valeur : la migration
s'arrête AVANT de modifier quoi que ce soit, en disant combien de lignes sont en cause. On ne
devine pas un capital.

Revision ID: 0063
Revises: 0062
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0063"
down_revision: str | None = "0062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    avec_centimes = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM tiers.legal_entity_profiles "
                "WHERE capital_amount IS NOT NULL AND capital_amount <> trunc(capital_amount)"
            )
        )
        .scalar_one()
    )
    if avec_centimes:
        raise RuntimeError(
            f"{avec_centimes} capital(aux) avec des centimes non nuls : à examiner avant de "
            "passer capital_amount en francs entiers (la migration n'arrondit aucune valeur)."
        )
    op.alter_column(
        "legal_entity_profiles",
        "capital_amount",
        existing_type=sa.Numeric(18, 2),
        type_=sa.BigInteger(),
        existing_nullable=True,
        postgresql_using="capital_amount::bigint",
        schema="tiers",
    )


def downgrade() -> None:
    op.alter_column(
        "legal_entity_profiles",
        "capital_amount",
        existing_type=sa.BigInteger(),
        type_=sa.Numeric(18, 2),
        existing_nullable=True,
        postgresql_using="capital_amount::numeric(18,2)",
        schema="tiers",
    )
