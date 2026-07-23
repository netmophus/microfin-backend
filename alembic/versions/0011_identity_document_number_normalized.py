"""Pièces T2c — numéro normalisé + index de recherche pour l'unicité conditionnelle.

L'unicité d'un numéro de pièce (une CNI n'appartient qu'à une personne) est CONDITIONNELLE au
type : `identity_document_types.enforce_unique` la commande. La base ne peut pas l'exprimer (un
index partiel ne peut pas lire un flag d'une AUTRE table), donc l'unicité reste portée par le
SERVICE (pieces.py). Cette migration ne pose donc PAS de contrainte unique : elle ajoute
seulement de quoi faire le contrôle vite et juste.

1. document_number_normalized : le numéro débarrassé des espaces et mis en majuscules
   (« ab 12 34 » et « AB1234 » sont le même numéro). Maintenu par le service à chaque écriture,
   comme phone_number en T2b. NOT NULL après backfill (tout document a un numéro).

2. ix_identity_documents_type_numero : index btree SIMPLE (non-unique, à dessein) sur
   (document_type_id, document_number_normalized) WHERE deleted_at IS NULL — il ne sert qu'à
   rendre la recherche de doublon indexée, réseau. Btree simple = alembic check reste propre
   (pas d'index fonctionnel fragile).

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "identity_documents",
        sa.Column("document_number_normalized", sa.String(50), nullable=True),
        schema="tiers",
    )
    # Backfill : même normalisation que le service (retrait des espaces + majuscules).
    op.execute(
        r"UPDATE tiers.identity_documents "
        r"SET document_number_normalized = upper(regexp_replace(document_number, '\s', '', 'g'))"
    )
    op.alter_column(
        "identity_documents", "document_number_normalized", nullable=False, schema="tiers"
    )
    op.create_index(
        "ix_identity_documents_type_numero",
        "identity_documents",
        ["document_type_id", "document_number_normalized"],
        unique=False,
        schema="tiers",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_identity_documents_type_numero", table_name="identity_documents", schema="tiers")
    op.drop_column("identity_documents", "document_number_normalized", schema="tiers")
