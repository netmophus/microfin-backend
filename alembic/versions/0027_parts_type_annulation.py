"""Parts sociales (PS2) — le type 'annulation' des mouvements (sortie du sociétariat).

Élargit le CHECK de `tiers.share_subscriptions.type` pour accepter 'annulation' : annuler une
souscription NON libérée (promesse jamais payée) — D 1021 / C 1022, sans caisse. Le remboursement
des parts LIBÉRÉES (D 1021 / C 5721) réutilise le type 'remboursement' déjà admis.

Pas de changement de modèle ORM : les CHECK ne sont pas comparés par alembic (la base les impose),
la colonne `type` reste VARCHAR(30).

Revision ID: 0027
Revises: 0026
Create Date: 2026-07-29
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# SQL brut explicite : évite que la convention de nommage `ck` se ré-applique au nom passé à
# op.drop_constraint (double préfixe). Le nom réel de la contrainte est ck_share_subscriptions_type.
_NOM = "ck_share_subscriptions_type"
_TYPES_PS2 = "'souscription','liberation','souscription_comptant','remboursement','annulation'"
_TYPES_PS1 = "'souscription','liberation','souscription_comptant','remboursement'"


def upgrade() -> None:
    op.execute(f"ALTER TABLE tiers.share_subscriptions DROP CONSTRAINT {_NOM}")
    op.execute(
        f"ALTER TABLE tiers.share_subscriptions ADD CONSTRAINT {_NOM} "
        f"CHECK (type IN ({_TYPES_PS2}))"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE tiers.share_subscriptions DROP CONSTRAINT {_NOM}")
    op.execute(
        f"ALTER TABLE tiers.share_subscriptions ADD CONSTRAINT {_NOM} "
        f"CHECK (type IN ({_TYPES_PS1}))"
    )
