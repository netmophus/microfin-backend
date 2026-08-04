"""Crédit CR3 — décaissement (écriture comptable D crédit / C caisse) + échéancier persisté.

Troisième bloc du module Crédit — la première vraie écriture comptable. Contenu :

  - credit.applications : nouvel état 'decaisse' (après 'approuve'), + disbursed_at/
    disbursed_by + compte_credit_id (l'ANCRAGE membre/client, résolu une fois au
    décaissement, jamais re-routé ensuite — miroir exact de epargne.accounts.compte_
    collectif_id / PS3).
  - credit.installments : l'échéancier PERSISTÉ, résultat figé de generer_echeancier()
    (CR2, pur) au moment du décaissement. `status` sans CHECK pour l'instant : le
    vocabulaire des états (payé/en retard/...) sera celui de CR4 (remboursements), pas
    deviné ici — colonne posée en avance pour éviter une migration disruptive plus tard,
    mais sa contrainte attend les règles réelles.

GATE KYC AU DÉCAISSEMENT, TROISIÈME ET DERNIER TEMPS (dernier rempart, comme la création et
l'approbation en 0032) : re-vérifie le tiers À CET INSTANT, sur la transition approuve ->
decaisse. Contrairement à l'approbation, PAS d'asymétrie ici — décaisser est l'unique sens
possible depuis 'approuve', et c'est le moment où l'argent sort réellement de la caisse : le
plus critique des trois gates du parcours crédit.

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")
FK_USER = "security.users.id"
FK_ACCOUNT = "comptabilite.accounts.id"


def upgrade() -> None:
    op.add_column("applications", sa.Column("disbursed_at", TS, nullable=True), schema="credit")
    op.add_column("applications", sa.Column("disbursed_by", UUID, nullable=True), schema="credit")
    op.add_column(
        "applications", sa.Column("compte_credit_id", UUID, nullable=True), schema="credit"
    )
    op.create_foreign_key(
        "fk_credit_applications_disbursed_by",
        "applications",
        "users",
        ["disbursed_by"],
        ["id"],
        source_schema="credit",
        referent_schema="security",
    )
    op.create_foreign_key(
        "fk_credit_applications_compte_credit",
        "applications",
        "accounts",
        ["compte_credit_id"],
        ["id"],
        source_schema="credit",
        referent_schema="comptabilite",
    )

    op.drop_constraint("status", "applications", schema="credit", type_="check")
    op.create_check_constraint(
        "status",
        "applications",
        "status IN ('en_instruction', 'approuve', 'refuse', 'decaisse')",
        schema="credit",
    )

    op.create_table(
        "installments",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("application_id", UUID, nullable=False),
        sa.Column("numero", sa.Integer(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("capital", sa.BigInteger(), nullable=False),
        sa.Column("interets", sa.BigInteger(), nullable=False),
        sa.Column("total", sa.BigInteger(), nullable=False),
        sa.Column("capital_restant_du", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'a_echoir'"), nullable=False),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("application_id", "numero"),
        sa.ForeignKeyConstraint(
            ["application_id"], ["credit.applications.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint("numero > 0", name="numero_positif"),
        sa.CheckConstraint("capital >= 0", name="capital_non_negatif"),
        sa.CheckConstraint("interets >= 0", name="interets_non_negatif"),
        sa.CheckConstraint("capital_restant_du >= 0", name="capital_restant_du_non_negatif"),
        schema="credit",
    )
    op.create_index(
        "ix_credit_installments_application", "installments", ["application_id"], schema="credit"
    )

    # --- Gate KYC au DÉCAISSEMENT (dernier rempart, un seul sens : approuve -> decaisse) ------
    op.execute(
        """
        CREATE FUNCTION credit.decaissement_exige_tiers_actif() RETURNS trigger AS $$
        DECLARE st text;
        BEGIN
          IF NEW.status = 'decaisse' AND OLD.status = 'approuve' THEN
            SELECT status INTO st FROM tiers.tiers WHERE id = NEW.tier_id;
            IF st IS DISTINCT FROM 'actif' THEN
              RAISE EXCEPTION
                'Decaissement refuse : le tiers (id=%) n''est plus actif (statut=%)',
                NEW.tier_id, st
                USING ERRCODE = 'check_violation';
            END IF;
          END IF;
          RETURN NEW;
        END $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_decaissement_tiers_actif
          BEFORE UPDATE ON credit.applications
          FOR EACH ROW EXECUTE FUNCTION credit.decaissement_exige_tiers_actif();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_decaissement_tiers_actif ON credit.applications")
    op.execute("DROP FUNCTION IF EXISTS credit.decaissement_exige_tiers_actif()")
    op.drop_index("ix_credit_installments_application", table_name="installments", schema="credit")
    op.drop_table("installments", schema="credit")
    op.drop_constraint("status", "applications", schema="credit", type_="check")
    op.create_check_constraint(
        "status",
        "applications",
        "status IN ('en_instruction', 'approuve', 'refuse')",
        schema="credit",
    )
    op.drop_constraint(
        "fk_credit_applications_compte_credit", "applications", schema="credit", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_credit_applications_disbursed_by", "applications", schema="credit", type_="foreignkey"
    )
    op.drop_column("applications", "compte_credit_id", schema="credit")
    op.drop_column("applications", "disbursed_by", schema="credit")
    op.drop_column("applications", "disbursed_at", schema="credit")
