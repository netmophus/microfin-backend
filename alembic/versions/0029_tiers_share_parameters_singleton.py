"""Paramétrage comptable Bloc 5 — garantit l'UNICITÉ de la ligne de config des parts sociales.

`tiers.share_parameters` n'était protégée que par convention : `_config()` fait un
`SELECT ... LIMIT 1` et ignore silencieusement toute ligne surnuméraire. Rien n'empêchait un
script ou une insertion manuelle de poser une deuxième ligne — et laquelle des deux ferait foi ?
Le risque était resté nul jusqu'ici (seul le seed idempotent écrit dans cette table), mais le
Bloc 5 ouvre un vrai écran d'édition : le corriger maintenant coûte une contrainte, plus tard ce
serait un bug de configuration invisible à diagnostiquer (deux lignes, deux comptables convaincus
chacun que la sienne est la bonne).

Patron Postgres classique pour une table singleton : une colonne TOUJOURS vraie (CHECK) et
UNIQUE — deux lignes ne peuvent pas partager la même valeur, donc deux lignes à `singleton=TRUE`
sont impossibles par construction. N'importe quelle deuxième INSERT échoue avec une violation
d'unicité, sans toucher à la clé primaire existante ni à aucune référence déjà posée ailleurs
(audit, etc.).

Revision ID: 0029
Revises: 0028
Create Date: 2026-08-01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "share_parameters",
        sa.Column("singleton", sa.Boolean, nullable=False, server_default=sa.true()),
        schema="tiers",
    )
    op.create_check_constraint(
        "share_parameters_singleton_check",
        "share_parameters",
        "singleton",
        schema="tiers",
    )
    op.create_unique_constraint(
        "share_parameters_singleton_unique",
        "share_parameters",
        ["singleton"],
        schema="tiers",
    )


def downgrade() -> None:
    op.drop_constraint(
        "share_parameters_singleton_unique", "share_parameters", schema="tiers", type_="unique"
    )
    op.drop_constraint(
        "share_parameters_singleton_check", "share_parameters", schema="tiers", type_="check"
    )
    op.drop_column("share_parameters", "singleton", schema="tiers")
