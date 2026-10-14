"""Caisse — la journée comptable d'une session devient une valeur STOCKÉE (autoritative).

Jusqu'ici la journée d'une session fermée se déduisait des horodatages civils (la journée dont
l'intervalle d'ouverture/clôture contient `closed_at`) : une reconstruction fragile pour un document
légal (lettre de demande d'explication). `ouvrir_session` la fixe désormais à l'ouverture — c'est
la journée ouverte, déjà exigée à ce moment — et elle ne change plus : un trigger refuse toute
modification (dernier rempart, comme les conditions d'un dépôt à terme).

BACKFILL, UNE SEULE FOIS, ici : les sessions existantes reçoivent la journée que la dérivation
donnait (journée ouverte à `closed_at`, ou la journée ouverte pour une session encore ouverte).
Une session qu'aucune journée ne couvre (antérieure à la journée comptable, P1bis) retombe sur la
date de son ouverture : seule date disponible pour elle, jamais utilisée ensuite pour une session
neuve. La colonne passe NOT NULL après ce remplissage.

Revision ID: 0066
Revises: 0065
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0066"
down_revision: str | None = "0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("journee_comptable", sa.Date()), schema="caisse")

    op.execute(
        """
        UPDATE caisse.sessions s
           SET journee_comptable = COALESCE(
                 (SELECT j.date_comptable
                    FROM comptabilite.journees_comptables j
                   WHERE j.opened_at <= COALESCE(s.closed_at, NOW())
                     AND (j.closed_at IS NULL OR j.closed_at >= COALESCE(s.closed_at, NOW()))
                   ORDER BY j.opened_at DESC
                   LIMIT 1),
                 s.opened_at::date)
        """
    )

    op.alter_column("sessions", "journee_comptable", nullable=False, schema="caisse")

    op.execute(
        """
        CREATE FUNCTION caisse.session_journee_immuable() RETURNS trigger AS $$
        BEGIN
          IF NEW.journee_comptable IS DISTINCT FROM OLD.journee_comptable THEN
            RAISE EXCEPTION
              'Session de caisse %: la journee comptable est fixee a l''ouverture, immuable',
              OLD.id USING ERRCODE = 'restrict_violation';
          END IF;
          RETURN NEW;
        END $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_session_journee_immuable
          BEFORE UPDATE ON caisse.sessions
          FOR EACH ROW EXECUTE FUNCTION caisse.session_journee_immuable();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_session_journee_immuable ON caisse.sessions")
    op.execute("DROP FUNCTION IF EXISTS caisse.session_journee_immuable()")
    op.drop_column("sessions", "journee_comptable", schema="caisse")
