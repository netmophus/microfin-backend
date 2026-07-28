"""Épargne — date de valeur sur les mouvements (fondation quinzaines, non exploitée).

Ajoute `epargne.movements.date_valeur` (DATE), distincte de la date d'OPÉRATION (`created_at`).
Par défaut = date d'opération, tant qu'aucune règle (règle des quinzaines…) ne l'en écarte :
AUCUN calcul d'intérêt ne la lit encore (le moteur date toujours sur `created_at`). FONDATION
DORMANTE, en attente de la validation de la méthode par l'expert (docs/conformite-comptable.md).

Backfill : les mouvements existants reçoivent `date_valeur = created_at::date`. Comme les mouvements
sont APPEND-ONLY (trigger `trg_mouvement_immuable` qui refuse tout UPDATE), on désactive ce trigger
LE TEMPS du backfill, dans la transaction de migration (rôle privilégié) ; le rôle applicatif, lui,
reste incapable de modifier un mouvement en fonctionnement.

Figée à la création (immuable ensuite, comme le reste du mouvement) : quand la règle des quinzaines
sera confirmée, le service calculera `date_valeur` À L'INSERTION selon la règle en vigueur ; les
mouvements passés gardent leur date de valeur d'origine (on ne réécrit pas le passé).

Revision ID: 0024
Revises: 0023
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Colonne d'abord NULLABLE et sans défaut : on maîtrise le remplissage ligne à ligne
    #    (un DEFAULT CURRENT_DATE remplirait l'existant avec la date de la migration, pas la
    #    date d'opération de chaque mouvement — ce n'est pas ce qu'on veut).
    op.add_column(
        "movements",
        sa.Column("date_valeur", sa.Date(), nullable=True),
        schema="epargne",
    )

    # 2. Backfill : date de valeur = date d'opération. Le trigger d'immuabilité bloque tout UPDATE
    #    sur movements -> on le désactive LE TEMPS du backfill, puis on le remet.
    op.execute("ALTER TABLE epargne.movements DISABLE TRIGGER trg_mouvement_immuable")
    op.execute("UPDATE epargne.movements SET date_valeur = created_at::date")
    op.execute("ALTER TABLE epargne.movements ENABLE TRIGGER trg_mouvement_immuable")

    # 3. Verrouiller : NOT NULL + défaut CURRENT_DATE pour les futurs inserts (= date d'opération,
    #    comme created_at, tant qu'aucune règle ne l'en écarte).
    op.alter_column(
        "movements",
        "date_valeur",
        existing_type=sa.Date(),
        nullable=False,
        server_default=sa.text("CURRENT_DATE"),
        schema="epargne",
    )


def downgrade() -> None:
    # drop_column est du DDL (pas un UPDATE de ligne) : le trigger d'immuabilité ne s'y oppose pas.
    op.drop_column("movements", "date_valeur", schema="epargne")
