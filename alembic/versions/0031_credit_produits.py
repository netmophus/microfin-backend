"""Crédit CR0 — référentiel : produits de crédit paramétrables.

Premier bloc du module Crédit (individuel simple, échéances fixes, ouvert aux membres ET
clients — pas de groupe/caution solidaire pour l'instant). Contient UNIQUEMENT le référentiel
produit — demandes, décision, décaissement, échéancier réel : blocs suivants (CR1+).

Frontière MÉCANIQUE / VALEURS (même principe que l'épargne E5) : taux, périodicité, méthode
d'amortissement, base jours, arrondi sont des VALEURS provisoires, à valider par l'expert
(docs/conformite-credit.md). Les DEUX méthodes d'amortissement (capital constant / échéances
constantes — des courbes de remboursement radicalement différentes pour un même taux) sont
posées ici comme choix paramétrable par produit ; le moteur de calcul (CR2) implémentera les
deux, aucune n'est favorisée en dur.

Rattachement classe 20 (compte membre / compte client) vers les extensions à 6 chiffres créées
en parallèle sous 2022/2031 (voir docs/conformite-credit.md) — pas de migration pour elles,
posées via le Bloc 2 (import CSV) comme caisse/parts.

Revision ID: 0031
Revises: 0030
Create Date: 2026-08-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")
FK_USER = "security.users.id"


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS credit")

    op.create_table(
        "products",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("code", sa.String(20), nullable=False),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_provisional", sa.Boolean(), server_default=sa.true(), nullable=False),
        # Rattachement classe 20 (extension à 6 chiffres) — membre/client, comme l'épargne.
        sa.Column("compte_credit_membre_id", UUID, nullable=True),
        sa.Column("compte_credit_client_id", UUID, nullable=True),
        # Taux/échéancier — PROVISOIRES, défaut « taux 0 » = pas d'intérêt tant que non fixé.
        sa.Column("taux_bp", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "periodicite", sa.String(20), server_default=sa.text("'mensuelle'"), nullable=False
        ),
        sa.Column(
            "methode_amortissement",
            sa.String(20),
            server_default=sa.text("'echeance_constante'"),
            nullable=False,
        ),
        sa.Column("base_jours", sa.Integer(), server_default=sa.text("360"), nullable=False),
        sa.Column(
            "regle_arrondi", sa.String(20), server_default=sa.text("'plus_proche'"), nullable=False
        ),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
        sa.ForeignKeyConstraint(["compte_credit_membre_id"], ["comptabilite.accounts.id"]),
        sa.ForeignKeyConstraint(["compte_credit_client_id"], ["comptabilite.accounts.id"]),
        sa.ForeignKeyConstraint(["created_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["updated_by"], [FK_USER]),
        sa.CheckConstraint(
            "methode_amortissement IN ('capital_constant', 'echeance_constante')",
            name="methode_amortissement",
        ),
        sa.CheckConstraint(
            "periodicite IN ('mensuelle', 'trimestrielle', 'annuelle')", name="periodicite"
        ),
        sa.CheckConstraint("regle_arrondi IN ('plus_proche', 'plancher')", name="regle_arrondi"),
        schema="credit",
    )


def downgrade() -> None:
    op.drop_table("products", schema="credit")
    op.execute("DROP SCHEMA IF EXISTS credit")
