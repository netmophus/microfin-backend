"""Épargne — tout mouvement a sa pièce comptable, et sa date de valeur vient de la pièce.

Deux changements sur `epargne.movements`, liés : le calcul des intérêts date les mouvements par la
journée comptable de leur pièce (`journal_entries.entry_date`), pas par leur date de saisie.

1. `journal_entry_id` passe NOT NULL. Aucun chemin du code n'a jamais créé de mouvement sans pièce
   (guichet, clôture, intérêts, crédit externe : tous passent une pièce) ; la contrainte en fait une
   garantie de la base. Si une ligne NULL existait, la migration s'arrête AVANT de modifier quoi que
   ce soit, en disant combien : on ne devine pas une pièce.

2. `date_valeur` perd son `server_default CURRENT_DATE` (migration 0024). Ce défaut recopiait la
   date CIVILE de saisie. Le service pose désormais `date_valeur = entry_date` de la pièce à
   l'insertion ; une insertion qui l'oublie échoue (NOT NULL) au lieu de prendre la date du jour.
   Aucun backfill : les mouvements existants gardent leur date de valeur (le trigger d'immuabilité
   interdit de toute façon de les réécrire sans le désactiver).

Revision ID: 0059
Revises: 0058
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0059"
down_revision: str | None = "0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    sans_piece = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM epargne.movements WHERE journal_entry_id IS NULL"))
        .scalar_one()
    )
    if sans_piece:
        raise RuntimeError(
            f"{sans_piece} mouvement(s) d'épargne sans pièce comptable : à examiner avant de "
            "rendre journal_entry_id obligatoire (aucune pièce n'est devinée par la migration)."
        )
    op.alter_column(
        "movements",
        "journal_entry_id",
        existing_type=sa.UUID(),
        nullable=False,
        schema="epargne",
    )
    op.alter_column(
        "movements",
        "date_valeur",
        existing_type=sa.Date(),
        existing_nullable=False,
        server_default=None,
        schema="epargne",
    )


def downgrade() -> None:
    op.alter_column(
        "movements",
        "date_valeur",
        existing_type=sa.Date(),
        existing_nullable=False,
        server_default=sa.text("CURRENT_DATE"),
        schema="epargne",
    )
    op.alter_column(
        "movements",
        "journal_entry_id",
        existing_type=sa.UUID(),
        nullable=True,
        schema="epargne",
    )
