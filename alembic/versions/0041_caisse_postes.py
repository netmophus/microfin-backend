"""Caisse — Bloc A (structurel, isolé) : postes de caisse, plusieurs par agence.

CHANGEMENT DE MODÈLE : `parameters.agencies.compte_caisse_id` (un seul compte par agence)
devient `caisse.postes` (N postes par agence, chacun avec son propre compte comptable
rattaché — nullable, même discipline que `Agency.compte_caisse_id` : un poste sans compte
rattaché est un état légitime, pas une erreur).

CE BLOC EST STRUCTUREL ET SANS IMPACT COMPORTEMENTAL : aucun guichet (épargne, décaissement/
remboursement crédit, souscription parts comptant) n'est modifié ici — ils continuent de lire
`Agency.compte_caisse_id` exactement comme avant. La bascule des guichets vers les postes est
un chantier séparé (blocs D à G, décidés un par un, pas enchaînés automatiquement).

TRANSITION — rétrocompatibilité de l'historique :
  - chaque agence ayant AUJOURD'HUI un `compte_caisse_id` non nul reçoit UN poste, backfillé
    avec ce même compte (le compte historique devient le premier poste — code '01', libellé
    provisoire « Caisse principale », à ajuster librement) ;
  - une agence sans rattachement aujourd'hui ne reçoit AUCUN poste (elle n'a jamais eu de compte
    permettant d'ouvrir une session — rien à migrer) ;
  - `caisse.sessions.poste_id` est backfillé par correspondance d'agence (à cet instant, au plus
    un poste par agence, donc aucune ambiguïté) ; une ASSERTION explicite vérifie que chaque
    session backfillée a bien `compte_caisse_id` identique à celui du poste qui la reçoit —
    la migration ÉCHOUE plutôt que de mentir sur l'historique si ce n'est pas le cas.

GARDE-FOU DE SESSION — un poste, comme un caissier, ne peut avoir qu'UNE session ouverte à la
fois (`uq_caisse_sessions_poste_ouverte`, même patron que `uq_caisse_sessions_caissier_ouverte`
posé en 0040). Les DEUX gardes-fous coexistent, volontairement distincts — pas un index
composite (caissier_id, poste_id), qui laisserait passer un caissier ouvrant deux postes à la
fois, ou deux caissiers ouvrant le même poste à la fois.

VÉRIFIÉ EN BASE avant d'écrire cette migration : un seul point de rattachement réel existe
aujourd'hui (`Agency.compte_caisse_id` n'est pas contraint par une unicité — deux agences
pourraient théoriquement partager le même compte, rien ne l'interdit). Le backfill ci-dessous
ne suppose donc PAS l'unicité d'un compte entre agences.

Pas de table d'assignation guichetier <-> poste ici (Bloc B, hors périmètre de cette migration).

Revision ID: 0041
Revises: 0040
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")
FK_USER = "security.users.id"
FK_ACCOUNT = "comptabilite.accounts.id"

CODE_POSTE_HISTORIQUE = "01"
LIBELLE_POSTE_HISTORIQUE = "Caisse principale"


def upgrade() -> None:
    # --- 1. caisse.postes -----------------------------------------------------------------
    op.create_table(
        "postes",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("agency_id", UUID, nullable=False),
        sa.Column("code", sa.String(20), nullable=False),
        sa.Column("libelle", sa.String(150), nullable=False),
        sa.Column("compte_caisse_id", UUID, nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agency_id", "code", name="uq_caisse_postes_agency_code"),
        sa.ForeignKeyConstraint(["agency_id"], ["parameters.agencies.id"]),
        sa.ForeignKeyConstraint(["compte_caisse_id"], [FK_ACCOUNT]),
        sa.ForeignKeyConstraint(["created_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["updated_by"], [FK_USER]),
        schema="caisse",
    )
    op.create_index("ix_caisse_postes_agency", "postes", ["agency_id"], schema="caisse")

    # --- 2. Backfill : le compte historique de chaque agence devient son premier poste -----
    # Une agence sans compte_caisse_id aujourd'hui n'a jamais permis d'ouvrir de session :
    # rien à migrer pour elle, elle ne reçoit aucun poste.
    op.execute(
        "INSERT INTO caisse.postes (agency_id, code, libelle, compte_caisse_id) "
        f"SELECT id, '{CODE_POSTE_HISTORIQUE}', '{LIBELLE_POSTE_HISTORIQUE}', compte_caisse_id "
        "FROM parameters.agencies "
        "WHERE compte_caisse_id IS NOT NULL"
    )

    # --- 3. caisse.sessions.poste_id -------------------------------------------------------
    op.add_column("sessions", sa.Column("poste_id", UUID, nullable=True), schema="caisse")

    # Backfill par correspondance d'agence : à cet instant, au plus UN poste par agence (celui
    # créé à l'étape 2), donc aucune ambiguïté possible sur quelle ligne rattacher.
    op.execute(
        "UPDATE caisse.sessions s "
        "SET poste_id = p.id "
        "FROM caisse.postes p "
        "WHERE p.agency_id = s.agency_id"
    )

    # ASSERTION : aucune session ne doit avoir été backfillée vers un poste dont le compte
    # diffère du sien — sinon la migration MENT sur l'historique. On échoue plutôt.
    conn = op.get_bind()
    incoherentes = conn.execute(
        sa.text(
            "SELECT count(*) FROM caisse.sessions s "
            "JOIN caisse.postes p ON p.id = s.poste_id "
            "WHERE s.compte_caisse_id != p.compte_caisse_id"
        )
    ).scalar_one()
    if incoherentes > 0:
        raise RuntimeError(
            f"{incoherentes} session(s) de caisse backfillées vers un poste dont le compte "
            "diffère du leur — migration interrompue avant de fausser l'historique."
        )

    orphelines = conn.execute(
        sa.text("SELECT count(*) FROM caisse.sessions WHERE poste_id IS NULL")
    ).scalar_one()
    if orphelines > 0:
        raise RuntimeError(
            f"{orphelines} session(s) de caisse sans poste correspondant après backfill "
            "(agence sans compte_caisse_id aujourd'hui, donc sans poste créé) — migration "
            "interrompue : une session existante suppose forcément un compte rattaché à "
            "l'époque, incohérence à investiguer avant de continuer."
        )

    op.alter_column("sessions", "poste_id", nullable=False, schema="caisse")
    op.create_foreign_key(
        "fk_caisse_sessions_poste",
        "sessions",
        "postes",
        ["poste_id"],
        ["id"],
        source_schema="caisse",
        referent_schema="caisse",
    )
    op.create_index("ix_caisse_sessions_poste", "sessions", ["poste_id"], schema="caisse")

    # Second garde-fou, DISTINCT de uq_caisse_sessions_caissier_ouverte (posé en 0040, inchangé
    # ici) : un poste n'a qu'UNE session ouverte à la fois, quel que soit le caissier.
    op.create_index(
        "uq_caisse_sessions_poste_ouverte",
        "sessions",
        ["poste_id"],
        unique=True,
        schema="caisse",
        postgresql_where=sa.text("status = 'ouverte'"),
    )


def downgrade() -> None:
    op.drop_index("uq_caisse_sessions_poste_ouverte", table_name="sessions", schema="caisse")
    op.drop_index("ix_caisse_sessions_poste", table_name="sessions", schema="caisse")
    op.drop_constraint(
        "fk_caisse_sessions_poste", "sessions", schema="caisse", type_="foreignkey"
    )
    op.drop_column("sessions", "poste_id", schema="caisse")
    op.drop_index("ix_caisse_postes_agency", table_name="postes", schema="caisse")
    op.drop_table("postes", schema="caisse")
