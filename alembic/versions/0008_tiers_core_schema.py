"""Schéma « tiers » — tables cœur du module Tiers (bloc T1a).

Fondations du premier module métier : le schéma tiers et ses six tables cœur, sans aucune
logique métier. Ni modèle ORM, ni service, ni route, ni seed — cette migration ne pose que
la structure. Les modèles et la numérotation viennent en T1b, la création et les permissions
en T1c.

PATTERN D'HÉRITAGE (D1) — Class Table Inheritance : une table parent tiers.tiers avec le
discriminateur tier_type, et trois tables enfants (individual/legal_entity/group_profiles)
dont la PK est aussi la FK vers le parent. L'exclusivité stricte entre enfants n'est PAS
garantie au niveau base (voir dette tracée) : le mapper joined n'écrit qu'un enfant par
tiers selon tier_type, aucun chemin de code n'insère à la main.

CHECK RÉELS COUPLANT TYPE ET STATUT (D4) — pas le CHECK tautologique du document source :
  - ck_tiers_deces_pp        : 'decede'  réservé aux personnes physiques ;
  - ck_tiers_dissolution_pm  : 'dissous' réservé aux morales et groupements.
Ces deux contraintes MORDENT (vérifiées à l'exécution).

tier_number UNIQUE INCONDITIONNEL — contrairement à matricule/email (index partiels WHERE
deleted_at IS NULL, migration 0006), le numéro de membre est immuable et jamais réattribué,
même après désactivation : il figure sur le livret d'épargne, les reçus, les archives
comptables de 10 ans. Le réattribuer créerait deux personnes avec le même identifiant dans
l'historique. La différence avec le matricule employé est VOLONTAIRE.

event_metadata (D3) — la colonne JSONB de lifecycle_events s'appelle event_metadata, pas
metadata : ce dernier est réservé par SQLAlchemy Declarative et casserait au chargement.

SOFT DELETE UNIQUEMENT — aucune suppression physique (conservation LBC/FT 10 ans) : deleted_at
sur le parent, jamais de DELETE. Le « qui » d'une désactivation est porté par lifecycle_events
et audit_logs, pas par une colonne deleted_by (D5).

Champs différés, marqués TODO côté modèle : activity_sector_id / education_level_id (personne
physique) attendent des référentiels Paramétrage non créés ; name_phonetic_key appartient à la
recherche/déduplication (T4).

DOWNGRADE : DROP des six tables en ordre inverse des dépendances, puis DROP du schéma tiers.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")

# Statuts et types autorisés — repris tels quels dans les CHECK ci-dessous.
_STATUTS = (
    "prospect",
    "actif",
    "suspendu_temporaire",
    "suspendu_lcb",
    "desactive",
    "decede",
    "dissous",
    "fusionne",
)
_TYPES = ("individual", "legal_entity", "group")
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
)

# Ordre de suppression au downgrade : enfants et tables satellites d'abord, parent en dernier.
_TABLES_A_SUPPRIMER = (
    "lifecycle_events",
    "numbering_sequences",
    "group_profiles",
    "legal_entity_profiles",
    "individual_profiles",
    "tiers",
)


def _en_liste_sql(valeurs: tuple[str, ...]) -> str:
    """Rend « 'a','b','c' » pour une clause IN d'un CHECK textuel."""
    return ",".join(f"'{v}'" for v in valeurs)


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS tiers")

    # --- tiers.tiers — table parent (Class Table Inheritance) ----------------
    op.create_table(
        "tiers",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("tier_number", sa.String(20), nullable=False),
        sa.Column("tier_type", sa.String(20), nullable=False),
        sa.Column("primary_agency_id", UUID, nullable=False),
        sa.Column("status", sa.String(30), server_default=sa.text("'prospect'"), nullable=False),
        sa.Column("primary_phone", sa.String(30), nullable=True),
        sa.Column("language_preference", sa.String(10), nullable=True),
        sa.Column("activated_at", TS, nullable=True),
        sa.Column("activated_by", UUID, nullable=True),
        sa.Column("suspended_at", TS, nullable=True),
        sa.Column("suspended_by", UUID, nullable=True),
        sa.Column("suspension_reason", sa.Text(), nullable=True),
        sa.Column("merged_into_tier_id", UUID, nullable=True),  # placeholder fusion (T4)
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_by", UUID, nullable=True),
        sa.Column("deleted_at", TS, nullable=True),  # soft delete uniquement
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tier_number"),  # inconditionnel (jamais réattribué, cf. docstring)
        sa.CheckConstraint(f"tier_type IN ({_en_liste_sql(_TYPES)})", name="tier_type"),
        sa.CheckConstraint(f"status IN ({_en_liste_sql(_STATUTS)})", name="status"),
        # D4 — couplage type/statut, contraintes qui mordent :
        sa.CheckConstraint("status <> 'decede' OR tier_type = 'individual'", name="deces_pp"),
        sa.CheckConstraint(
            "status <> 'dissous' OR tier_type IN ('legal_entity','group')", name="dissolution_pm"
        ),
        sa.ForeignKeyConstraint(["primary_agency_id"], ["parameters.agencies.id"]),
        sa.ForeignKeyConstraint(["merged_into_tier_id"], ["tiers.tiers.id"]),
        sa.ForeignKeyConstraint(["activated_by"], ["security.users.id"]),
        sa.ForeignKeyConstraint(["suspended_by"], ["security.users.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["security.users.id"]),
        sa.ForeignKeyConstraint(["updated_by"], ["security.users.id"]),
        schema="tiers",
    )
    # Index de la spec §9.5 — partiels : cloisonnement et listes ignorent les fiches supprimées.
    op.create_index(
        "ix_tiers_type_status",
        "tiers",
        ["tier_type", "status"],
        schema="tiers",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_tiers_agency_status",
        "tiers",
        ["primary_agency_id", "status"],
        schema="tiers",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_tiers_activated_at",
        "tiers",
        ["activated_at"],
        schema="tiers",
        postgresql_where=sa.text("activated_at IS NOT NULL"),
    )

    # --- tiers.individual_profiles — personne physique -----------------------
    op.create_table(
        "individual_profiles",
        sa.Column("tier_id", UUID, nullable=False),
        sa.Column("last_name", sa.String(100), nullable=False),
        sa.Column("first_name", sa.String(100), nullable=False),
        sa.Column("middle_names", sa.String(200), nullable=True),
        sa.Column("name_at_birth", sa.String(200), nullable=True),
        sa.Column("birth_date", sa.Date(), nullable=False),
        sa.Column("birth_place", sa.String(200), nullable=True),
        sa.Column("birth_country_id", UUID, nullable=True),
        sa.Column("gender", sa.CHAR(1), nullable=False),
        sa.Column("nationality_id", UUID, nullable=False),
        sa.Column("secondary_nationality_id", UUID, nullable=True),
        sa.Column("marital_status", sa.String(30), nullable=True),
        sa.Column("dependents_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("profession", sa.String(200), nullable=True),
        sa.Column("monthly_income_estimate", sa.Numeric(18, 2), nullable=True),
        sa.Column("is_literate", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.PrimaryKeyConstraint("tier_id"),
        sa.ForeignKeyConstraint(["tier_id"], ["tiers.tiers.id"]),
        sa.ForeignKeyConstraint(["birth_country_id"], ["parameters.countries.id"]),
        sa.ForeignKeyConstraint(["nationality_id"], ["parameters.countries.id"]),
        sa.ForeignKeyConstraint(["secondary_nationality_id"], ["parameters.countries.id"]),
        sa.CheckConstraint("gender IN ('M','F')", name="gender"),
        sa.CheckConstraint(
            "marital_status IS NULL OR marital_status IN "
            "('celibataire','marie','divorce','veuf','union_libre','autre')",
            name="marital",
        ),
        sa.CheckConstraint("birth_date < CURRENT_DATE", name="birth_past"),
        sa.CheckConstraint("birth_date > DATE '1900-01-01'", name="birth_sanity"),
        sa.CheckConstraint("dependents_count >= 0", name="dependents"),
        schema="tiers",
    )

    # --- tiers.legal_entity_profiles — personne morale -----------------------
    op.create_table(
        "legal_entity_profiles",
        sa.Column("tier_id", UUID, nullable=False),
        sa.Column("legal_name", sa.String(300), nullable=False),
        sa.Column("commercial_name", sa.String(300), nullable=True),
        sa.Column("legal_form", sa.String(50), nullable=False),
        sa.Column("rccm_number", sa.String(50), nullable=True),
        sa.Column("nif_number", sa.String(50), nullable=True),
        sa.Column("constitution_date", sa.Date(), nullable=False),
        sa.Column("capital_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("capital_currency_id", UUID, nullable=True),
        sa.Column("business_purpose", sa.Text(), nullable=True),
        sa.Column("headquarters_country_id", UUID, nullable=False),
        sa.PrimaryKeyConstraint("tier_id"),
        sa.ForeignKeyConstraint(["tier_id"], ["tiers.tiers.id"]),
        sa.ForeignKeyConstraint(["capital_currency_id"], ["parameters.currencies.id"]),
        sa.ForeignKeyConstraint(["headquarters_country_id"], ["parameters.countries.id"]),
        sa.CheckConstraint(
            "legal_form IN ('SA','SARL','SAS','SNC','GIE','ASSOCIATION',"
            "'COOPERATIVE','ONG','EI','AUTRE')",
            name="legal_form",
        ),
        sa.CheckConstraint("constitution_date < CURRENT_DATE", name="constitution_past"),
        schema="tiers",
    )
    # Unicité partielle : un RCCM/NIF ne se partage pas, NULL reste permis (données incomplètes).
    op.create_index(
        "uq_legal_entity_profiles_rccm",
        "legal_entity_profiles",
        ["rccm_number"],
        schema="tiers",
        unique=True,
        postgresql_where=sa.text("rccm_number IS NOT NULL"),
    )
    op.create_index(
        "uq_legal_entity_profiles_nif",
        "legal_entity_profiles",
        ["nif_number"],
        schema="tiers",
        unique=True,
        postgresql_where=sa.text("nif_number IS NOT NULL"),
    )

    # --- tiers.group_profiles — groupement solidaire -------------------------
    op.create_table(
        "group_profiles",
        sa.Column("tier_id", UUID, nullable=False),
        sa.Column("group_name", sa.String(300), nullable=False),
        sa.Column("group_type", sa.String(30), nullable=False),
        sa.Column("constitution_date", sa.Date(), nullable=False),
        sa.Column("intervention_zone", sa.String(200), nullable=True),
        sa.Column("group_purpose", sa.Text(), nullable=True),
        sa.Column("expected_member_count", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("tier_id"),
        sa.ForeignKeyConstraint(["tier_id"], ["tiers.tiers.id"]),
        sa.CheckConstraint(
            "group_type IN ('caution_solidaire','tontine','association_locale',"
            "'cooperative_villageoise','autre')",
            name="group_type",
        ),
        sa.CheckConstraint("constitution_date <= CURRENT_DATE", name="constitution"),
        sa.CheckConstraint(
            "expected_member_count IS NULL OR expected_member_count > 0", name="member_count"
        ),
        schema="tiers",
    )

    # --- tiers.numbering_sequences — compteur atomique -----------------------
    # Pas de colonnes *_by : incrémentée par le système sous verrou, aucun utilisateur ne la
    # possède. Pas de seed : le NumberingService (T1b) crée la ligne (prefix, year) à la volée.
    op.create_table(
        "numbering_sequences",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("prefix", sa.String(10), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("last_value", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("prefix", "year"),
        schema="tiers",
    )

    # --- tiers.lifecycle_events — frise chronologique par fiche --------------
    op.create_table(
        "lifecycle_events",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("tier_id", UUID, nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("previous_status", sa.String(30), nullable=True),
        sa.Column("new_status", sa.String(30), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("performed_at", TS, server_default=NOW, nullable=False),
        sa.Column("performed_by", UUID, nullable=False),
        sa.Column("event_metadata", postgresql.JSONB(), nullable=True),  # D3 : pas « metadata »
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tier_id"], ["tiers.tiers.id"]),
        sa.ForeignKeyConstraint(["performed_by"], ["security.users.id"]),
        sa.CheckConstraint(f"event_type IN ({_en_liste_sql(_EVENEMENTS)})", name="event_type"),
        schema="tiers",
    )
    op.create_index(
        "ix_lifecycle_events_tier_id_performed_at",
        "lifecycle_events",
        ["tier_id", "performed_at"],
        schema="tiers",
    )


def downgrade() -> None:
    for table in _TABLES_A_SUPPRIMER:
        op.drop_table(table, schema="tiers")
    op.execute("DROP SCHEMA IF EXISTS tiers")
