"""Épargne E5 — intérêts : paramètres de calcul (par produit) + calculs archivés (anti-double).

Frontière MÉCANIQUE / VALEURS : la mécanique est ici (paramètres, moteur, batch, écriture,
archivage) ; les VALEURS (taux, périodicité, méthode de calcul du solde, base jours, arrondi,
seuil, comptes) sont des DONNÉES provisoires, validées par l'expert (docs/conformite-comptable.md).

  - epargne.products : + paramètres d'intérêt (taux en POINTS DE BASE entiers — pas de flottant),
    tous provisoires, défaut « taux 0 » (pas d'intérêt tant que non paramétré).
  - epargne.interet_calculs : la photographie APPEND-ONLY d'un calcul, par compte et par PÉRIODE.
    Rejouable (comme le score KYC) : mêmes entrées archivées -> même montant. Immuable (trigger).
    GARDE-FOU SACRÉ anti-double-versement : UNIQUE (account_id, periode) — un compte ne peut pas
    être crédité deux fois pour la même période, même si le batch est relancé (dernier rempart).

Revision ID: 0023
Revises: 0022
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")
FK_USER = "security.users.id"


def upgrade() -> None:
    # --- Paramètres d'intérêt du produit (provisoires) --------------------------------------
    op.add_column(
        "products",
        sa.Column("taux_bp", sa.Integer(), server_default=sa.text("0"), nullable=False),
        schema="epargne",
    )
    op.add_column(
        "products",
        sa.Column(
            "periodicite", sa.String(20), server_default=sa.text("'annuelle'"), nullable=False
        ),
        schema="epargne",
    )
    op.add_column(
        "products",
        sa.Column(
            "methode_calcul_solde",
            sa.String(20),
            server_default=sa.text("'fin_periode'"),
            nullable=False,
        ),
        schema="epargne",
    )
    op.add_column(
        "products",
        sa.Column("base_jours", sa.Integer(), server_default=sa.text("360"), nullable=False),
        schema="epargne",
    )
    op.add_column(
        "products",
        sa.Column(
            "regle_arrondi", sa.String(20), server_default=sa.text("'plus_proche'"), nullable=False
        ),
        schema="epargne",
    )
    op.add_column(
        "products",
        sa.Column(
            "solde_minimum_remunere", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        schema="epargne",
    )
    op.add_column(
        "products",
        sa.Column("compte_charge_interet_id", UUID, nullable=True),
        schema="epargne",
    )
    op.create_foreign_key(
        "fk_products_compte_charge_interet",
        "products",
        "accounts",
        ["compte_charge_interet_id"],
        ["id"],
        source_schema="epargne",
        referent_schema="comptabilite",
    )
    op.create_check_constraint(
        "periodicite",
        "products",
        "periodicite IN ('mensuelle', 'trimestrielle', 'annuelle')",
        schema="epargne",
    )
    op.create_check_constraint(
        "methode_calcul_solde",
        "products",
        "methode_calcul_solde IN ('min_periode', 'moyen_quotidien', 'fin_periode')",
        schema="epargne",
    )
    op.create_check_constraint(
        "regle_arrondi",
        "products",
        "regle_arrondi IN ('plus_proche', 'plancher')",
        schema="epargne",
    )

    # --- Calculs d'intérêts archivés (append-only, un par compte et par période) ------------
    op.create_table(
        "interet_calculs",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("account_id", UUID, nullable=False),
        sa.Column("periode", sa.String(20), nullable=False),  # ex. « 2026-Q3 », clé anti-double
        sa.Column("base_solde", sa.BigInteger(), nullable=False),  # solde retenu (méthode)
        sa.Column("methode", sa.String(20), nullable=False),
        sa.Column("taux_bp", sa.Integer(), nullable=False),
        sa.Column("base_jours", sa.Integer(), nullable=False),
        sa.Column("jours", sa.Integer(), nullable=False),
        sa.Column("montant", sa.BigInteger(), nullable=False),  # XOF entier, >= 0
        # Photographie complète pour rejouer/vérifier (trajectoire, bornes, arrondi…).
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        # Pièce de versement (NULL si montant = 0 : compte traité, rien à verser).
        sa.Column("journal_entry_id", UUID, nullable=True),
        sa.Column("computed_at", TS, server_default=NOW, nullable=False),
        sa.Column("computed_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        # LE garde-fou anti-double-versement : un compte, une période, un seul calcul.
        sa.UniqueConstraint("account_id", "periode", name="uq_interet_periode"),
        sa.ForeignKeyConstraint(["account_id"], ["epargne.accounts.id"]),
        sa.ForeignKeyConstraint(
            ["journal_entry_id"], ["comptabilite.journal_entries.id"]
        ),
        sa.ForeignKeyConstraint(["computed_by"], [FK_USER]),
        sa.CheckConstraint("montant >= 0", name="montant_positif"),
        schema="epargne",
    )
    op.create_index(
        "ix_interet_calculs_account", "interet_calculs", ["account_id"], schema="epargne"
    )

    # --- Immuabilité (append-only) : un calcul archivé ne se réécrit pas --------------------
    op.execute(
        """
        CREATE FUNCTION epargne.interet_calcul_immuable() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'Calcul d''interet immuable : ni modification ni suppression'
            USING ERRCODE = 'restrict_violation';
        END $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_interet_calcul_immuable
          BEFORE UPDATE OR DELETE ON epargne.interet_calculs
          FOR EACH ROW EXECUTE FUNCTION epargne.interet_calcul_immuable();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_interet_calcul_immuable ON epargne.interet_calculs")
    op.execute("DROP FUNCTION IF EXISTS epargne.interet_calcul_immuable()")
    op.drop_table("interet_calculs", schema="epargne")
    op.drop_constraint(
        "fk_products_compte_charge_interet", "products", schema="epargne", type_="foreignkey"
    )
    for col in (
        "compte_charge_interet_id",
        "solde_minimum_remunere",
        "regle_arrondi",
        "base_jours",
        "methode_calcul_solde",
        "periodicite",
        "taux_bp",
    ):
        op.drop_column("products", col, schema="epargne")
