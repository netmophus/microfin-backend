"""Chantier coffre-fort/caisses — Sous-chantier 3, Lot A : responsabilité des niveaux de caisse
(modèle B, décisions actées).

`caisse.caissiers_principaux` — désigne LE caissier titulaire de la caisse principale d'une
agence. UN caissier principal par agence à la fois (`agency_id` UNIQUE — pas un rattachement
N:N comme `caisse.poste_assignations` : contrairement à un guichet, qui peut avoir plusieurs
guichetiers assignés, la responsabilité de la principale est nominative et unique). Redésigner
quelqu'un est un remplacement (upsert), jamais un ajout.

DÉLIBÉRÉMENT SÉPARÉ de `caisse.niveaux_caisse` (qui reste purement comptable — quel compte,
`compta.plan.manage`) : cette table est organisationnelle — qui est responsable, éditée sous une
permission différente (`caisse.principale.manage`, Lot B/C). Mélanger les deux aurait brouillé
la frontière entre les deux permissions qui les éditent, exactement l'écueil déjà évité entre
`caisse.postes` (compte, `compta.plan.manage`) et `caisse.poste_assignations` (personnes,
`caisse.poste.manage`).

LE COFFRE N'A PAS DE TABLE ÉQUIVALENTE : sa responsabilité est de RÔLE (RESPONSABLE_AGENCE,
cloisonné à son agence), pas nominative — voir `caisse.coffre.gerer` (seed_security.py) et
`transferts.py::_verifier_responsable_coffre`.

VIDE À L'INSTALLATION, AUCUN INSERT ICI : même discipline que `niveaux_caisse` — une désignation
non faite est un état LÉGITIME et transitoire (refus propre à l'usage, jamais un caissier
deviné), pas une erreur de seed.

Revision ID: 0048
Revises: 0047
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")
FK_USER = "security.users.id"


def upgrade() -> None:
    op.create_table(
        "caissiers_principaux",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("agency_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("assigned_at", TS, server_default=NOW, nullable=False),
        sa.Column("assigned_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "agency_id", name="uq_caisse_caissiers_principaux_agency"
        ),
        sa.ForeignKeyConstraint(["agency_id"], ["parameters.agencies.id"]),
        sa.ForeignKeyConstraint(["user_id"], [FK_USER]),
        sa.ForeignKeyConstraint(["assigned_by"], [FK_USER]),
        schema="caisse",
    )


def downgrade() -> None:
    op.drop_table("caissiers_principaux", schema="caisse")
