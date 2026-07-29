"""Tiers PS1 — parts sociales (sociétariat) : config, solde de parts, registre des mouvements.

Trois tables dans le schéma `tiers` :
  - share_parameters : CONFIG d'INSTITUTION (une ligne), PROVISOIRE — valeur d'une part, minimum
    pour adhérer, remboursable, moment de l'adhésion (souscription | libération), rattachements
    1021/1022. Valeurs validées par l'expert / les statuts (comme les taux d'épargne).
  - member_shares : SOLDE de parts par tier (le CACHE) — libérées (capital réel -> 1021) et non
    libérées (promesses -> 1022). Vérité = Σ des mouvements ci-dessous. Verrou FOR UPDATE à l'op.
  - share_subscriptions : REGISTRE APPEND-ONLY des mouvements (souscription / libération /
    souscription_comptant / remboursement), chacun relié à sa pièce comptable, avec l'agence de
    l'acte (résout la CAISSE, cloisonne). Immuable (trigger), comme epargne.movements.

FRONTIÈRE mécanique / valeurs : la mécanique est ici ; les VALEURS sont dans share_parameters,
PROVISOIRES (docs/conformite-comptable.md). Montants XOF ENTIERS (BIGINT), jamais de décimale.

Revision ID: 0026
Revises: 0025
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")
FK_USER = "security.users.id"
FK_ACCOUNT = "comptabilite.accounts.id"


def upgrade() -> None:
    # --- (a) Config d'institution (PROVISOIRE) : une ligne, valeurs de l'expert/statuts ----------
    op.create_table(
        "share_parameters",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("unit_value", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("minimum_shares", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("is_refundable", sa.Boolean(), nullable=False, server_default=sa.true()),
        # Moment de l'adhésion — ⚠️ À VALIDER (statuts). Défaut prudent : à la libération.
        sa.Column(
            "membership_on", sa.String(20), nullable=False, server_default=sa.text("'liberation'")
        ),
        sa.Column("compte_parts_liberees_id", UUID, nullable=True),  # 1021 (provisoire)
        sa.Column("compte_parts_non_liberees_id", UUID, nullable=True),  # 1022 (provisoire)
        sa.Column("is_provisional", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["compte_parts_liberees_id"], [FK_ACCOUNT]),
        sa.ForeignKeyConstraint(["compte_parts_non_liberees_id"], [FK_ACCOUNT]),
        sa.ForeignKeyConstraint(["created_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["updated_by"], [FK_USER]),
        sa.CheckConstraint(
            "membership_on IN ('souscription','liberation')", name="membership_on"
        ),
        sa.CheckConstraint("unit_value >= 0 AND minimum_shares >= 1", name="valeurs_positives"),
        schema="tiers",
    )

    # --- (b) Solde de parts par tier (le CACHE) : libérées / non libérées -----------------------
    op.create_table(
        "member_shares",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("tier_id", UUID, nullable=False),
        sa.Column("shares_liberees", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "shares_non_liberees", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tier_id"], ["tiers.tiers.id"]),
        sa.ForeignKeyConstraint(["updated_by"], [FK_USER]),
        sa.UniqueConstraint("tier_id", name="uq_member_shares_tier"),  # un solde par tier
        sa.CheckConstraint(
            "shares_liberees >= 0 AND shares_non_liberees >= 0", name="parts_non_negatives"
        ),
        schema="tiers",
    )

    # --- (c) Registre APPEND-ONLY des mouvements de parts ---------------------------------------
    op.create_table(
        "share_subscriptions",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("tier_id", UUID, nullable=False),
        # Agence de l'acte : résout la CAISSE (5721), cloisonne, audit.
        sa.Column("agency_id", UUID, nullable=False),
        sa.Column("type", sa.String(30), nullable=False),  # 'souscription_comptant' = 21 car.
        sa.Column("shares_count", sa.Integer(), nullable=False),
        sa.Column("unit_value", sa.BigInteger(), nullable=False),  # figée au moment (snapshot)
        sa.Column("amount", sa.BigInteger(), nullable=False),  # = shares_count * unit_value
        sa.Column("journal_entry_id", UUID, nullable=True),
        sa.Column("certificate_number", sa.String(30), nullable=True),  # PDF -> PS4
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tier_id"], ["tiers.tiers.id"]),
        sa.ForeignKeyConstraint(["agency_id"], ["parameters.agencies.id"]),
        sa.ForeignKeyConstraint(["journal_entry_id"], ["comptabilite.journal_entries.id"]),
        sa.ForeignKeyConstraint(["created_by"], [FK_USER]),
        sa.CheckConstraint(
            "type IN ('souscription','liberation','souscription_comptant','remboursement')",
            name="type",
        ),
        sa.CheckConstraint("shares_count > 0", name="parts_positives"),
        sa.CheckConstraint("amount >= 0", name="montant_non_negatif"),
        schema="tiers",
    )
    op.create_index(
        "ix_share_subscriptions_tier", "share_subscriptions", ["tier_id"], schema="tiers"
    )

    # --- (d) Immuabilité append-only (même mécanisme que epargne.movements / audit) -------------
    op.execute(
        """
        CREATE FUNCTION tiers.share_subscription_immuable() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'Mouvement de parts sociales immuable : ni modification ni suppression'
            USING ERRCODE = 'restrict_violation';
        END $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_share_subscription_immuable
          BEFORE UPDATE OR DELETE ON tiers.share_subscriptions
          FOR EACH ROW EXECUTE FUNCTION tiers.share_subscription_immuable();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_share_subscription_immuable ON tiers.share_subscriptions"
    )
    op.execute("DROP FUNCTION IF EXISTS tiers.share_subscription_immuable()")
    op.drop_table("share_subscriptions", schema="tiers")
    op.drop_table("member_shares", schema="tiers")
    op.drop_table("share_parameters", schema="tiers")
