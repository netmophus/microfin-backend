"""Crédit — décaissement multi-mode (caisse ou crédit direct sur un compte du tiers).

Deux modes, choisis au moment du décaissement : 'caisse' (D crédit / C caisse, inchangé) ou
'epargne' (D crédit / C le compte epargne.accounts choisi par le responsable — n'importe quel
produit, EAV/DAT/EPR, sans transiter par la caisse — journal OD, virement comptable interne).

`mode_decaissement` et `compte_destination_id` sont remplis dans TOUS les cas (y compris
'caisse', où compte_destination_id pointe vers le compte de caisse de l'agence utilisé) — pour
qu'un contrôleur sache après coup ce qui a été crédité sans devoir remonter le journal
comptable. Miroir de compte_credit_id (côté créance), posé en 0033.

Point OUVERT documenté dans conformite-credit.md : ces deux modes sont opérationnels, pas
réglementaires — aucune règle BCEAO connue n'impose l'un ou ne restreint l'autre.

Revision ID: 0035
Revises: 0034
Create Date: 2026-08-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.add_column(
        "applications",
        sa.Column(
            "mode_decaissement", sa.String(20), server_default=sa.text("'caisse'"), nullable=False
        ),
        schema="credit",
    )
    op.add_column(
        "applications", sa.Column("compte_destination_id", UUID, nullable=True), schema="credit"
    )
    op.create_foreign_key(
        "fk_credit_applications_compte_destination",
        "applications",
        "accounts",
        ["compte_destination_id"],
        ["id"],
        source_schema="credit",
        referent_schema="comptabilite",
    )
    op.create_check_constraint(
        "mode_decaissement",
        "applications",
        "mode_decaissement IN ('caisse', 'epargne')",
        schema="credit",
    )


def downgrade() -> None:
    op.drop_constraint("mode_decaissement", "applications", schema="credit", type_="check")
    op.drop_constraint(
        "fk_credit_applications_compte_destination",
        "applications",
        schema="credit",
        type_="foreignkey",
    )
    op.drop_column("applications", "compte_destination_id", schema="credit")
    op.drop_column("applications", "mode_decaissement", schema="credit")
