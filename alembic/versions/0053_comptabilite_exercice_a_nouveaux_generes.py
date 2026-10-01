"""Comptabilité — marqueur de génération des à-nouveaux sur l'exercice (chantier P1, lot b2b).

Migration SÉPARÉE de la 0052 (lot b2a), par choix délibéré : même précédent que
`decided_at`/`decided_by` (0032) et `disbursed_at`/`disbursed_by` (0033), deux migrations
distinctes pour deux marqueurs de cycle de vie différents, même si proches dans le temps et sur
la même table. b2a et b2b restent deux actes indépendants (décision actée) ; les regrouper dans
une seule migration aurait couplé leur downgrade pour rien.

Posé sur l'exercice RECEVEUR (celui qui reçoit ses à-nouveaux), jamais sur l'exercice source :
la question posée par la garde anti-double-génération est « cet exercice a-t-il déjà reçu ses
soldes d'ouverture ? », une propriété du receveur — même raisonnement que `resultat_affecte_at`
sur l'exercice qui AFFECTE (là, le même exercice est source et sujet de l'action ; ici, les deux
rôles sont portés par deux exercices différents, donc le marqueur suit le sujet de l'action : le
receveur).

Revision ID: 0053
Revises: 0052
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.add_column(
        "exercices",
        sa.Column("a_nouveaux_generes_at", TS, nullable=True),
        schema="comptabilite",
    )
    op.add_column(
        "exercices",
        sa.Column("a_nouveaux_generes_by", UUID, nullable=True),
        schema="comptabilite",
    )
    op.create_foreign_key(
        "fk_comptabilite_exercices_a_nouveaux_generes_by",
        "exercices",
        "users",
        ["a_nouveaux_generes_by"],
        ["id"],
        source_schema="comptabilite",
        referent_schema="security",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_comptabilite_exercices_a_nouveaux_generes_by",
        "exercices",
        schema="comptabilite",
        type_="foreignkey",
    )
    op.drop_column("exercices", "a_nouveaux_generes_by", schema="comptabilite")
    op.drop_column("exercices", "a_nouveaux_generes_at", schema="comptabilite")
