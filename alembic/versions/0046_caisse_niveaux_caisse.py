"""Chantier coffre-fort/caisses — Sous-chantier 1, Bloc 1 : paramétrage des niveaux caisse.

`caisse.niveaux_caisse` — pour une agence, quel compte de saisie joue le rôle « coffre » ou
« principale ». PAS le niveau secondaire : celui-ci continue de vivre sur
`caisse.postes.compte_caisse_id` (déjà nullable, déjà indépendant par poste depuis le Bloc A,
migration 0041) — l'IMF choisit d'y rattacher un compte commun ou un compte par guichet sans
qu'aucune colonne nouvelle n'ait à trancher entre les deux.

VOLONTAIREMENT VIDE À L'INSTALLATION, AUCUN INSERT ICI : ce n'est pas une numérotation figée par
le logiciel, c'est un paramétrage que chaque IMF choisit et rattache elle-même, exactement comme
les rattachements épargne/parts existants (`parameters.agencies.compte_caisse_id`,
`epargne.products.compte_epargne_id`, etc.). `compte_caisse_id` NULLABLE : un niveau non
paramétré est un état LÉGITIME, jamais deviné, refusé proprement à l'usage (même discipline que
`caisse.postes.compte_caisse_id`, `caisse.parametres.compte_ecart_manquant_id`).

CE BLOC N'AJOUTE QU'UNE TABLE VIDE : ne touche ni 101111, ni les postes/sessions existants, ni
`parameters.agencies.compte_caisse_id` (qui reste le rattachement historique/de secours,
inchangé). Aucune migration de données.

Revision ID: 0046
Revises: 0045
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
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
        "niveaux_caisse",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("agency_id", UUID, nullable=False),
        sa.Column("niveau", sa.String(20), nullable=False),
        sa.Column("compte_caisse_id", UUID, nullable=True),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("niveau IN ('coffre', 'principale')", name="niveau_valide"),
        sa.UniqueConstraint(
            "agency_id", "niveau", name="uq_caisse_niveaux_caisse_agency_niveau"
        ),
        sa.ForeignKeyConstraint(["agency_id"], ["parameters.agencies.id"]),
        sa.ForeignKeyConstraint(["compte_caisse_id"], [FK_ACCOUNT]),
        sa.ForeignKeyConstraint(["created_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["updated_by"], [FK_USER]),
        schema="caisse",
    )
    op.create_index(
        "ix_caisse_niveaux_caisse_agency", "niveaux_caisse", ["agency_id"], schema="caisse"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_caisse_niveaux_caisse_agency", table_name="niveaux_caisse", schema="caisse"
    )
    op.drop_table("niveaux_caisse", schema="caisse")
