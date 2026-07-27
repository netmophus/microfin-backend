"""Socle comptable C0 — schéma `comptabilite` + plan de comptes (table accounts).

Le plan de comptes est une DONNÉE, pas du code : la migration crée la STRUCTURE, une commande CLI
importe les 345 comptes du CSV RCSFD (provisoires). Chaque IMF a son plan, il évolue sans
redéploiement.

Table comptabilite.accounts :
  - account_number : code hiérarchique UNIQUE et immuable (comme tier_number).
  - hiérarchie par parent_id (self-FK) ; la cohérence de PRÉFIXE (parent de 1011 = 101) est
    validée au service/import (règle inter-lignes, pas un CHECK).
  - normal_side D/C ; account_class 1..9 et COHÉRENT avec le 1er chiffre du numéro (CHECK).
  - is_posting : compte de SAISIE (feuille, mouvementable) vs REGROUPEMENT (titre, jamais mouvementé).
  - is_system : plan de référence PROTÉGÉ (sens/is_posting verrouillés, non supprimable).
  - is_provisional : « À VALIDER » par l'expert-comptable SFD (défaut TRUE à l'import ; bannière écran).
  - is_active : désactivation LOGIQUE (un compte ne se supprime pas, il se désactive).

Garde-fous « mouvementé » (pas de suppression / pas de changement de sens d'un compte utilisé) :
portés par le service, ils s'ACTIVENT avec C2 (table journal_lines) — testés là. Ici : structure,
CHECK sens/classe, hiérarchie, protection système.

DOWNGRADE : drop de la table puis du schéma (premier objet du schéma).

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS comptabilite")

    op.create_table(
        "accounts",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("account_number", sa.String(20), nullable=False),  # code RCSFD, immuable
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("short_name", sa.String(50), nullable=True),
        sa.Column("account_class", sa.SmallInteger(), nullable=False),
        sa.Column("parent_id", UUID, nullable=True),
        sa.Column("normal_side", sa.CHAR(1), nullable=False),
        sa.Column("is_posting", sa.Boolean(), nullable=False),  # saisie vs regroupement
        sa.Column("is_system", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_provisional", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_number"),
        sa.ForeignKeyConstraint(["parent_id"], ["comptabilite.accounts.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["security.users.id"]),
        sa.ForeignKeyConstraint(["updated_by"], ["security.users.id"]),
        sa.CheckConstraint("normal_side IN ('D', 'C')", name="normal_side"),
        sa.CheckConstraint("account_class BETWEEN 1 AND 9", name="classe"),
        # La classe est le 1er chiffre du numéro : cohérence garantie en base, pas seulement au service.
        sa.CheckConstraint(
            "account_class = CAST(LEFT(account_number, 1) AS SMALLINT)", name="classe_coherente"
        ),
        schema="comptabilite",
    )
    op.create_index(
        "ix_accounts_parent_id", "accounts", ["parent_id"], schema="comptabilite"
    )
    # Recherche du plan par classe (arbre, filtres) sur les comptes vivants.
    op.create_index(
        "ix_accounts_class",
        "accounts",
        ["account_class"],
        schema="comptabilite",
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    op.drop_table("accounts", schema="comptabilite")
    op.execute("DROP SCHEMA IF EXISTS comptabilite")
