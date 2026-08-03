"""Historique des rôles de compte pour les parts sociales (rapprochement du capital).

Sans mémoire des rattachements passés, rapprocher_capital_libere() (parts_rapprochement.py)
ne compterait plus que le compte COURANT — dès qu'un rattachement change (ex. bascule vers un
compte d'extension à 6 chiffres), tout l'historique posté sur l'ancien compte deviendrait un
faux écart. Même principe que compte_collectif_id côté épargne (jamais réécrit, jamais purgé),
mais ancré au niveau du RÔLE plutôt que du compte auxiliaire, faute d'équivalent ici :
`share_parameters` est une ligne UNIQUE, écrasée en place à chaque changement — l'ancienne
valeur ne survivait que dans l'audit (fait pour un humain, pas pour être requêté par le code).

Revision ID: 0030
Revises: 0029
Create Date: 2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "share_account_roles",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column(
            "account_id", UUID, sa.ForeignKey("comptabilite.accounts.id"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("created_by", UUID, sa.ForeignKey("security.users.id")),
        sa.CheckConstraint(
            "role IN ('liberees', 'non_liberees')", name="share_account_roles_role_check"
        ),
        sa.UniqueConstraint("role", "account_id", name="uq_share_account_roles"),
        schema="tiers",
    )

    # Backfill : capture le rattachement ACTUEL (57111/57112) avant qu'il ne change jamais —
    # sinon le tout premier rôle de l'historique serait perdu dès la prochaine mise à jour.
    op.execute(
        """
        INSERT INTO tiers.share_account_roles (role, account_id)
        SELECT 'liberees', compte_parts_liberees_id FROM tiers.share_parameters
        WHERE compte_parts_liberees_id IS NOT NULL
        UNION ALL
        SELECT 'non_liberees', compte_parts_non_liberees_id FROM tiers.share_parameters
        WHERE compte_parts_non_liberees_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_table("share_account_roles", schema="tiers")
