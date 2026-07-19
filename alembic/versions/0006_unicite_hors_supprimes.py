"""Unicité des identifiants limitée aux comptes VIVANTS (préalable au bloc 4c).

LE PROBLÈME. matricule, email et username portent une contrainte UNIQUE inconditionnelle.
Or la suppression d'un utilisateur est LOGIQUE (deleted_at) : la ligne reste en base, donc
elle continue d'occuper l'index. Conséquences concrètes, invisibles la première année :

  - un employé qui part puis revient ne peut pas retrouver son matricule ;
  - une adresse de service (caisse@…, credit@…) ne peut jamais être réattribuée ;
  - recréer un compte supprimé par erreur oblige à inventer un identifiant de contournement,
    qui devient définitif.

Aucun de ces cas ne se présente le jour de la mise en service. Tous se présentent en
exploitation, et la correction sera d'autant plus coûteuse que des données existeront.

LA CORRECTION. Contraintes remplacées par des index uniques PARTIELS, WHERE deleted_at IS
NULL. L'unicité reste absolue entre comptes vivants — c'est elle qui compte, puisque c'est
elle qui empêche deux personnes de se connecter sous le même identifiant. Les comptes
supprimés sortent de l'index : plusieurs lignes supprimées peuvent partager un identifiant,
et un identifiant libéré redevient attribuable.

CE QUE ÇA N'AFFAIBLIT PAS. La suppression étant réservée à la portée réseau (4c), libérer un
identifiant reste un acte rare et tracé. Et l'audit conserve la trace de l'ancien compte :
réutiliser un matricule ne réécrit pas l'histoire, il l'accompagne.

CE QUE ÇA CHANGE POUR LE CODE APPELANT. Une requête d'unicité doit désormais préciser
deleted_at IS NULL, sinon elle « trouve » des comptes supprimés et refuse à tort une
création. Le service de création (4c) applique la même _condition_visibilite que la lecture,
qui porte déjà cette clause.

CITEXT : email est en citext, l'index hérite donc de son insensibilité à la casse —
« A.Kane@imf.ne » et « a.kane@imf.ne » restent le même compte.

DOWNGRADE : rétablit les contraintes inconditionnelles. Il ÉCHOUERA si des comptes supprimés
partagent un identifiant avec un compte vivant — ce que la migration autorise justement. Ce
n'est pas un oubli : redescendre exige alors de trancher quelles lignes garder, et ce choix
appartient à l'exploitant, pas à un script.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# sa.text(...) et NON op.inline_literal(...) : inline_literal produit une CHAÎNE, si bien
# que la clause sortait en WHERE 'deleted_at IS NULL' — un littéral toujours vrai au lieu
# d'un prédicat. L'index aurait alors couvert TOUTES les lignes, supprimées comprises,
# reconduisant en silence le défaut que cette migration corrige. Attrapé en relisant le SQL
# rendu avant exécution.
CONDITION_VIVANT = sa.text("deleted_at IS NULL")

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLONNES = ("matricule", "email", "username")


def upgrade() -> None:
    for colonne in COLONNES:
        op.drop_constraint(f"uq_users_{colonne}", "users", schema="security", type_="unique")
        op.create_index(
            f"uq_users_{colonne}_vivants",
            "users",
            [colonne],
            schema="security",
            unique=True,
            postgresql_where=CONDITION_VIVANT,
        )


def downgrade() -> None:
    for colonne in COLONNES:
        op.drop_index(f"uq_users_{colonne}_vivants", table_name="users", schema="security")
        op.create_unique_constraint(f"uq_users_{colonne}", "users", [colonne], schema="security")
