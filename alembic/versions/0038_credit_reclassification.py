"""Crédit CR5c — reclassification automatique (encours + provisionnement).

Trois ajouts :

1. `credit.applications.delinquency_tier_id` (nullable, FK vers un palier CR5a) : la
   classification COURANTE d'un dossier décaissé. NULL = sain (aucun palier ne correspond,
   jours_retard == 0). Jamais recalculée à la volée : posée par le job de reclassification,
   lue partout ailleurs (notamment `rembourser()`, qui doit désormais créditer le compte
   COURANT de l'encours — celui du palier si classé, sinon l'ancrage `compte_credit_id` figé
   au décaissement — sans quoi un remboursement continuerait de créditer le compte sain même
   après un passage en souffrance, laissant le solde de la classe 29 ne jamais s'apurer).

2. Deux comptes supplémentaires sur `credit.delinquency_tiers`, à côté de `compte_encours_id`/
   `compte_dotation_id` posés en CR5a :
   - `compte_provision_id` : le compte de BILAN (299x, contra-actif) qui porte la provision
     accumulée elle-même — oublié en CR5a (paramétrage seul, rien ne lisait encore ces comptes),
     nécessaire dès qu'une écriture de dotation/reprise doit avoir DEUX jambes réelles.
   - `compte_reprise_id` : le mouvement inverse de la dotation, quand la provision diminue ou
     s'annule. Le référentiel RCSFD officiel n'a qu'UN compte de reprise (764, sans
     sous-tranches) mais on le rend paramétrable par palier comme les autres — même discipline,
     aucune exception codée en dur.

3. `credit.delinquency_events` : historique immuable d'un reclassement. TROIS montants
   peuvent être ZÉRO et dans ce cas l'écriture correspondante n'est PAS posée (le moteur
   comptable refuse toute ligne à montant nul) :
   - `entry_id_encours` : NULL si l'encours à reclasser est retombé à 0 tout seul (le dernier
     remboursement a déjà crédité directement le compte courant de l'encours — cas le plus net,
     un crédit classé qui devient intégralement soldé).
   - `entry_id_reprise`/`entry_id_dotation` : la provision n'est JAMAIS nettée en un delta —
     chaque palier a son PROPRE compte 299x (pas de pool commun), donc changer de palier
     REPREND intégralement la provision de l'ancien (`entry_id_reprise`, sur le compte du
     palier QUITTÉ) et REDOTE intégralement celle du nouveau (`entry_id_dotation`, sur le
     compte du palier ATTEINT) — les deux peuvent coexister dans le même événement (ex. deux
     paliers de souffrance successifs), ou un seul selon qu'on entre/sort/reste hors classement.
   Chaque ligne existe indépendamment du montant : l'événement documente le reclassement
   (nouveau palier, jours_retard) même quand une ou plusieurs écritures ont été sautées.

Revision ID: 0038
Revises: 0037
Create Date: 2026-08-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")
FK_USER = "security.users.id"
FK_ACCOUNT = "comptabilite.accounts.id"
FK_APPLICATION = "credit.applications.id"
FK_TIER_PALIER = "credit.delinquency_tiers.id"
FK_JOURNAL_ENTRY = "comptabilite.journal_entries.id"


def upgrade() -> None:
    op.add_column(
        "delinquency_tiers",
        sa.Column("compte_provision_id", UUID, nullable=True),
        schema="credit",
    )
    op.create_foreign_key(
        "fk_delinquency_tiers_compte_provision_id_accounts",
        "delinquency_tiers",
        "accounts",
        ["compte_provision_id"],
        ["id"],
        source_schema="credit",
        referent_schema="comptabilite",
    )

    op.add_column(
        "delinquency_tiers",
        sa.Column("compte_reprise_id", UUID, nullable=True),
        schema="credit",
    )
    op.create_foreign_key(
        "fk_delinquency_tiers_compte_reprise_id_accounts",
        "delinquency_tiers",
        "accounts",
        ["compte_reprise_id"],
        ["id"],
        source_schema="credit",
        referent_schema="comptabilite",
    )

    op.add_column(
        "applications",
        sa.Column("delinquency_tier_id", UUID, nullable=True),
        schema="credit",
    )
    op.create_foreign_key(
        "fk_applications_delinquency_tier_id_delinquency_tiers",
        "applications",
        "delinquency_tiers",
        ["delinquency_tier_id"],
        ["id"],
        source_schema="credit",
        referent_schema="credit",
    )
    op.create_index(
        "ix_credit_applications_delinquency_tier",
        "applications",
        ["delinquency_tier_id"],
        schema="credit",
    )

    op.create_table(
        "delinquency_events",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("application_id", UUID, nullable=False),
        sa.Column("executed_at", TS, server_default=NOW, nullable=False),
        sa.Column("executed_by", UUID, nullable=True),
        sa.Column("jours_retard", sa.Integer(), nullable=False),
        sa.Column("tier_avant_id", UUID, nullable=True),
        sa.Column("tier_apres_id", UUID, nullable=True),
        sa.Column("encours_actuel", sa.BigInteger(), nullable=False),
        sa.Column(
            "montant_encours_reclasse", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("provision_avant", sa.BigInteger(), nullable=False),
        sa.Column("provision_apres", sa.BigInteger(), nullable=False),
        sa.Column("entry_id_encours", UUID, nullable=True),
        sa.Column("entry_id_reprise", UUID, nullable=True),
        sa.Column("entry_id_dotation", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["application_id"], [FK_APPLICATION]),
        sa.ForeignKeyConstraint(["executed_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["tier_avant_id"], [FK_TIER_PALIER]),
        sa.ForeignKeyConstraint(["tier_apres_id"], [FK_TIER_PALIER]),
        sa.ForeignKeyConstraint(["entry_id_encours"], [FK_JOURNAL_ENTRY]),
        sa.ForeignKeyConstraint(["entry_id_reprise"], [FK_JOURNAL_ENTRY]),
        sa.ForeignKeyConstraint(["entry_id_dotation"], [FK_JOURNAL_ENTRY]),
        sa.CheckConstraint("jours_retard >= 0", name="jours_retard_positif"),
        sa.CheckConstraint("encours_actuel >= 0", name="encours_actuel_positif"),
        sa.CheckConstraint(
            "montant_encours_reclasse >= 0", name="montant_encours_reclasse_positif"
        ),
        sa.CheckConstraint("provision_avant >= 0", name="provision_avant_positif"),
        sa.CheckConstraint("provision_apres >= 0", name="provision_apres_positif"),
        schema="credit",
    )
    op.create_index(
        "ix_credit_delinquency_events_application",
        "delinquency_events",
        ["application_id"],
        schema="credit",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_credit_delinquency_events_application",
        table_name="delinquency_events",
        schema="credit",
    )
    op.drop_table("delinquency_events", schema="credit")

    op.drop_index(
        "ix_credit_applications_delinquency_tier", table_name="applications", schema="credit"
    )
    op.drop_constraint(
        "fk_applications_delinquency_tier_id_delinquency_tiers",
        "applications",
        schema="credit",
        type_="foreignkey",
    )
    op.drop_column("applications", "delinquency_tier_id", schema="credit")

    op.drop_constraint(
        "fk_delinquency_tiers_compte_reprise_id_accounts",
        "delinquency_tiers",
        schema="credit",
        type_="foreignkey",
    )
    op.drop_column("delinquency_tiers", "compte_reprise_id", schema="credit")

    op.drop_constraint(
        "fk_delinquency_tiers_compte_provision_id_accounts",
        "delinquency_tiers",
        schema="credit",
        type_="foreignkey",
    )
    op.drop_column("delinquency_tiers", "compte_provision_id", schema="credit")
