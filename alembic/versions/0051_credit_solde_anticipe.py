"""Crédit — solde anticipé (clôture totale avant terme) : statut 'solde' + traçabilité.

Remboursement anticipé, lot B. Arbitrages actés (cadrage validé) :
  - TOTAL seulement (pas de partiel anticipé, qui exigerait de réécrire l'échéancier).
  - Les `credit.installments` FUTURES ne sont JAMAIS touchées (option c du cadrage) : le plan
    reste un témoin historique intact. Aucune migration sur `installments` — c'est précisément
    ce qui évite le conflit avec son CHECK `statut_coherent_avec_montant_paye` (migration 0037),
    qui exigerait `montant_paye = total` pour marquer une échéance 'paye', alors qu'un solde
    anticipé paie MOINS que la somme des `total` futurs (intérêts annulés).
  - Conséquence gratuite : `rembourser()` (`status != 'decaisse'` refuse) et
    `executer_reclassification()` (`status == 'decaisse'` sélectionne) excluent déjà tout statut
    différent de 'decaisse' — passer à 'solde' les coupe TOUS LES DEUX sans toucher une ligne de
    ces deux fichiers.

  - `solde_at`/`solde_by` : même patron que `decided_at`/`decided_by` et
    `disbursed_at`/`disbursed_by` (migration 0032/0033) — une colonne dédiée par étape majeure
    du cycle de vie, pas seulement le journal d'audit.

Revision ID: 0051
Revises: 0050
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
FK_USER = "security.users.id"

STATUTS_AVANT = "status IN ('en_instruction', 'approuve', 'refuse', 'decaisse')"
STATUTS_APRES = "status IN ('en_instruction', 'approuve', 'refuse', 'decaisse', 'solde')"


def upgrade() -> None:
    op.drop_constraint("status", "applications", schema="credit", type_="check")
    op.create_check_constraint(
        "status",
        "applications",
        STATUTS_APRES,
        schema="credit",
    )

    op.add_column(
        "applications",
        sa.Column("solde_at", TS, nullable=True),
        schema="credit",
    )
    op.add_column(
        "applications",
        sa.Column("solde_by", UUID, nullable=True),
        schema="credit",
    )
    op.create_foreign_key(
        "fk_credit_applications_solde_by",
        "applications",
        "users",
        ["solde_by"],
        ["id"],
        source_schema="credit",
        referent_schema="security",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_credit_applications_solde_by", "applications", schema="credit", type_="foreignkey"
    )
    op.drop_column("applications", "solde_by", schema="credit")
    op.drop_column("applications", "solde_at", schema="credit")

    op.drop_constraint("status", "applications", schema="credit", type_="check")
    op.create_check_constraint(
        "status",
        "applications",
        STATUTS_AVANT,
        schema="credit",
    )
