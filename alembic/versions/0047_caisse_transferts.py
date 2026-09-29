"""Chantier coffre-fort/caisses — Sous-chantier 2, Lot 1 : l'objet Transfert entre niveaux de
caisse (coffre/principale/secondaire), et son pont comptable (compte de liaison + écarts dédiés).

`caisse.transferts` — UN mouvement de fonds entre deux niveaux ADJACENTS d'une même agence
(coffre↔principale, principale↔secondaire — jamais coffre↔secondaire direct, imposé par le CHECK
`adjacence_valide`, qui fait aussi office de contrôle de valeur sur `niveau_source`/
`niveau_destination` : toute autre valeur échoue aux quatre branches). Mécanique ENVOI/RÉCEPTION :

  - `envoye_par`/`envoye_le` : TOUJOURS renseignés (l'écriture d'envoi est posée dans la même
    transaction que la création de la ligne — `journal_entry_envoi_id` NOT NULL, un transfert
    n'existe jamais sans son écriture d'envoi déjà posée).
  - `receptionne_par`/`receptionne_le`/`montant_compte`/`journal_entry_reception_id` : NULL tant
    que `statut = 'en_transit'`, tous renseignés une fois `statut = 'receptionne'` — CHECK
    `statut_coherent_avec_reception`, même patron que `caisse.sessions.statut_coherent_avec_cloture`
    (0040). SEULEMENT DEUX statuts : l'écart (montant_compte != montant_envoye) est un FAIT
    DÉRIVÉ, jamais un troisième statut stocké — même philosophie que `session_a_valider` (jamais un
    calcul mis en cache qui pourrait diverger).

DOUBLE REGARD : CHECK `receptionne_par != envoye_par` — dernier rempart en base (le refus
applicatif, avec message clair, mord en premier dans le service).

COMPTES ANCRÉS À L'INITIATION (`compte_source_id`/`compte_destination_id`, NOT NULL) : résolus
depuis `niveaux_caisse`/`postes.compte_caisse_id` à cet instant, jamais recalculés — même
discipline que `caisse.sessions.compte_caisse_id`. `poste_source_id`/`poste_destination_id`
NULLABLE, renseigné SEULEMENT du côté « secondaire » (CHECK `poste_coherent_source`/
`poste_coherent_destination`) — c'est ce qui permet de rattacher un mouvement à la session
ouverte de son titulaire (voir `app/modules/caisse/transferts.py`, contrôle à l'objet).

PONT COMPTABLE (option B actée, compte de liaison) : trois colonnes NULLABLES ajoutées à
`caisse.parametres` (paramétrage d'institution existant, migration 0043) — même discipline que
`compte_ecart_manquant_id`/`compte_ecart_excedent_id` (0044) : un rattachement absent est un état
LÉGITIME, refusé proprement au moment de poser l'écriture, jamais deviné.
  - `compte_transit_id` : le compte de liaison, débité à l'envoi, crédité à la réception —
    montre en permanence « ce qui est en transit » dans le grand livre lui-même.
  - `compte_ecart_transfert_manquant_id`/`compte_ecart_transfert_excedent_id` : DÉDIÉS,
    DISTINCTS des comptes d'écart de caisse (`compte_ecart_manquant_id`/`compte_ecart_excedent_id`,
    0044) — l'IMF peut choisir le même compte si elle le souhaite (rien ne l'en empêche), mais le
    logiciel ne présume jamais qu'un écart de transfert est de même nature qu'un écart de session.

Revision ID: 0047
Revises: 0046
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")
FK_USER = "security.users.id"
FK_ACCOUNT = "comptabilite.accounts.id"
FK_ENTRY = "comptabilite.journal_entries.id"

ADJACENCE_VALIDE = (
    "(niveau_source = 'coffre' AND niveau_destination = 'principale') OR "
    "(niveau_source = 'principale' AND niveau_destination = 'coffre') OR "
    "(niveau_source = 'principale' AND niveau_destination = 'secondaire') OR "
    "(niveau_source = 'secondaire' AND niveau_destination = 'principale')"
)

STATUT_COHERENT = (
    "(statut = 'en_transit' AND receptionne_par IS NULL AND receptionne_le IS NULL "
    "AND montant_compte IS NULL AND journal_entry_reception_id IS NULL) "
    "OR "
    "(statut = 'receptionne' AND receptionne_par IS NOT NULL AND receptionne_le IS NOT NULL "
    "AND montant_compte IS NOT NULL AND journal_entry_reception_id IS NOT NULL)"
)


def upgrade() -> None:
    op.create_table(
        "transferts",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("agency_id", UUID, nullable=False),
        sa.Column("niveau_source", sa.String(20), nullable=False),
        sa.Column("niveau_destination", sa.String(20), nullable=False),
        sa.Column("compte_source_id", UUID, nullable=False),
        sa.Column("compte_destination_id", UUID, nullable=False),
        sa.Column("poste_source_id", UUID, nullable=True),
        sa.Column("poste_destination_id", UUID, nullable=True),
        sa.Column("montant_envoye", sa.BigInteger(), nullable=False),
        sa.Column("montant_compte", sa.BigInteger(), nullable=True),
        sa.Column(
            "statut", sa.String(15), nullable=False, server_default=sa.text("'en_transit'")
        ),
        sa.Column("envoye_par", UUID, nullable=False),
        sa.Column("envoye_le", TS, server_default=NOW, nullable=False),
        sa.Column("receptionne_par", UUID, nullable=True),
        sa.Column("receptionne_le", TS, nullable=True),
        sa.Column("motif", sa.Text(), nullable=False),
        sa.Column("journal_entry_envoi_id", UUID, nullable=False),
        sa.Column("journal_entry_reception_id", UUID, nullable=True),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "statut IN ('en_transit', 'receptionne')", name="statut_transfert_valide"
        ),
        sa.CheckConstraint(ADJACENCE_VALIDE, name="adjacence_valide"),
        sa.CheckConstraint(
            "(niveau_source = 'secondaire') = (poste_source_id IS NOT NULL)",
            name="poste_coherent_source",
        ),
        sa.CheckConstraint(
            "(niveau_destination = 'secondaire') = (poste_destination_id IS NOT NULL)",
            name="poste_coherent_destination",
        ),
        sa.CheckConstraint("montant_envoye > 0", name="montant_envoye_positif"),
        sa.CheckConstraint(
            "montant_compte IS NULL OR montant_compte >= 0", name="montant_compte_positif"
        ),
        sa.CheckConstraint(STATUT_COHERENT, name="statut_coherent_avec_reception"),
        sa.CheckConstraint(
            "receptionne_par IS NULL OR receptionne_par != envoye_par",
            name="double_regard_envoyeur_receveur",
        ),
        sa.ForeignKeyConstraint(["agency_id"], ["parameters.agencies.id"]),
        sa.ForeignKeyConstraint(["compte_source_id"], [FK_ACCOUNT]),
        sa.ForeignKeyConstraint(["compte_destination_id"], [FK_ACCOUNT]),
        sa.ForeignKeyConstraint(["poste_source_id"], ["caisse.postes.id"]),
        sa.ForeignKeyConstraint(["poste_destination_id"], ["caisse.postes.id"]),
        sa.ForeignKeyConstraint(["envoye_par"], [FK_USER]),
        sa.ForeignKeyConstraint(["receptionne_par"], [FK_USER]),
        sa.ForeignKeyConstraint(["journal_entry_envoi_id"], [FK_ENTRY]),
        sa.ForeignKeyConstraint(["journal_entry_reception_id"], [FK_ENTRY]),
        sa.ForeignKeyConstraint(["created_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["updated_by"], [FK_USER]),
        schema="caisse",
    )
    op.create_index("ix_caisse_transferts_agency", "transferts", ["agency_id"], schema="caisse")
    op.create_index("ix_caisse_transferts_statut", "transferts", ["statut"], schema="caisse")
    op.create_index(
        "ix_caisse_transferts_poste_source", "transferts", ["poste_source_id"], schema="caisse"
    )
    op.create_index(
        "ix_caisse_transferts_poste_destination",
        "transferts",
        ["poste_destination_id"],
        schema="caisse",
    )

    op.add_column(
        "parametres", sa.Column("compte_transit_id", UUID, nullable=True), schema="caisse"
    )
    op.create_foreign_key(
        "fk_caisse_param_compte_transit",
        "parametres",
        "accounts",
        ["compte_transit_id"],
        ["id"],
        source_schema="caisse",
        referent_schema="comptabilite",
    )

    op.add_column(
        "parametres",
        sa.Column("compte_ecart_transfert_manquant_id", UUID, nullable=True),
        schema="caisse",
    )
    op.create_foreign_key(
        "fk_caisse_param_ecart_transfert_manquant",
        "parametres",
        "accounts",
        ["compte_ecart_transfert_manquant_id"],
        ["id"],
        source_schema="caisse",
        referent_schema="comptabilite",
    )

    op.add_column(
        "parametres",
        sa.Column("compte_ecart_transfert_excedent_id", UUID, nullable=True),
        schema="caisse",
    )
    op.create_foreign_key(
        "fk_caisse_param_ecart_transfert_excedent",
        "parametres",
        "accounts",
        ["compte_ecart_transfert_excedent_id"],
        ["id"],
        source_schema="caisse",
        referent_schema="comptabilite",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_caisse_param_ecart_transfert_excedent",
        "parametres",
        schema="caisse",
        type_="foreignkey",
    )
    op.drop_column("parametres", "compte_ecart_transfert_excedent_id", schema="caisse")

    op.drop_constraint(
        "fk_caisse_param_ecart_transfert_manquant",
        "parametres",
        schema="caisse",
        type_="foreignkey",
    )
    op.drop_column("parametres", "compte_ecart_transfert_manquant_id", schema="caisse")

    op.drop_constraint(
        "fk_caisse_param_compte_transit",
        "parametres",
        schema="caisse",
        type_="foreignkey",
    )
    op.drop_column("parametres", "compte_transit_id", schema="caisse")

    op.drop_index(
        "ix_caisse_transferts_poste_destination", table_name="transferts", schema="caisse"
    )
    op.drop_index("ix_caisse_transferts_poste_source", table_name="transferts", schema="caisse")
    op.drop_index("ix_caisse_transferts_statut", table_name="transferts", schema="caisse")
    op.drop_index("ix_caisse_transferts_agency", table_name="transferts", schema="caisse")
    op.drop_table("transferts", schema="caisse")
