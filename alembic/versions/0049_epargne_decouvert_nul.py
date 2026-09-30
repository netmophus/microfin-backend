"""Épargne — verrou en base : aucun découvert autorisé sur un produit (conformité SFD).

Un SFD ne tient pas de comptes courants ; l'épargne est un simple dépôt, elle ne doit JAMAIS
autoriser de découvert. `epargne.products.decouvert_autorise` existe depuis 0021 (défaut 0,
CHECK `decouvert_positif : decouvert_autorise >= 0`) — positif, pas forcément NUL. Le verrou
côté API (schemas.py, `CreationProduitEpargne`/`ModificationProduitEpargne`, extra="forbid")
empêche déjà toute écriture applicative d'un découvert non nul, mais rien n'empêchait une
insertion directe en base (ORM, script, autre client SQL) de poser une valeur non nulle — la
contrainte ci-dessous ferme cette porte au niveau le plus bas, le seul qui ne puisse pas être
contourné par un futur appelant qui oublierait le garde-fou applicatif.

GARDE DE MIGRATION : avant d'ajouter la contrainte, on VÉRIFIE qu'aucune ligne existante ne la
violerait. En dev, tout est déjà à 0 (vérifié le 30/09/2026) ; en production, une IMF qui aurait
un jour posé un découvert par un autre chemin doit voir la migration ÉCHOUER avec un message
clair plutôt que voir ses données altérées en silence — même discipline que l'import du plan
comptable (tout ou rien, jamais un compromis silencieux).

Le CHECK `decouvert_positif` (0021) reste en place : `= 0` implique `>= 0`, les deux coexistent
sans conflit ; on ne remplace pas l'ancien, on resserre.

Réversible : downgrade() retire la contrainte, ne touche à aucune donnée.

Revision ID: 0049
Revises: 0048
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONTRAINTE = "decouvert_nul_epargne"


def upgrade() -> None:
    connexion = op.get_bind()
    violations = connexion.execute(
        sa.text("SELECT count(*) FROM epargne.products WHERE decouvert_autorise <> 0")
    ).scalar_one()
    if violations:
        raise RuntimeError(
            f"Migration 0049 refusée : {violations} produit(s) d'épargne ont déjà un "
            "decouvert_autorise non nul. Un SFD n'autorise aucun découvert — corriger ces "
            "lignes avant de rejouer cette migration, ne jamais les altérer silencieusement ici."
        )

    op.create_check_constraint(
        CONTRAINTE,
        "products",
        "decouvert_autorise = 0",
        schema="epargne",
    )


def downgrade() -> None:
    op.drop_constraint(CONTRAINTE, "products", schema="epargne", type_="check")
