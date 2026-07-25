"""Cycle de vie R2 — autoriser l'event_type 'restored' sur lifecycle_events.

La restauration d'une fiche désactivée (desactive → prospect) écrit un lifecycle_event
'restored'. La colonne event_type porte une liste blanche (CHECK ck_lifecycle_events_event_type,
posée en 0008) qui ne connaît pas encore cette valeur. On la recrée en l'ajoutant.

DOWNGRADE : rétablit la liste sans 'restored'. Échouera s'il existe déjà des événements
'restored' en base (données incompatibles avec l'ancien schéma) — comportement voulu d'une liste
blanche : on ne redescend pas sous des données que l'ancienne contrainte interdirait.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONTRAINTE = "ck_lifecycle_events_event_type"

# Liste 0008 + 'restored'.
_EVENEMENTS = (
    "created",
    "updated",
    "activated",
    "suspended",
    "reactivated",
    "deactivated",
    "marked_deceased",
    "marked_dissolved",
    "merged",
    "restored",
)
_EVENEMENTS_AVANT = _EVENEMENTS[:-1]  # sans 'restored'


def _recreer(valeurs: tuple[str, ...]) -> None:
    liste = ", ".join(f"'{v}'" for v in valeurs)
    op.execute(f"ALTER TABLE tiers.lifecycle_events DROP CONSTRAINT {_CONTRAINTE}")
    op.execute(
        f"ALTER TABLE tiers.lifecycle_events ADD CONSTRAINT {_CONTRAINTE} "
        f"CHECK (event_type IN ({liste}))"
    )


def upgrade() -> None:
    _recreer(_EVENEMENTS)


def downgrade() -> None:
    _recreer(_EVENEMENTS_AVANT)
