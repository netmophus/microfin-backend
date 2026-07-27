"""Socle comptable C2 — écritures à double entrée (pièces + lignes) et leurs garde-fous.

Une PIÈCE (journal_entries) = un en-tête + AU MOINS DEUX lignes (journal_lines). Chaque ligne
porte un compte, un sens (D/C) et un MONTANT ENTIER en francs CFA (BIGINT — le XOF n'a pas de
subdivision, et un flottant perdrait des francs à l'arrondi : proscrit).

FRONTIÈRE FRANCHE brouillon / validé (CHECK `frontiere` + triggers) :
  - brouillon : n'existe PAS comptablement — pas de numéro, modifiable et supprimable, PEUT être
    déséquilibré (espace de travail) ;
  - validé : réel, numéroté (atomique, sans trou, par journal+exercice), IMMUABLE. La seule
    correction est la CONTRE-PASSATION (pièce inverse), jamais la retouche.
La bascule brouillon -> validé est irréversible.

LES QUATRE GARDE-FOUS (chacun destiné à être vu MORDRE) :
  1. `trg_ligne_compte_saisie`  (BEFORE, immédiat) : on n'écrit que sur un compte de SAISIE
     (is_posting=TRUE) ; jamais sur un compte de regroupement.
  2. `trg_entry_equilibre`      (CONSTRAINT TRIGGER, DÉFÉRÉ au COMMIT) : une pièce VALIDÉE doit
     avoir >= 2 lignes et Σ débit = Σ crédit AU NIVEAU PIÈCE. Différé parce qu'on insère les
     lignes une à une : l'équilibre n'est exigé qu'à la fin de la transaction.
  3. `trg_entry_immuable`       (BEFORE UPDATE/DELETE) : une pièce validée ne se modifie ni ne
     se supprime.
  4. `trg_ligne_immuable`       (BEFORE INSERT/UPDATE/DELETE) : les lignes d'une pièce validée
     sont figées.
L'immuabilité reprend le mécanisme du journal d'audit (trigger qui LÈVE), cf. migration 0002.

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")
FK_USER = "security.users.id"


def upgrade() -> None:
    # --- Séquence de numérotation par (journal, exercice) : repart à 1 à chaque exercice ------
    op.create_table(
        "numbering_sequences",
        sa.Column("journal_id", UUID, nullable=False),
        sa.Column("exercice_id", UUID, nullable=False),
        sa.Column("last_value", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("journal_id", "exercice_id"),
        sa.ForeignKeyConstraint(["journal_id"], ["comptabilite.journals.id"]),
        sa.ForeignKeyConstraint(["exercice_id"], ["comptabilite.exercices.id"]),
        schema="comptabilite",
    )

    # --- La PIÈCE (en-tête) ------------------------------------------------------------------
    op.create_table(
        "journal_entries",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("entry_number", sa.String(30), nullable=True),  # NULL tant que brouillon
        sa.Column("journal_id", UUID, nullable=False),
        sa.Column("exercice_id", UUID, nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("description", sa.String(300), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'brouillon'"), nullable=False),
        sa.Column("posted_at", TS, nullable=True),
        sa.Column("posted_by", UUID, nullable=True),
        sa.Column("reversal_of_id", UUID, nullable=True),  # cette pièce contre-passe...
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("journal_id", "exercice_id", "entry_number", name="numero_unique"),
        sa.ForeignKeyConstraint(["journal_id"], ["comptabilite.journals.id"]),
        sa.ForeignKeyConstraint(["exercice_id"], ["comptabilite.exercices.id"]),
        sa.ForeignKeyConstraint(["reversal_of_id"], ["comptabilite.journal_entries.id"]),
        sa.ForeignKeyConstraint(["posted_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["created_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["updated_by"], [FK_USER]),
        sa.CheckConstraint("status IN ('brouillon', 'validee')", name="status"),
        # LA FRONTIÈRE, imposée par le schéma : un brouillon n'a ni numéro ni validation ; une
        # pièce validée a les deux. Impossible d'exister « à moitié ».
        sa.CheckConstraint(
            "(status = 'brouillon' AND entry_number IS NULL AND posted_at IS NULL) "
            "OR (status = 'validee' AND entry_number IS NOT NULL AND posted_at IS NOT NULL)",
            name="frontiere",
        ),
        schema="comptabilite",
    )
    op.create_index(
        "ix_entries_journal_exercice",
        "journal_entries",
        ["journal_id", "exercice_id"],
        schema="comptabilite",
    )
    op.create_index(
        "ix_entries_reversal_of", "journal_entries", ["reversal_of_id"], schema="comptabilite"
    )

    # --- Les LIGNES --------------------------------------------------------------------------
    op.create_table(
        "journal_lines",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("entry_id", UUID, nullable=False),
        sa.Column("line_number", sa.SmallInteger(), nullable=False),
        sa.Column("account_id", UUID, nullable=False),
        sa.Column("side", sa.CHAR(1), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),  # francs CFA entiers, jamais float
        sa.Column("label", sa.String(300), nullable=True),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # ON DELETE CASCADE : supprimer un BROUILLON emporte ses lignes (une pièce validée, elle,
        # n'est jamais supprimée — trigger d'immuabilité).
        sa.ForeignKeyConstraint(
            ["entry_id"], ["comptabilite.journal_entries.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["account_id"], ["comptabilite.accounts.id"]),
        sa.CheckConstraint("side IN ('D', 'C')", name="side"),
        sa.CheckConstraint("amount > 0", name="montant_positif"),
        schema="comptabilite",
    )
    op.create_index("ix_lines_entry_id", "journal_lines", ["entry_id"], schema="comptabilite")
    op.create_index("ix_lines_account_id", "journal_lines", ["account_id"], schema="comptabilite")

    # --- Garde-fou 1 : écriture uniquement sur un compte de SAISIE (immédiat) ----------------
    op.execute(
        """
        CREATE FUNCTION comptabilite.ligne_exige_compte_de_saisie() RETURNS trigger AS $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM comptabilite.accounts a
             WHERE a.id = NEW.account_id AND a.is_posting
          ) THEN
            RAISE EXCEPTION
              'Ecriture interdite sur un compte de regroupement (account_id=%)', NEW.account_id
              USING ERRCODE = 'check_violation';
          END IF;
          RETURN NEW;
        END $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_ligne_compte_saisie
          BEFORE INSERT OR UPDATE ON comptabilite.journal_lines
          FOR EACH ROW EXECUTE FUNCTION comptabilite.ligne_exige_compte_de_saisie();
        """
    )

    # --- Garde-fou 2 : équilibre + >= 2 lignes au niveau PIÈCE, DÉFÉRÉ au COMMIT --------------
    op.execute(
        """
        CREATE FUNCTION comptabilite.entry_verifier_equilibre() RETURNS trigger AS $$
        DECLARE
          nb int; total_d bigint; total_c bigint;
        BEGIN
          -- Un brouillon peut être déséquilibré : on ne contrôle QUE les pièces validées.
          IF NEW.status <> 'validee' THEN
            RETURN NULL;
          END IF;
          SELECT count(*),
                 COALESCE(SUM(amount) FILTER (WHERE side = 'D'), 0),
                 COALESCE(SUM(amount) FILTER (WHERE side = 'C'), 0)
            INTO nb, total_d, total_c
            FROM comptabilite.journal_lines WHERE entry_id = NEW.id;
          IF nb < 2 THEN
            RAISE EXCEPTION 'Piece % : au moins deux lignes exigees (trouve %)', NEW.id, nb
              USING ERRCODE = 'check_violation';
          END IF;
          IF total_d <> total_c THEN
            RAISE EXCEPTION 'Piece % desequilibree : debit % <> credit %', NEW.id, total_d, total_c
              USING ERRCODE = 'check_violation';
          END IF;
          RETURN NULL;
        END $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER trg_entry_equilibre
          AFTER INSERT OR UPDATE ON comptabilite.journal_entries
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION comptabilite.entry_verifier_equilibre();
        """
    )

    # --- Garde-fou 3 : immuabilité de la pièce validée ---------------------------------------
    op.execute(
        """
        CREATE FUNCTION comptabilite.entry_immuable() RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            IF OLD.status = 'validee' THEN
              RAISE EXCEPTION
                'Piece validee % : suppression interdite (contre-passer)', OLD.id
                USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN OLD;
          END IF;
          IF OLD.status = 'validee' THEN
            RAISE EXCEPTION
              'Piece validee % : modification interdite (contre-passer)', OLD.id
              USING ERRCODE = 'restrict_violation';
          END IF;
          RETURN NEW;
        END $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_entry_immuable
          BEFORE UPDATE OR DELETE ON comptabilite.journal_entries
          FOR EACH ROW EXECUTE FUNCTION comptabilite.entry_immuable();
        """
    )

    # --- Garde-fou 4 : immuabilité des lignes d'une pièce validée ----------------------------
    op.execute(
        """
        CREATE FUNCTION comptabilite.ligne_immuable() RETURNS trigger AS $$
        DECLARE
          st text; eid uuid;
        BEGIN
          eid := CASE WHEN TG_OP = 'DELETE' THEN OLD.entry_id ELSE NEW.entry_id END;
          SELECT status INTO st FROM comptabilite.journal_entries WHERE id = eid;
          IF st = 'validee' THEN
            RAISE EXCEPTION 'Lignes d''une piece validee figees (entry_id=%)', eid
              USING ERRCODE = 'restrict_violation';
          END IF;
          RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_ligne_immuable
          BEFORE INSERT OR UPDATE OR DELETE ON comptabilite.journal_lines
          FOR EACH ROW EXECUTE FUNCTION comptabilite.ligne_immuable();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_ligne_immuable ON comptabilite.journal_lines")
    op.execute("DROP TRIGGER IF EXISTS trg_ligne_compte_saisie ON comptabilite.journal_lines")
    op.execute("DROP TRIGGER IF EXISTS trg_entry_immuable ON comptabilite.journal_entries")
    op.execute("DROP TRIGGER IF EXISTS trg_entry_equilibre ON comptabilite.journal_entries")
    op.execute("DROP FUNCTION IF EXISTS comptabilite.ligne_immuable()")
    op.execute("DROP FUNCTION IF EXISTS comptabilite.entry_immuable()")
    op.execute("DROP FUNCTION IF EXISTS comptabilite.entry_verifier_equilibre()")
    op.execute("DROP FUNCTION IF EXISTS comptabilite.ligne_exige_compte_de_saisie()")
    op.drop_table("journal_lines", schema="comptabilite")
    op.drop_table("journal_entries", schema="comptabilite")
    op.drop_table("numbering_sequences", schema="comptabilite")
