"""Comptabilité — journée comptable (chantier P1bis, lot 1).

FONDATION ADDITIVE SEULE : cette migration crée la table et rien d'autre. Aucun module
existant (caisse, datation des opérations) n'est touché — ce sont les lots 2 et 3, pas
encore codés.

CENTRALISÉE (globale, PAS par agence), même registre qu'un exercice comptable (`exercices`,
migration 0002) : `date_comptable` n'est pas forcément la date système (ex. on ouvre le
vendredi la journée du lundi suivant). GARDE-FOU EN BASE, pas seulement applicatif, même
patron que `caisse.sessions` (migration 0040, une seule session ouverte par caissier) :
l'index unique partiel `uq_journees_comptables_ouverte` sur `status` filtré à 'ouverte'
rend structurellement impossible d'avoir deux journées ouvertes à la fois, y compris sous
appel concurrent — le contrôle applicatif (`journee.ouvrir_journee`) n'est qu'une
commodité de message, pas le dernier rempart.

`date_comptable` est UNIQUE (toutes les journées confondues, ouverte ou clôturée) : on ne
rouvre jamais une date déjà utilisée.

DÉFINITIVE (décision actée, même philosophie que l'exercice) : aucune réouverture, aucune
colonne ni fonction pour ça.

Revision ID: 0055
Revises: 0054
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0055"
down_revision: str | None = "0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")
FK_USER = "security.users.id"


def upgrade() -> None:
    op.create_table(
        "journees_comptables",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("date_comptable", sa.Date(), nullable=False),
        sa.Column("status", sa.String(10), server_default=sa.text("'ouverte'"), nullable=False),
        sa.Column("opened_at", TS, server_default=NOW, nullable=False),
        sa.Column("opened_by", UUID, nullable=True),
        sa.Column("closed_at", TS, nullable=True),
        sa.Column("closed_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["opened_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["closed_by"], [FK_USER]),
        sa.UniqueConstraint("date_comptable"),
        sa.CheckConstraint("status IN ('ouverte', 'cloturee')", name="status"),
        sa.CheckConstraint(
            "(status = 'ouverte' AND closed_at IS NULL) "
            "OR "
            "(status = 'cloturee' AND closed_at IS NOT NULL)",
            name="statut_coherent_avec_cloture",
        ),
        schema="comptabilite",
    )
    # LE garde-fou : au plus une journée ouverte à la fois, tout le réseau confondu.
    op.create_index(
        "uq_journees_comptables_ouverte",
        "journees_comptables",
        ["status"],
        unique=True,
        schema="comptabilite",
        postgresql_where=sa.text("status = 'ouverte'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_journees_comptables_ouverte", table_name="journees_comptables", schema="comptabilite"
    )
    op.drop_table("journees_comptables", schema="comptabilite")
