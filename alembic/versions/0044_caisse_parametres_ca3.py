"""Caisse CA3 — rattachement comptable de l'écart (compte manquant / compte excédent).

ÉTEND `caisse.parametres` (créée par CA2, migration 0043) plutôt que d'ajouter une nouvelle
table — même dépendance temporelle que `credit.delinquency_tiers` (créée en 0036) qui a reçu
ses rattachements comptables `compte_provision_id`/`compte_reprise_id` dans une migration
PLUS TARDIVE, 0038 : CA2 (le seuil, le motif, la validation) est un bloc gouvernance complet
sans aucune écriture ; CA3 lui ajoute le rattachement comptable, séparément.

DEUX COMPTES DISTINCTS, jamais un signe négatif sur un seul (décision actée) :
  - compte_ecart_manquant_id : la CHARGE quand le réel compté est INFÉRIEUR au théorique
    (D ECART / C CAISSE) — candidat proposé 6099 (Diverses charges d'exploitation
    financière), PAS 6239 (candidat de l'analyse initiale, écarté : explicitement libellé
    « non financière », un mauvais candidat sémantique pour un écart de caisse).
  - compte_ecart_excedent_id : le PRODUIT quand le réel compté est SUPÉRIEUR au théorique
    (D CAISSE / C ECART) — candidat proposé 7099 (Divers produits d'exploitation), sous la
    même famille « 60/70 — exploitation financière » que 6099, le miroir exact.

Les DEUX comptes existent déjà dans le plan RCSFD officiel (`is_posting=TRUE`), déjà
`is_provisional=TRUE` (hérité de l'import du plan, pas quelque chose que cette migration
pose) — AUCUNE création de compte ici, seulement le RATTACHEMENT, comme
`delinquency_tiers.compte_provision_id` rattache un compte déjà existant.

Nullable : un rattachement absent est un état LÉGITIME (paramétrage incomplet), refusé
proprement par le service au moment de poser l'écriture — jamais deviné, jamais un défaut
codé en dur.

Revision ID: 0044
Revises: 0043
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
FK_ACCOUNT = "comptabilite.accounts.id"


def upgrade() -> None:
    op.add_column(
        "parametres",
        sa.Column("compte_ecart_manquant_id", UUID, nullable=True),
        schema="caisse",
    )
    op.create_foreign_key(
        "fk_caisse_parametres_compte_ecart_manquant_id_accounts",
        "parametres",
        "accounts",
        ["compte_ecart_manquant_id"],
        ["id"],
        source_schema="caisse",
        referent_schema="comptabilite",
    )

    op.add_column(
        "parametres",
        sa.Column("compte_ecart_excedent_id", UUID, nullable=True),
        schema="caisse",
    )
    op.create_foreign_key(
        "fk_caisse_parametres_compte_ecart_excedent_id_accounts",
        "parametres",
        "accounts",
        ["compte_ecart_excedent_id"],
        ["id"],
        source_schema="caisse",
        referent_schema="comptabilite",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_caisse_parametres_compte_ecart_excedent_id_accounts",
        "parametres",
        schema="caisse",
        type_="foreignkey",
    )
    op.drop_column("parametres", "compte_ecart_excedent_id", schema="caisse")

    op.drop_constraint(
        "fk_caisse_parametres_compte_ecart_manquant_id_accounts",
        "parametres",
        schema="caisse",
        type_="foreignkey",
    )
    op.drop_column("parametres", "compte_ecart_manquant_id", schema="caisse")
