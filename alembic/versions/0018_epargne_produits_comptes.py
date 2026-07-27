"""Épargne E0+E2 — produits d'épargne, comptes d'épargne et registre des mouvements.

Premier bloc du module Épargne, posé sur le socle comptable. Contenu :

  - epargne.products : ce que l'IMF propose (à vue, terme, programmée). DONNÉE provisoire,
    comme le plan de comptes et la grille KYC.
  - epargne.accounts : le compte d'un MEMBRE. Rattaché à un tiers, à un produit, à une agence
    (cloisonnement). Porte un SOLDE-CACHE (`balance`) : un reflet pour l'affichage. La VÉRITÉ
    est la somme des mouvements — le cache se vérifie, ne se croit pas (décision d'archi).
    GATE KYC au niveau BASE : un trigger refuse la CRÉATION d'un compte si le membre n'est pas
    'actif' (un prospect n'a pas de compte). Il ne mord qu'à l'INSERT : un compte existant
    survit si le membre redevient prospect/suspendu plus tard (interaction avec le cycle de vie
    des tiers). Le service double ce garde-fou par une erreur métier claire.
  - epargne.movements : le registre APPEND-ONLY du compte. Chaque mouvement porte son sens
    (credit = augmente le solde membre / debit = le diminue), son montant ENTIER (XOF, BIGINT),
    le solde APRÈS lui (snapshot), et le lien vers la pièce comptable équilibrée (rempli en E3).
    Immuable par trigger (mécanisme du journal d'audit) : un mouvement ne se perd ni ne se
    réécrit.
  - epargne.numbering_sequences : numéro de compte atomique, sans trou (patron des tiers).

Ce bloc N'écrit PAS d'argent : l'ouverture crée un compte à solde 0. Les opérations (dépôt,
retrait) qui remplissent movements ET posent l'écriture comptable, dans une seule transaction,
sont le bloc E3.

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")
FK_USER = "security.users.id"


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS epargne")

    # --- E0 : produits d'épargne (donnée provisoire) ----------------------------------------
    op.create_table(
        "products",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("code", sa.String(20), nullable=False),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("currency", sa.CHAR(3), server_default=sa.text("'XOF'"), nullable=False),
        sa.Column("min_balance", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_provisional", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
        sa.ForeignKeyConstraint(["created_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["updated_by"], [FK_USER]),
        sa.CheckConstraint("type IN ('a_vue', 'terme', 'programmee')", name="type"),
        sa.CheckConstraint("min_balance >= 0", name="min_balance_positif"),
        schema="epargne",
    )

    # --- Numérotation atomique des comptes (patron des tiers) --------------------------------
    op.create_table(
        "numbering_sequences",
        sa.Column("prefix", sa.String(10), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("last_value", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("prefix", "year"),
        schema="epargne",
    )

    # --- E2 : compte d'épargne d'un membre ---------------------------------------------------
    op.create_table(
        "accounts",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("account_number", sa.String(30), nullable=False),
        sa.Column("product_id", UUID, nullable=False),
        sa.Column("tier_id", UUID, nullable=False),  # le membre (tiers)
        sa.Column("agency_id", UUID, nullable=False),  # cloisonnement
        sa.Column("currency", sa.CHAR(3), server_default=sa.text("'XOF'"), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'actif'"), nullable=False),
        # SOLDE-CACHE : reflet pour l'affichage. La vérité est SUM(movements).
        sa.Column("balance", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("opened_at", TS, server_default=NOW, nullable=False),
        sa.Column("opened_by", UUID, nullable=True),
        sa.Column("closed_at", TS, nullable=True),
        sa.Column("closed_by", UUID, nullable=True),
        sa.Column("closure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_number"),
        sa.ForeignKeyConstraint(["product_id"], ["epargne.products.id"]),
        sa.ForeignKeyConstraint(["tier_id"], ["tiers.tiers.id"]),
        sa.ForeignKeyConstraint(["agency_id"], ["parameters.agencies.id"]),
        sa.ForeignKeyConstraint(["opened_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["closed_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["created_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["updated_by"], [FK_USER]),
        sa.CheckConstraint("status IN ('actif', 'cloture')", name="status"),
        schema="epargne",
    )
    op.create_index("ix_epargne_accounts_tier", "accounts", ["tier_id"], schema="epargne")
    op.create_index("ix_epargne_accounts_agency", "accounts", ["agency_id"], schema="epargne")

    # --- Gate KYC au niveau BASE : pas de compte sur un non-membre-actif (dernier rempart) ---
    op.execute(
        """
        CREATE FUNCTION epargne.compte_exige_membre_actif() RETURNS trigger AS $$
        DECLARE st text;
        BEGIN
          SELECT status INTO st FROM tiers.tiers WHERE id = NEW.tier_id;
          IF st IS DISTINCT FROM 'actif' THEN
            RAISE EXCEPTION
              'Compte d''epargne refuse : le membre (tier=%) n''est pas actif (statut=%)',
              NEW.tier_id, st
              USING ERRCODE = 'check_violation';
          END IF;
          RETURN NEW;
        END $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_compte_membre_actif
          BEFORE INSERT ON epargne.accounts
          FOR EACH ROW EXECUTE FUNCTION epargne.compte_exige_membre_actif();
        """
    )

    # --- Registre des mouvements (APPEND-ONLY) -----------------------------------------------
    op.create_table(
        "movements",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("account_id", UUID, nullable=False),
        # sens vu du MEMBRE : credit augmente son solde (dépôt), debit le diminue (retrait).
        sa.Column("sens", sa.String(6), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),  # XOF entier, > 0
        sa.Column("balance_after", sa.BigInteger(), nullable=False),  # snapshot après ce mvt
        sa.Column("operation_type", sa.String(20), nullable=False),  # depot, retrait, interet…
        # Lien vers la pièce comptable équilibrée (posée dans la MÊME transaction en E3).
        sa.Column("journal_entry_id", UUID, nullable=True),
        sa.Column("label", sa.String(300), nullable=True),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        # RESTRICT : un compte porteur de mouvements ne se supprime pas (il n'y a pas de suppression
        # de compte de toute façon — clôture logique).
        sa.ForeignKeyConstraint(["account_id"], ["epargne.accounts.id"]),
        sa.ForeignKeyConstraint(
            ["journal_entry_id"], ["comptabilite.journal_entries.id"]
        ),
        sa.ForeignKeyConstraint(["created_by"], [FK_USER]),
        sa.CheckConstraint("sens IN ('credit', 'debit')", name="sens"),
        sa.CheckConstraint("amount > 0", name="montant_positif"),
        schema="epargne",
    )
    op.create_index("ix_epargne_movements_account", "movements", ["account_id"], schema="epargne")

    # --- Immuabilité des mouvements : append-only (mécanisme du journal d'audit) -------------
    op.execute(
        """
        CREATE FUNCTION epargne.mouvement_immuable() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'Mouvement d''epargne immuable : ni modification ni suppression'
            USING ERRCODE = 'restrict_violation';
        END $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_mouvement_immuable
          BEFORE UPDATE OR DELETE ON epargne.movements
          FOR EACH ROW EXECUTE FUNCTION epargne.mouvement_immuable();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_mouvement_immuable ON epargne.movements")
    op.execute("DROP FUNCTION IF EXISTS epargne.mouvement_immuable()")
    op.drop_table("movements", schema="epargne")
    op.execute("DROP TRIGGER IF EXISTS trg_compte_membre_actif ON epargne.accounts")
    op.execute("DROP FUNCTION IF EXISTS epargne.compte_exige_membre_actif()")
    op.drop_table("accounts", schema="epargne")
    op.drop_table("numbering_sequences", schema="epargne")
    op.drop_table("products", schema="epargne")
    op.execute("DROP SCHEMA IF EXISTS epargne")
