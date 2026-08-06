"""Crédit CR5b — paiement partiel d'une échéance.

`installments` gagne UNE colonne : `montant_paye` (cumul encaissé, jamais négatif, jamais au-
delà de `total`). `total`/`capital`/`interets` ne bougent jamais — le plan d'origine reste
lisible. `solde_du` (`total - montant_paye`) n'est PAS stocké : calculé à la lecture, même
discipline que le solde épargne dérivé des mouvements.

`status` : CHECK étendu de `('a_echoir', 'paye')` à `('a_echoir', 'partiellement_paye',
'paye')`. Nouveau CHECK `statut_coherent_avec_montant_paye` — dernier rempart en base, miroir
des triggers de statut tiers déjà en place : un statut ne peut PAS diverger de son
`montant_paye` (a_echoir <=> 0 ; partiellement_paye <=> strictement entre 0 et total ; paye
<=> égal à total). Empêche un bug applicatif de laisser une ligne dans un état impossible.

`repayments` : la contrainte `UNIQUE(installment_id)` (un seul paiement par échéance, v1)
tombe — plusieurs paiements peuvent désormais viser la même échéance (paiements successifs
jusqu'à solde). Remplacée par un index simple, comme `application_id`.

Migration réversible : voir docs/conformite-credit.md pour la ventilation intérêts-d'abord
(déduite de montant_paye à la lecture, aucune colonne dédiée).

Revision ID: 0037
Revises: 0036
Create Date: 2026-08-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "installments",
        sa.Column("montant_paye", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        schema="credit",
    )

    op.drop_constraint("status", "installments", schema="credit", type_="check")
    op.create_check_constraint(
        "status",
        "installments",
        "status IN ('a_echoir', 'partiellement_paye', 'paye')",
        schema="credit",
    )
    op.create_check_constraint(
        "montant_paye_borne",
        "installments",
        "montant_paye >= 0 AND montant_paye <= total",
        schema="credit",
    )
    op.create_check_constraint(
        "statut_coherent_avec_montant_paye",
        "installments",
        "(status = 'a_echoir' AND montant_paye = 0) OR "
        "(status = 'partiellement_paye' AND montant_paye > 0 AND montant_paye < total) OR "
        "(status = 'paye' AND montant_paye = total)",
        schema="credit",
    )

    op.drop_constraint(
        "uq_repayments_installment_id", "repayments", schema="credit", type_="unique"
    )
    op.create_index(
        "ix_credit_repayments_installment", "repayments", ["installment_id"], schema="credit"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_credit_repayments_installment", table_name="repayments", schema="credit"
    )
    op.create_unique_constraint(
        "uq_repayments_installment_id", "repayments", ["installment_id"], schema="credit"
    )

    op.drop_constraint(
        "statut_coherent_avec_montant_paye", "installments", schema="credit", type_="check"
    )
    op.drop_constraint("montant_paye_borne", "installments", schema="credit", type_="check")
    op.drop_constraint("status", "installments", schema="credit", type_="check")
    op.create_check_constraint(
        "status", "installments", "status IN ('a_echoir', 'paye')", schema="credit"
    )

    op.drop_column("installments", "montant_paye", schema="credit")
