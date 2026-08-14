"""Caisse CA2 — seuil de tolérance sur l'écart, motif obligatoire au-delà, validation a
posteriori du responsable.

Deux mouvements distincts, dans une même migration parce qu'ils forment UN bloc fonctionnel
(le seuil sans le motif ne sert à rien ; le motif sans une trace de validation ne se referme
jamais) :

  (a) `caisse.parametres` — config d'INSTITUTION (une ligne), PROVISOIRE, même patron que
      `tiers.share_parameters` (migration 0026/0029) : `seuil_tolerance` (F, comparé à
      `abs(ecart)`), `is_provisional`, singleton CHECK+UNIQUE posé DÈS LA CRÉATION (pas retrofit
      comme share_parameters, qui l'a reçu après coup en 0029 — ici rien n'existe encore avant).
      Les rattachements comptables de l'écart (compte manquant/excédent) ne sont PAS dans cette
      migration : CA3 les ajoute séparément (voir docstring du module caisse, décision actée).

  (b) Trois colonnes sur `caisse.sessions` : `motif_ecart` (ce que le caissier a saisi à la
      fermeture — optionnel sous le seuil, obligatoire au-delà, contrôle fait en service, PAS
      en CHECK base : le seuil est une donnée modifiable, une contrainte SQL figée mentirait dès
      le premier changement de seuil) ; `valide_le`/`valide_par` (trace de la validation a
      posteriori du responsable — AUCUNE colonne de statut séparée : « à valider » se DÉRIVE de
      fermé + écart significatif + valide_le IS NULL, jamais stocké, même philosophie que le
      solde théorique lui-même qui n'est jamais mis en cache).

NE BLOQUE TOUJOURS PAS la fermeture (décision actée dès l'analyse initiale) : ces colonnes
tracent et exigent un motif, elles n'empêchent jamais un caissier de fermer sa caisse.

Revision ID: 0043
Revises: 0042
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")
FK_USER = "security.users.id"


def upgrade() -> None:
    # --- (a) Seuil de tolérance — singleton, CHECK+UNIQUE posé dès la création ------------------
    op.create_table(
        "parametres",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column(
            "seuil_tolerance", sa.BigInteger(), nullable=False, server_default=sa.text("500")
        ),
        sa.Column("is_provisional", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("singleton", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["created_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["updated_by"], [FK_USER]),
        sa.CheckConstraint("seuil_tolerance >= 0", name="seuil_tolerance_positif"),
        sa.CheckConstraint("singleton", name="caisse_parametres_singleton_check"),
        sa.UniqueConstraint("singleton", name="caisse_parametres_singleton_unique"),
        schema="caisse",
    )

    # --- (b) Motif de l'écart + trace de validation a posteriori, sur la session existante ------
    op.add_column(
        "sessions", sa.Column("motif_ecart", sa.Text(), nullable=True), schema="caisse"
    )
    op.add_column(
        "sessions", sa.Column("valide_le", TS, nullable=True), schema="caisse"
    )
    op.add_column(
        "sessions", sa.Column("valide_par", UUID, nullable=True), schema="caisse"
    )
    op.create_foreign_key(
        "fk_caisse_sessions_valide_par",
        "sessions",
        "users",
        ["valide_par"],
        ["id"],
        source_schema="caisse",
        referent_schema="security",
    )
    # Cohérence : pas de validation sans fermeture préalable (une session ouverte n'a pas encore
    # d'écart à valider). Même esprit que le CHECK `statut_coherent_avec_cloture` déjà en place.
    op.create_check_constraint(
        "validation_apres_fermeture",
        "sessions",
        "valide_le IS NULL OR status = 'fermee'",
        schema="caisse",
    )


def downgrade() -> None:
    op.drop_constraint("validation_apres_fermeture", "sessions", schema="caisse", type_="check")
    op.drop_constraint(
        "fk_caisse_sessions_valide_par", "sessions", schema="caisse", type_="foreignkey"
    )
    op.drop_column("sessions", "valide_par", schema="caisse")
    op.drop_column("sessions", "valide_le", schema="caisse")
    op.drop_column("sessions", "motif_ecart", schema="caisse")
    op.drop_table("parametres", schema="caisse")
