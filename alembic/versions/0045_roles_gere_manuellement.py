"""Lot 3 de l'écran « Rôles et habilitations » — le verrou `gere_manuellement`.

L'écran devient la source de vérité pour un rôle édité via l'API (lot 2 : rôles
personnalisés ; lot 4 : rôles système). `security.roles.gere_manuellement` marque ce
basculement : dès que `TRUE`, `seed-security` n'écrase plus jamais ce rôle — ni ses
métadonnées (`_UPSERT_ROLE`), ni ses habilitations (`_ACCORDER`, `_REVOQUER_HORS_MATRICE`).
Voir app/cli/seed_security.py.

DÉFAUT FALSE : une base neuve, ou tout rôle jamais touché par un endpoint d'écriture,
converge exactement comme avant cette migration. Colonne posée pour tous les rôles
existants (les 11 rôles système actuels) à FALSE — aucun changement de comportement à
l'installation de cette migration sur une base déjà peuplée.

PAS DE DÉVERROUILLAGE dans ce lot : le flag ne repasse jamais à FALSE automatiquement, et
aucun endpoint ne le permet encore. Dette notée pour un lot ultérieur (lot 4 ou au-delà).

Revision ID: 0045
Revises: 0044
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "roles",
        sa.Column(
            "gere_manuellement", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        schema="security",
    )


def downgrade() -> None:
    op.drop_column("roles", "gere_manuellement", schema="security")
