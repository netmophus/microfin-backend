"""Crédit CR1 — demande et décision (dossier, sans écriture comptable).

Deuxième bloc du module Crédit — le premier testable de bout en bout. Contenu :

  - credit.numbering_sequences : numéro de dossier atomique, sans trou (patron épargne/tiers).
  - credit.applications : LA demande. États : en_instruction -> approuve | refuse — décision
    UNIQUE et définitive (le service refuse toute redécision, comme une pièce comptable
    validée : corriger, c'est créer un nouveau dossier, pas réécrire l'ancien).

GATE KYC AU NIVEAU BASE, EN DEUX TEMPS (dernier rempart, comme epargne.accounts E2) :
  - À LA CRÉATION (trigger sur INSERT) : refuse si le tiers n'est pas 'actif'.
  - À L'APPROBATION (trigger sur UPDATE, uniquement pour la transition en_instruction->approuve) :
    revérifie le statut du tiers À CET INSTANT — une demande peut rester en_instruction plusieurs
    jours, rien ne garantit que le tiers soit resté actif entre-temps. REFUSER, en revanche,
    n'est JAMAIS bloqué : refuser un dossier périmé pour un tiers désormais suspendu doit
    toujours être possible (aucun risque, et bloquer nuirait à la tenue du portefeuille).
    Le service double ce garde-fou par une erreur claire AVANT d'écrire.

Revision ID: 0032
Revises: 0031
Create Date: 2026-08-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")
FK_USER = "security.users.id"


def upgrade() -> None:
    op.create_table(
        "numbering_sequences",
        sa.Column("prefix", sa.String(10), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("last_value", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("prefix", "year"),
        schema="credit",
    )

    op.create_table(
        "applications",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("application_number", sa.String(30), nullable=False),
        sa.Column("tier_id", UUID, nullable=False),
        sa.Column("agency_id", UUID, nullable=False),  # cloisonnement
        sa.Column("product_id", UUID, nullable=False),
        sa.Column("montant_demande", sa.BigInteger(), nullable=False),
        sa.Column("duree_echeances", sa.Integer(), nullable=False),
        sa.Column("objet", sa.Text(), nullable=True),
        sa.Column(
            "status", sa.String(20), server_default=sa.text("'en_instruction'"), nullable=False
        ),
        sa.Column("montant_decide", sa.BigInteger(), nullable=True),
        sa.Column("decided_at", TS, nullable=True),
        sa.Column("decided_by", UUID, nullable=True),
        sa.Column("motif_decision", sa.Text(), nullable=True),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("application_number"),
        sa.ForeignKeyConstraint(["tier_id"], ["tiers.tiers.id"]),
        sa.ForeignKeyConstraint(["agency_id"], ["parameters.agencies.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["credit.products.id"]),
        sa.ForeignKeyConstraint(["decided_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["created_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["updated_by"], [FK_USER]),
        sa.CheckConstraint(
            "status IN ('en_instruction', 'approuve', 'refuse')", name="status"
        ),
        sa.CheckConstraint("montant_demande > 0", name="montant_demande_positif"),
        sa.CheckConstraint("duree_echeances > 0", name="duree_positive"),
        sa.CheckConstraint(
            "montant_decide IS NULL OR montant_decide > 0", name="montant_decide_positif"
        ),
        schema="credit",
    )
    op.create_index("ix_credit_applications_tier", "applications", ["tier_id"], schema="credit")
    op.create_index(
        "ix_credit_applications_agency", "applications", ["agency_id"], schema="credit"
    )

    # --- Gate KYC à la CRÉATION (dernier rempart) --------------------------------------------
    op.execute(
        """
        CREATE FUNCTION credit.demande_exige_tiers_actif() RETURNS trigger AS $$
        DECLARE st text;
        BEGIN
          SELECT status INTO st FROM tiers.tiers WHERE id = NEW.tier_id;
          IF st IS DISTINCT FROM 'actif' THEN
            RAISE EXCEPTION
              'Demande de credit refusee : le tiers (id=%) n''est pas actif (statut=%)',
              NEW.tier_id, st
              USING ERRCODE = 'check_violation';
          END IF;
          RETURN NEW;
        END $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_demande_tiers_actif
          BEFORE INSERT ON credit.applications
          FOR EACH ROW EXECUTE FUNCTION credit.demande_exige_tiers_actif();
        """
    )

    # --- Gate KYC à L'APPROBATION (re-vérification, PAS au refus) -----------------------------
    op.execute(
        """
        CREATE FUNCTION credit.decision_exige_tiers_actif() RETURNS trigger AS $$
        DECLARE st text;
        BEGIN
          IF NEW.status = 'approuve' AND OLD.status = 'en_instruction' THEN
            SELECT status INTO st FROM tiers.tiers WHERE id = NEW.tier_id;
            IF st IS DISTINCT FROM 'actif' THEN
              RAISE EXCEPTION
                'Decision refusee : le tiers (id=%) n''est plus actif (statut=%), demande non approuvable',
                NEW.tier_id, st
                USING ERRCODE = 'check_violation';
            END IF;
          END IF;
          RETURN NEW;
        END $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_decision_tiers_actif
          BEFORE UPDATE ON credit.applications
          FOR EACH ROW EXECUTE FUNCTION credit.decision_exige_tiers_actif();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_decision_tiers_actif ON credit.applications")
    op.execute("DROP FUNCTION IF EXISTS credit.decision_exige_tiers_actif()")
    op.execute("DROP TRIGGER IF EXISTS trg_demande_tiers_actif ON credit.applications")
    op.execute("DROP FUNCTION IF EXISTS credit.demande_exige_tiers_actif()")
    op.drop_table("applications", schema="credit")
    op.drop_table("numbering_sequences", schema="credit")
