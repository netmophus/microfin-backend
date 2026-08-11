"""Caisse — Bloc B : assignation guichetier <-> poste de caisse.

Mirroring EXACT de `security.user_agencies` (habilitation réseau, C6) — même forme, même
discipline (association pure, clé composite, `granted_at`/`granted_by`, `ondelete="CASCADE"`
sur `user_id`) — précédent déjà en production, pas un patron inventé pour l'occasion.

Table `caisse` (pas `security`) : « qui peut se connecter où » (habilitation) et « qui travaille
à quel poste aujourd'hui » (affectation opérationnelle) sont deux notions distinctes — décision
explicite, pas une confusion de schéma.

Aucune colonne nouvelle sur `caisse.postes` : la table existe depuis le Bloc A (migration 0041).

Revision ID: 0042
Revises: 0041
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
FK_USER = "security.users.id"


def upgrade() -> None:
    op.create_table(
        "poste_assignations",
        sa.Column("poste_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("granted_at", TS, server_default=NOW, nullable=False),
        sa.Column("granted_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("poste_id", "user_id"),
        sa.ForeignKeyConstraint(["poste_id"], ["caisse.postes.id"]),
        sa.ForeignKeyConstraint(["user_id"], [FK_USER], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["granted_by"], [FK_USER]),
        schema="caisse",
    )
    op.create_index(
        "ix_caisse_poste_assignations_user", "poste_assignations", ["user_id"], schema="caisse"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_caisse_poste_assignations_user", table_name="poste_assignations", schema="caisse"
    )
    op.drop_table("poste_assignations", schema="caisse")
