"""Crédit CR4 — remboursements : encaissement d'une échéance, split capital/intérêts.

Quatrième bloc du module Crédit. Contenu :

  - credit.products : compte_produits_interets_id (7021, compte OFFICIEL direct — les
    intérêts perçus sont un produit de l'institution, pas une dette envers le tiers, donc
    AUCUN split membre/client ici, contrairement au capital).
  - credit.installments : gagne paid_at/paid_by + un CHECK sur status, vocabulaire connu
    maintenant : 'a_echoir' -> 'paye'. Toujours RIEN pour « en retard » — condition calculée
    à la lecture (due_date < aujourd'hui AND status='a_echoir'), pas un état stocké : le
    vocabulaire de pénalité appartient à CR5, pas deviné ici.
  - credit.repayments : le registre append-only des paiements (miroir epargne.movements) —
    installments reste le PLAN prévisionnel, repayments est l'historique de ce qui a
    réellement été encaissé, avec la référence de la pièce comptable qui l'a posé.

PAS de nouveau statut sur credit.applications (décision CR4, cohérente avec l'Épargne :
« vérité = Σ mouvements ») : un crédit soldé se déduit (aucune installment 'a_echoir'), il ne
se stocke pas.

La pièce comptable du remboursement (D CAISSE / C CREDIT / C PRODUITS_INTERETS, 2 ou 3 lignes
selon que l'échéance porte des intérêts) est posée par du code applicatif dédié
(ecritures.creer_brouillon/valider), PAS par le moteur générique poser_depuis_schema : ce
dernier applique un même montant à toutes les lignes d'un nombre de lignes fixe, ce qui ne
correspond pas à un remboursement (montants différents par ligne, ligne d'intérêts parfois
absente). Décision : ne pas complexifier un moteur partagé par 3 modules pour un cas
structurellement différent — voir credit/remboursement.py.

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")
FK_USER = "security.users.id"
FK_ACCOUNT = "comptabilite.accounts.id"


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("compte_produits_interets_id", UUID, nullable=True),
        schema="credit",
    )
    op.create_foreign_key(
        "fk_credit_products_compte_produits_interets",
        "products",
        "accounts",
        ["compte_produits_interets_id"],
        ["id"],
        source_schema="credit",
        referent_schema="comptabilite",
    )

    op.add_column("installments", sa.Column("paid_at", TS, nullable=True), schema="credit")
    op.add_column("installments", sa.Column("paid_by", UUID, nullable=True), schema="credit")
    op.create_foreign_key(
        "fk_credit_installments_paid_by",
        "installments",
        "users",
        ["paid_by"],
        ["id"],
        source_schema="credit",
        referent_schema="security",
    )
    op.create_check_constraint(
        "status", "installments", "status IN ('a_echoir', 'paye')", schema="credit"
    )

    op.create_table(
        "repayments",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("installment_id", UUID, nullable=False),
        sa.Column("application_id", UUID, nullable=False),
        sa.Column("montant_capital", sa.BigInteger(), nullable=False),
        sa.Column("montant_interets", sa.BigInteger(), nullable=False),
        sa.Column("montant_total", sa.BigInteger(), nullable=False),
        sa.Column("entry_id", UUID, nullable=False),
        sa.Column("paid_at", TS, server_default=NOW, nullable=False),
        sa.Column("paid_by", UUID, nullable=True),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("installment_id"),
        sa.ForeignKeyConstraint(
            ["installment_id"], ["credit.installments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["application_id"], ["credit.applications.id"]),
        sa.ForeignKeyConstraint(["entry_id"], ["comptabilite.journal_entries.id"]),
        sa.ForeignKeyConstraint(["paid_by"], [FK_USER]),
        sa.CheckConstraint("montant_capital >= 0", name="montant_capital_non_negatif"),
        sa.CheckConstraint("montant_interets >= 0", name="montant_interets_non_negatif"),
        sa.CheckConstraint("montant_total > 0", name="montant_total_positif"),
        sa.CheckConstraint(
            "montant_total = montant_capital + montant_interets", name="montant_total_coherent"
        ),
        schema="credit",
    )
    op.create_index(
        "ix_credit_repayments_application", "repayments", ["application_id"], schema="credit"
    )


def downgrade() -> None:
    op.drop_index("ix_credit_repayments_application", table_name="repayments", schema="credit")
    op.drop_table("repayments", schema="credit")
    op.drop_constraint("status", "installments", schema="credit", type_="check")
    op.drop_constraint(
        "fk_credit_installments_paid_by", "installments", schema="credit", type_="foreignkey"
    )
    op.drop_column("installments", "paid_by", schema="credit")
    op.drop_column("installments", "paid_at", schema="credit")
    op.drop_constraint(
        "fk_credit_products_compte_produits_interets",
        "products",
        schema="credit",
        type_="foreignkey",
    )
    op.drop_column("products", "compte_produits_interets_id", schema="credit")
