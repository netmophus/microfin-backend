"""Extension unaccent : recherche d'utilisateurs insensible aux accents (bloc 4b).

POURQUOI. La recherche de l'annuaire (GET /users?q=) doit trouver « Kané » en cherchant
« KANE », et « Traoré » en cherchant « traore ». Ce n'est pas un confort : en Afrique de
l'Ouest les patronymes sont saisis avec ou sans accents selon l'opérateur et le clavier, si
bien qu'une recherche stricte ne retrouve pas des gens qui EXISTENT. Un agent qui ne trouve
pas un membre au guichet contourne l'outil — la donnée cesse alors d'être fiable.

citext (déjà installée) règle la casse, pas les accents : « Kané » et « KANÉ » sont égaux
pour elle, « Kané » et « Kane » ne le sont pas. D'où unaccent.

DROITS ÉLEVÉS REQUIS — point de déploiement. CREATE EXTENSION exige d'être superuser ou de
détenir CREATE sur la base. Le rôle applicatif de production ne doit PAS être superuser
(même exigence que pour la propriété de audit.audit_logs). À l'installation d'une IMF,
l'extension est donc à créer par le DBA, ou cette migration à jouer sous un rôle distinct
de celui qui fait tourner l'application. En développement le rôle « mifin » est superuser,
ce qui masque la contrainte : elle ne se révélerait qu'en production, d'où cette note.

IF NOT EXISTS : l'extension est souvent déjà installée par le DBA au provisionnement de la
base. La migration doit alors passer sans échouer, sans quoi une IMF correctement préparée
serait la seule à ne pas pouvoir migrer.

PAS D'INDEX, et c'est délibéré. unaccent() est déclarée STABLE, pas IMMUTABLE :
PostgreSQL refuse de l'employer dans un index fonctionnel. La recherche fera donc un
balayage séquentiel. C'est le bon choix ici — un annuaire d'IMF compte des dizaines à des
centaines d'employés, et un index sur cette volumétrie coûterait plus qu'il ne rapporte.
DETTE DATÉE : si un annuaire dépassait quelques milliers de lignes (regroupement d'IMF,
réseau national), il faudrait encapsuler unaccent dans une fonction SQL IMMUTABLE maison
puis poser un index GIN pg_trgm par-dessus. Ne pas le faire par anticipation.

DOWNGRADE : DROP EXTENSION délibérément NON joué. L'extension peut avoir été installée par
le DBA avant cette migration, ou servir à d'autres schémas du produit (la recherche de
tiers du module suivant en aura besoin). Un downgrade qui la retire casserait ces usages
sans rapport avec le bloc 4b. Redescendre d'une révision ne doit pas désinstaller ce qu'on
n'a pas nécessairement installé.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")


def downgrade() -> None:
    # Volontairement vide — voir la note DOWNGRADE de l'en-tête.
    pass
