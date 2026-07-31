"""Épargne PS3 — routage comptable membre/client (3111 vs 3112), ANCRÉ PAR COMPTE.

Le plan RCSFD dédouble chaque nature d'épargne : xxx1 membres / xxx2 clients. Jusqu'ici tout
allait au compte MEMBRE (provisoire). PS3 branche le marqueur `tiers.is_member` :

  - `epargne.products.compte_epargne_client_id` : le rattachement CLIENT du produit
    (EAV -> 3112, DAT -> 3122, EPR -> 3132 ; seed), nullable, PROVISOIRE — tant qu'il est NULL,
    tout continue d'aller au compte membre (rétrocompatible).
  - `epargne.accounts.compte_collectif_id` : le compte MÉMORISE son collectif, FIGÉ à
    l'ouverture selon le statut du titulaire À CE MOMENT-LÀ (option B, ⚠️ à valider par
    l'expert). Un client qui devient membre garde ses comptes sur 3112 ; seuls les NOUVEAUX
    comptes suivent le nouveau statut. Jamais re-routé, aucun transfert automatique : un même
    compte ne peut pas s'éclater entre 3111 et 3112 (le rapprochement reste sain par construction).

Backfill : les comptes existants sont ancrés là où leurs écritures sont DÉJÀ (le compte membre
de leur produit). AUCUNE écriture n'est réécrite — on ne touche que l'ancrage.

Revision ID: 0028
Revises: 0027
Create Date: 2026-07-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    # (a) Le produit porte le rattachement CLIENT (3112/3122/3132), provisoire, nullable.
    op.add_column(
        "products",
        sa.Column(
            "compte_epargne_client_id",
            UUID,
            sa.ForeignKey("comptabilite.accounts.id"),
            nullable=True,
        ),
        schema="epargne",
    )
    # (b) Le compte MÉMORISE son collectif (ancré à l'ouverture ; jamais re-routé ensuite).
    op.add_column(
        "accounts",
        sa.Column(
            "compte_collectif_id",
            UUID,
            sa.ForeignKey("comptabilite.accounts.id"),
            nullable=True,
        ),
        schema="epargne",
    )
    # (c) Backfill : l'existant est ancré là où ses écritures sont déjà (le compte membre du
    #     produit). Aucune écriture réécrite — on ne touche que l'ancrage.
    op.execute(
        "UPDATE epargne.accounts a SET compte_collectif_id = p.compte_epargne_id "
        "FROM epargne.products p WHERE p.id = a.product_id"
    )


def downgrade() -> None:
    op.drop_column("accounts", "compte_collectif_id", schema="epargne")
    op.drop_column("products", "compte_epargne_client_id", schema="epargne")
