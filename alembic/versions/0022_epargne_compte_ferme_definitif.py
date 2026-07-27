"""Épargne E4 — un compte fermé est DÉFINITIF, non réouvrable (trigger base).

La fermeture d'un compte est irréversible, comme l'immuabilité d'une pièce validée. Le service
empêche déjà toute opération sur un compte fermé (garde-fou de E3, CompteClotureError). Ici, le
DERNIER REMPART : un trigger interdit tout changement de statut d'un compte 'cloture' (donc la
réouverture cloture -> actif), même par du SQL brut. Si le membre revient, c'est un NOUVEAU compte.

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION epargne.compte_ferme_definitif() RETURNS trigger AS $$
        BEGIN
          IF OLD.status = 'cloture' AND NEW.status IS DISTINCT FROM OLD.status THEN
            RAISE EXCEPTION
              'Compte ferme % : definitif, non reouvrable', OLD.account_number
              USING ERRCODE = 'restrict_violation';
          END IF;
          RETURN NEW;
        END $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_compte_ferme_definitif
          BEFORE UPDATE ON epargne.accounts
          FOR EACH ROW EXECUTE FUNCTION epargne.compte_ferme_definitif();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_compte_ferme_definitif ON epargne.accounts")
    op.execute("DROP FUNCTION IF EXISTS epargne.compte_ferme_definitif()")
