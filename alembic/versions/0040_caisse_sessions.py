"""Caisse CA0 — fondations : sessions de caisse (une par CAISSIER, jamais deux ouvertes à la
fois pour le même caissier — garde-fou en BASE, pas seulement applicatif, même philosophie que
l'exclusion de chevauchement des exercices, migration 0016).

`compte_caisse_id` est ANCRÉ à l'ouverture (copié depuis `parameters.agencies.compte_caisse_id`
au moment T) — même discipline que `compte_credit_id` (CR3) / `compte_collectif_id` (PS3) : si
le rattachement caisse de l'agence change après coup, les sessions passées ne sont jamais
réinterprétées.

Le solde théorique n'est PAS une valeur tenue à jour en continu : c'est un calcul DÉRIVÉ
(fonds_initial + Σ mouvements de caisse validés du caissier sur sa fenêtre d'ouverture — voir
app/modules/caisse/service.py::calculer_solde_theorique), figé UNE SEULE FOIS à la fermeture
(`solde_theorique_cloture`) — même philosophie de cache que `epargne.accounts.balance` : jamais
une seconde source de vérité qui pourrait diverger silencieusement des écritures.

CHECK `statut_coherent_avec_cloture` : dernier rempart en base, même patron que
`credit.installments.statut_coherent_avec_montant_paye` (migration 0037) — une session ouverte
n'a AUCUN champ de clôture rempli, une session fermée les a TOUS.

CE BLOC (CA0/CA1) : ouverture, fermeture, calcul et AFFICHAGE de l'écart. AUCUNE écriture
comptable posée (CA3, une fois le compte d'écart validé par l'expert), AUCUN blocage sur écart
(CA2), AUCUNE tolérance/seuil (CA2, table caisse.parametres pas encore créée ici — ne pas
construire par anticipation ce qui n'est pas encore utilisé).

Revision ID: 0040
Revises: 0039
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")
FK_USER = "security.users.id"


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS caisse")

    op.create_table(
        "sessions",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("agency_id", UUID, nullable=False),
        sa.Column("caissier_id", UUID, nullable=False),
        sa.Column("compte_caisse_id", UUID, nullable=False),
        sa.Column("fonds_initial", sa.BigInteger(), nullable=False),
        sa.Column("opened_at", TS, server_default=NOW, nullable=False),
        sa.Column("closed_at", TS, nullable=True),
        # Les 3 colonnes suivantes ne sont remplies QU'À la fermeture — voir le CHECK plus bas.
        sa.Column("montant_reel_cloture", sa.BigInteger(), nullable=True),
        sa.Column("solde_theorique_cloture", sa.BigInteger(), nullable=True),
        sa.Column("ecart", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(10), server_default=sa.text("'ouverte'"), nullable=False),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["agency_id"], ["parameters.agencies.id"]),
        sa.ForeignKeyConstraint(["caissier_id"], [FK_USER]),
        sa.ForeignKeyConstraint(["compte_caisse_id"], ["comptabilite.accounts.id"]),
        sa.ForeignKeyConstraint(["created_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["updated_by"], [FK_USER]),
        sa.CheckConstraint("status IN ('ouverte', 'fermee')", name="status"),
        sa.CheckConstraint("fonds_initial >= 0", name="fonds_initial_positif"),
        sa.CheckConstraint(
            "(status = 'ouverte' AND closed_at IS NULL AND montant_reel_cloture IS NULL "
            "AND solde_theorique_cloture IS NULL AND ecart IS NULL) "
            "OR "
            "(status = 'fermee' AND closed_at IS NOT NULL AND montant_reel_cloture IS NOT NULL "
            "AND solde_theorique_cloture IS NOT NULL AND ecart IS NOT NULL)",
            name="statut_coherent_avec_cloture",
        ),
        schema="caisse",
    )
    op.create_index("ix_caisse_sessions_agency", "sessions", ["agency_id"], schema="caisse")
    op.create_index("ix_caisse_sessions_caissier", "sessions", ["caissier_id"], schema="caisse")
    # LE garde-fou : un caissier ne peut avoir qu'UNE session ouverte à la fois.
    op.create_index(
        "uq_caisse_sessions_caissier_ouverte",
        "sessions",
        ["caissier_id"],
        unique=True,
        schema="caisse",
        postgresql_where=sa.text("status = 'ouverte'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_caisse_sessions_caissier_ouverte", table_name="sessions", schema="caisse"
    )
    op.drop_index("ix_caisse_sessions_caissier", table_name="sessions", schema="caisse")
    op.drop_index("ix_caisse_sessions_agency", table_name="sessions", schema="caisse")
    op.drop_table("sessions", schema="caisse")
    op.execute("DROP SCHEMA IF EXISTS caisse")
