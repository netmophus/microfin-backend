"""Épargne E1 — le pont comptable : modèles d'écriture (données) + caisse par agence.

Chaque opération métier (dépôt, retrait…) a un MODÈLE D'ÉCRITURE stocké en base : une suite de
lignes (rôle, sens D/C), dans un journal. Le rôle est ABSTRAIT (CAISSE, EPARGNE…) et se résout
en compte concret au moment de l'opération (E3) : EPARGNE -> product.compte_epargne_id (le 3111
collectif), CAISSE -> agency.compte_caisse_id (le 5721 de l'agence). Le « quoi » est de la DONNÉE
provisoire (validable par l'expert) ; le montant et la résolution rôle->compte sont du code.

Mécanisme GÉNÉRIQUE, posé dans le schéma `comptabilite` (réutilisable par le Crédit plus tard) :
  - entry_schemas : code (« epargne.depot »), libellé, journal, provisoire ;
  - entry_schema_lines : (rôle, sens) ordonnées.

CAISSE — la face « argent physique » : on ajoute seulement `parameters.agencies.compte_caisse_id`
(FK -> comptabilite.accounts, provisoire). C'est le COMPTE comptable de caisse de l'agence. Le
module Caisse (guichets, sessions, dénombrement, rapprochement physique) est REPORTÉ ; il se posera
au-dessus de ce compte sans le déplacer.

Aucun garde-fou comptable ici : ce sont des DONNÉES. Les garde-fous (équilibre, compte de saisie)
mordent en E3 quand le moteur pose la vraie pièce via le service C2.

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")
FK_USER = "security.users.id"


def upgrade() -> None:
    # --- Modèles d'écriture (en-tête) -------------------------------------------------------
    op.create_table(
        "entry_schemas",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("code", sa.String(50), nullable=False),  # ex. « epargne.depot »
        sa.Column("label", sa.String(150), nullable=False),
        sa.Column("journal_id", UUID, nullable=True),  # journal où poser la pièce (provisoire)
        sa.Column("is_provisional", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
        sa.ForeignKeyConstraint(["journal_id"], ["comptabilite.journals.id"]),
        sa.ForeignKeyConstraint(["created_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["updated_by"], [FK_USER]),
        schema="comptabilite",
    )

    # --- Lignes du modèle : (rôle, sens), ordonnées -----------------------------------------
    op.create_table(
        "entry_schema_lines",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("schema_id", UUID, nullable=False),
        sa.Column("line_order", sa.SmallInteger(), nullable=False),
        # Rôle ABSTRAIT, résolu en compte au moment de l'opération (CAISSE, EPARGNE, …).
        # Laissé libre (pas de CHECK) : de nouveaux rôles (INTERETS…) arriveront sans migration.
        sa.Column("role", sa.String(30), nullable=False),
        sa.Column("side", sa.CHAR(1), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("schema_id", "line_order", name="ordre_unique"),
        sa.ForeignKeyConstraint(
            ["schema_id"], ["comptabilite.entry_schemas.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint("side IN ('D', 'C')", name="side"),
        schema="comptabilite",
    )
    op.create_index(
        "ix_schema_lines_schema", "entry_schema_lines", ["schema_id"], schema="comptabilite"
    )

    # --- La caisse : compte comptable de caisse par agence (provisoire) ---------------------
    op.add_column(
        "agencies",
        sa.Column("compte_caisse_id", UUID, nullable=True),
        schema="parameters",
    )
    op.create_foreign_key(
        "fk_agencies_compte_caisse",
        "agencies",
        "accounts",
        ["compte_caisse_id"],
        ["id"],
        source_schema="parameters",
        referent_schema="comptabilite",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_agencies_compte_caisse", "agencies", schema="parameters", type_="foreignkey"
    )
    op.drop_column("agencies", "compte_caisse_id", schema="parameters")
    op.drop_table("entry_schema_lines", schema="comptabilite")
    op.drop_table("entry_schemas", schema="comptabilite")
