"""Crédit CR5a — paliers de souffrance (paramétrage), fondation du bloc impayés/provisionnement.

Table institution-level, PLUSIEURS lignes (pas un singleton comme share_parameters) : chaque
ligne est un « palier » de la machine à états créance saine -> impayée -> douteuse ->
irrécouvrable. `seuil_jours` sert LUI-MÊME de clé de tri (pas de colonne `ordre` séparée —
aucune chance de divergence entre un ordre et le seuil qu'il est censé refléter).

`compte_encours_id` et `compte_dotation_id` sont VOLONTAIREMENT découplés l'un de l'autre : la
classe 29 (encours, 3 tranches officielles) et la classe 664 (dotations, 4 tranches officielles)
ne se correspondent pas terme à terme (voir docs/conformite-credit.md §2, point non tranché).
Deux paliers consécutifs peuvent pointer vers le MÊME compte d'encours tout en ayant des
comptes de dotation distincts — la table n'impose aucune correspondance 1:1 entre les deux
classes, elle absorbe l'incohérence au lieu de la deviner.

Aucun comportement nouveau dans ce bloc (CR5a) : paramétrage seul, rien ne lit encore cette
table (la reclassification automatique est CR5c). Comptes posés à NULL au seed — remplis via
l'écran de paramétrage (Bloc 5), jamais codés en dur.

Revision ID: 0036
Revises: 0035
Create Date: 2026-08-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")
FK_USER = "security.users.id"
FK_ACCOUNT = "comptabilite.accounts.id"


def upgrade() -> None:
    op.create_table(
        "delinquency_tiers",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("code", sa.String(20), nullable=False),
        sa.Column("libelle", sa.String(150), nullable=False),
        sa.Column("seuil_jours", sa.Integer(), nullable=False),
        sa.Column(
            "taux_provision_bp", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("compte_encours_id", UUID, nullable=True),
        sa.Column("compte_dotation_id", UUID, nullable=True),
        sa.Column("is_terminal", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_provisional", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
        sa.UniqueConstraint("seuil_jours"),
        sa.ForeignKeyConstraint(["compte_encours_id"], [FK_ACCOUNT]),
        sa.ForeignKeyConstraint(["compte_dotation_id"], [FK_ACCOUNT]),
        sa.ForeignKeyConstraint(["created_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["updated_by"], [FK_USER]),
        sa.CheckConstraint("seuil_jours >= 0", name="seuil_jours_positif"),
        sa.CheckConstraint(
            "taux_provision_bp BETWEEN 0 AND 10000", name="taux_provision_bp_pourcentage"
        ),
        schema="credit",
    )


def downgrade() -> None:
    op.drop_table("delinquency_tiers", schema="credit")
