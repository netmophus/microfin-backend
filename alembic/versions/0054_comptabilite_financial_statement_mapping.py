"""Comptabilité — table de mapping compte → poste d'état financier (chantier P1, lot c).

UNE LIGNE PAR COMPTE (pas par préfixe/sous-classe) : le CSV source (`plan_comptable_enrichi.csv`)
liste les 393 comptes un par un, c'est la granularité la plus simple et la plus fiable — aucune
règle de préfixe à maintenir, aucun risque qu'un nouveau compte à 6 chiffres tombe entre deux
règles. `account_id` (FK, PK) ancre chaque ligne à un compte précis, jamais à un numéro en texte.

`gere_manuellement` : même discipline que `security.roles.gere_manuellement` (lot 3, sécurité) —
une fois une ligne ajustée à la main (écran d'admin, pas codé dans ce lot ou lot suivant), le
seed ne la réécrit plus jamais. Sans ce verrou, rejouer le seed après un paramétrage RCSFD mis à
jour effacerait silencieusement les ajustements d'un comptable.

Rien d'autre dans cette migration : le remplissage est un SEED (CLI), pas cette migration.

Revision ID: 0054
Revises: 0053
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "financial_statement_mapping",
        sa.Column("account_id", UUID, primary_key=True),
        sa.Column("etat", sa.String(10), nullable=False),
        sa.Column("masse", sa.String(20), nullable=False),
        sa.Column("poste_libelle", sa.String(200), nullable=False),
        sa.Column("poste_ordre", sa.SmallInteger, nullable=False),
        sa.Column(
            "gere_manuellement", sa.Boolean, nullable=False, server_default=sa.false()
        ),
        sa.Column("created_at", TS, nullable=False, server_default=sa.text("NOW()")),
        sa.Column("created_by", UUID),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_by", UUID),
        sa.ForeignKeyConstraint(
            ["account_id"], ["comptabilite.accounts.id"], name="fk_financial_statement_mapping_account"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["security.users.id"], name="fk_financial_statement_mapping_created_by"
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["security.users.id"], name="fk_financial_statement_mapping_updated_by"
        ),
        sa.CheckConstraint("etat IN ('BILAN', 'RESULTAT')", name="etat"),
        sa.CheckConstraint(
            "masse IN ('ACTIF', 'PASSIF', 'CONTRA_ACTIF', 'CHARGE', 'PRODUIT', 'MIXTE')",
            name="masse",
        ),
        schema="comptabilite",
    )


def downgrade() -> None:
    op.drop_table("financial_statement_mapping", schema="comptabilite")
