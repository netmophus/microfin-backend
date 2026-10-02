"""Comptabilité — calendrier des jours fériés (chantier P1bis, lot 4a).

FONDATION ADDITIVE SEULE : une date fériée varie chaque année (fêtes musulmanes au calendrier
lunaire) — saisie PAR DATE précise, jamais une règle récurrente (« le 1er mai » ne suffirait
qu'aux fériés fixes). `date_feriee` UNIQUE : pas de doublon pour une même date.

Aucun calcul existant n'est modifié par CETTE migration (le report d'échéance est le lot 4b,
séparé) — seule la table est créée ici ; `comptabilite.calendrier` (couche service) et la mise
à jour de `journee.prochaine_date_ouvree` pour qu'elle tienne compte des fériés sont du code
Python, pas du schéma.

Revision ID: 0056
Revises: 0055
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0056"
down_revision: str | None = "0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")
FK_USER = "security.users.id"


def upgrade() -> None:
    op.create_table(
        "jours_feries",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("date_feriee", sa.Date(), nullable=False),
        sa.Column("libelle", sa.String(100), nullable=False),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["created_by"], [FK_USER]),
        sa.UniqueConstraint("date_feriee"),
        schema="comptabilite",
    )


def downgrade() -> None:
    op.drop_table("jours_feries", schema="comptabilite")
