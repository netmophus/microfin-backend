"""Comptabilité — marqueur d'affectation du résultat sur l'exercice (chantier P1, lot b2a).

Même patron que `solde_at`/`solde_by` (migration 0051) et `decided_at`/`decided_by` (0032/0033) :
une paire de colonnes dédiées à l'étape du cycle de vie, pas seulement le journal d'audit.
`resultat_affecte_at IS NOT NULL` sert DIRECTEMENT de marqueur « déjà affecté » — pas de colonne
booléenne séparée, qui ne ferait que dupliquer cette même information.

Rien d'autre dans cette migration : le schéma d'écriture (591 -> réserves/58) est posé par le
moteur existant (ecritures.py), aucune structure supplémentaire n'est nécessaire pour ça.

Revision ID: 0052
Revises: 0051
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.add_column(
        "exercices",
        sa.Column("resultat_affecte_at", TS, nullable=True),
        schema="comptabilite",
    )
    op.add_column(
        "exercices",
        sa.Column("resultat_affecte_by", UUID, nullable=True),
        schema="comptabilite",
    )
    op.create_foreign_key(
        "fk_comptabilite_exercices_resultat_affecte_by",
        "exercices",
        "users",
        ["resultat_affecte_by"],
        ["id"],
        source_schema="comptabilite",
        referent_schema="security",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_comptabilite_exercices_resultat_affecte_by",
        "exercices",
        schema="comptabilite",
        type_="foreignkey",
    )
    op.drop_column("exercices", "resultat_affecte_by", schema="comptabilite")
    op.drop_column("exercices", "resultat_affecte_at", schema="comptabilite")
