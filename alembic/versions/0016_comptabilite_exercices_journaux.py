"""Socle comptable C1 — exercices comptables + journaux.

Le CADRE dans lequel vivent les écritures (C2) :

  - exercices : la période comptable (un exercice courant OUVERT dès le départ ; la clôture /
    report à nouveau est REPORTÉE). Contrainte d'EXCLUSION : deux exercices ne peuvent pas se
    chevaucher, donc « l'exercice qui contient la date D » est sans ambiguïté.
  - journals : caisse, banque, opérations diverses… Ce sont des DONNÉES (comme le plan de
    comptes) : livrés PROVISOIRES, gérés par le comptable, sans redéploiement.

Aucune écriture ici : seulement le cadre. Les pièces et leurs lignes arrivent en 0017.

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")
FK_USER = "security.users.id"


def upgrade() -> None:
    op.create_table(
        "exercices",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("code", sa.String(20), nullable=False),  # ex. « 2026 »
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("date_debut", sa.Date(), nullable=False),
        sa.Column("date_fin", sa.Date(), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'ouvert'"), nullable=False),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
        sa.ForeignKeyConstraint(["created_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["updated_by"], [FK_USER]),
        sa.CheckConstraint("status IN ('ouvert', 'clos')", name="status"),
        sa.CheckConstraint("date_fin > date_debut", name="bornes"),
        schema="comptabilite",
    )
    # Deux exercices ne peuvent pas se chevaucher : « l'exercice contenant la date D » est
    # alors unique. Gist sur daterange (bornes incluses), pas d'extension requise.
    op.execute(
        "ALTER TABLE comptabilite.exercices ADD CONSTRAINT exercices_sans_chevauchement "
        "EXCLUDE USING gist (daterange(date_debut, date_fin, '[]') WITH &&)"
    )

    op.create_table(
        "journals",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("code", sa.String(10), nullable=False),  # ex. « CA », « BQ », « OD »
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("type", sa.String(30), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_provisional", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_system", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
        sa.ForeignKeyConstraint(["created_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["updated_by"], [FK_USER]),
        sa.CheckConstraint(
            "type IN ('tresorerie', 'operations_diverses', 'a_nouveaux')", name="type"
        ),
        schema="comptabilite",
    )


def downgrade() -> None:
    op.drop_table("journals", schema="comptabilite")
    op.drop_table("exercices", schema="comptabilite")
