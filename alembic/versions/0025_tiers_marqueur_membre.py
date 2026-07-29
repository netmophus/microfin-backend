"""Tiers — marqueur membre / client (sociétariat), fondation du chantier Parts sociales.

Ajoute `tiers.tiers.is_member` (BOOLEAN, NOT NULL, défaut FALSE = client). Marqueur ORTHOGONAL :
- au `status` (cycle de vie : prospect/actif/suspendu/désactivé…) — un membre peut être actif OU
  suspendu ;
- au `tier_type` (nature juridique : individual/legal_entity/group) — un individu comme une
  personne morale peut être membre OU client.

On naît CLIENT (défaut le moins engageant : on ne fait de personne un sociétaire sans qu'il l'ait
voulu). On devient MEMBRE par un acte VOLONTAIRE — souscrire des parts sociales (bloc à venir).
Ce marqueur est un reflet MAINTENU par le futur service de parts (souscription -> TRUE, retrait
total -> FALSE) ; il n'est PAS basculé à la main ici (aucune opération : juste le champ).

Backfill : tous les tiers existants deviennent client. Le défaut FALSE est une CONSTANTE (non
volatile) -> les lignes existantes le prennent sans UPDATE, et `tiers.tiers` ne porte aucun trigger
d'immuabilité, donc rien à désactiver. Ne casse aucun calcul ni aucune écriture existante : c'est
une information ajoutée, inerte tant que les parts n'existent pas.

Revision ID: 0025
Revises: 0024
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tiers",
        sa.Column(
            "is_member",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),  # tous les tiers existants -> client
        ),
        schema="tiers",
    )


def downgrade() -> None:
    op.drop_column("tiers", "is_member", schema="tiers")
