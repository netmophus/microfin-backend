"""Modèles ORM du schéma « tiers » — Class Table Inheritance (D1).

Mappe les tables créées par la migration 0008 ; ces classes ne créent rien, toute structure
vient des migrations. L'héritage joined : une table parent Tier avec le discriminateur
tier_type, trois enfants (IndividualProfile / LegalEntityProfile / GroupProfile) dont la PK
est aussi la FK vers le parent.

DEUX CHOIX qui méritent d'être compris :

  - Le parent Tier porte polymorphic_on=tier_type mais AUCUN polymorphic_identity. Aucune
    ligne n'aura jamais tier_type='tier' (le CHECK de la migration l'interdit) et on
    n'instancie jamais un Tier nu — toujours l'un des trois sous-types. Un chargement
    polymorphe résout donc toujours vers un enfant. C'est le montage joined correct ici.

  - Les FK et index déclarés ici reflètent EXACTEMENT la migration 0008. C'est ce que le
    méta-test « alembic check » exige : un modèle qui omettrait une FK ou un index que la base
    porte serait signalé comme une dérive. Les CHECK, eux, ne sont pas comparés par alembic —
    on ne les redéclare pas ici, la base les impose. Aucune relationship n'est déclarée (pas
    de navigation ORM à ce stade) ; les FK ne servent qu'à la parité et à l'intégrité.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")
FK_USER = "security.users.id"
FK_TIER = "tiers.tiers.id"


class Tier(Base):
    """Table parent — données communes à tous les types de tiers."""

    __tablename__ = "tiers"
    __table_args__: tuple[Any, ...] = (
        # Index de la spec §9.5 — partiels : cloisonnement et listes ignorent les supprimées.
        sa.Index(
            "ix_tiers_type_status",
            "tier_type",
            "status",
            postgresql_where=sa.text("deleted_at IS NULL"),
        ),
        sa.Index(
            "ix_tiers_agency_status",
            "primary_agency_id",
            "status",
            postgresql_where=sa.text("deleted_at IS NULL"),
        ),
        sa.Index(
            "ix_tiers_activated_at",
            "activated_at",
            postgresql_where=sa.text("activated_at IS NOT NULL"),
        ),
        {"schema": "tiers"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    tier_number: Mapped[str] = mapped_column(sa.String(20), nullable=False, unique=True)
    tier_type: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    primary_agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("parameters.agencies.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        sa.String(30), nullable=False, server_default=sa.text("'prospect'")
    )
    primary_phone: Mapped[str | None] = mapped_column(sa.String(30))
    language_preference: Mapped[str | None] = mapped_column(sa.String(10))
    activated_at: Mapped[datetime | None] = mapped_column(TS)
    activated_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    suspended_at: Mapped[datetime | None] = mapped_column(TS)
    suspended_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    suspension_reason: Mapped[str | None] = mapped_column(sa.Text())
    merged_into_tier_id: Mapped[uuid.UUID | None] = mapped_column(  # placeholder fusion (T4)
        UUID, sa.ForeignKey(FK_TIER)
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    deleted_at: Mapped[datetime | None] = mapped_column(TS)  # soft delete uniquement

    # RUF012 est ignoré ci-dessous : __mapper_args__ est le contrat SQLAlchemy (un dict est
    # attendu). L'annoter ClassVar déclencherait une erreur mypy (override d'une variable
    # d'instance de DeclarativeBase). ruff et mypy s'opposent ici ; on tranche pour mypy.
    __mapper_args__ = {  # noqa: RUF012
        "polymorphic_on": "tier_type",
        # pas de polymorphic_identity : le parent n'est jamais une ligne (cf. docstring).
    }

    def __repr__(self) -> str:
        return f"<Tier {self.tier_number} ({self.tier_type})>"


class IndividualProfile(Tier):
    """Personne physique — extension jointe de Tier."""

    __tablename__ = "individual_profiles"
    __table_args__: tuple[Any, ...] = ({"schema": "tiers"},)

    tier_id: Mapped[uuid.UUID] = mapped_column(UUID, sa.ForeignKey(FK_TIER), primary_key=True)
    last_name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    first_name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    middle_names: Mapped[str | None] = mapped_column(sa.String(200))
    name_at_birth: Mapped[str | None] = mapped_column(sa.String(200))
    birth_date: Mapped[date] = mapped_column(sa.Date(), nullable=False)
    birth_place: Mapped[str | None] = mapped_column(sa.String(200))
    birth_country_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("parameters.countries.id")
    )
    gender: Mapped[str] = mapped_column(sa.CHAR(1), nullable=False)
    nationality_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("parameters.countries.id"), nullable=False
    )
    secondary_nationality_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("parameters.countries.id")
    )
    marital_status: Mapped[str | None] = mapped_column(sa.String(30))
    dependents_count: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, server_default=sa.text("0")
    )
    profession: Mapped[str | None] = mapped_column(sa.String(200))
    monthly_income_estimate: Mapped[Decimal | None] = mapped_column(sa.Numeric(18, 2))
    is_literate: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=sa.true()
    )
    # TODO(T2/Paramétrage) : activity_sector_id, education_level_id — référentiels non créés.
    # name_phonetic_key appartient à la recherche/déduplication (T4), pas à ce stade.

    __mapper_args__ = {"polymorphic_identity": "individual"}  # noqa: RUF012


class LegalEntityProfile(Tier):
    """Personne morale — extension jointe de Tier."""

    __tablename__ = "legal_entity_profiles"
    __table_args__: tuple[Any, ...] = (
        # Unicité partielle : un RCCM/NIF ne se partage pas, NULL reste permis.
        sa.Index(
            "uq_legal_entity_profiles_rccm",
            "rccm_number",
            unique=True,
            postgresql_where=sa.text("rccm_number IS NOT NULL"),
        ),
        sa.Index(
            "uq_legal_entity_profiles_nif",
            "nif_number",
            unique=True,
            postgresql_where=sa.text("nif_number IS NOT NULL"),
        ),
        {"schema": "tiers"},
    )

    tier_id: Mapped[uuid.UUID] = mapped_column(UUID, sa.ForeignKey(FK_TIER), primary_key=True)
    legal_name: Mapped[str] = mapped_column(sa.String(300), nullable=False)
    commercial_name: Mapped[str | None] = mapped_column(sa.String(300))
    legal_form: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    rccm_number: Mapped[str | None] = mapped_column(sa.String(50))
    nif_number: Mapped[str | None] = mapped_column(sa.String(50))
    constitution_date: Mapped[date] = mapped_column(sa.Date(), nullable=False)
    capital_amount: Mapped[Decimal | None] = mapped_column(sa.Numeric(18, 2))
    capital_currency_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("parameters.currencies.id")
    )
    business_purpose: Mapped[str | None] = mapped_column(sa.Text())
    headquarters_country_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("parameters.countries.id"), nullable=False
    )

    __mapper_args__ = {"polymorphic_identity": "legal_entity"}  # noqa: RUF012


class GroupProfile(Tier):
    """Groupement solidaire — extension jointe de Tier."""

    __tablename__ = "group_profiles"
    __table_args__: tuple[Any, ...] = ({"schema": "tiers"},)

    tier_id: Mapped[uuid.UUID] = mapped_column(UUID, sa.ForeignKey(FK_TIER), primary_key=True)
    group_name: Mapped[str] = mapped_column(sa.String(300), nullable=False)
    group_type: Mapped[str] = mapped_column(sa.String(30), nullable=False)
    constitution_date: Mapped[date] = mapped_column(sa.Date(), nullable=False)
    intervention_zone: Mapped[str | None] = mapped_column(sa.String(200))
    group_purpose: Mapped[str | None] = mapped_column(sa.Text())
    expected_member_count: Mapped[int | None] = mapped_column(sa.Integer())

    __mapper_args__ = {"polymorphic_identity": "group"}  # noqa: RUF012


class NumberingSequence(Base):
    """Compteur atomique des numéros lisibles, une ligne par (prefix, année).

    Incrémentée par le NumberingService sous verrou de ligne. Pas de colonnes *_by : aucun
    utilisateur ne « possède » une séquence, elle est bumpée par le système.
    """

    __tablename__ = "numbering_sequences"
    __table_args__: tuple[Any, ...] = (
        sa.UniqueConstraint("prefix", "year"),
        {"schema": "tiers"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    prefix: Mapped[str] = mapped_column(sa.String(10), nullable=False)
    year: Mapped[int] = mapped_column(sa.Integer(), nullable=False)
    last_value: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, server_default=sa.text("0")
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)


class LifecycleEvent(Base):
    """Frise chronologique par fiche — événements de cycle de vie (D5).

    event_metadata, pas metadata : ce dernier est réservé par SQLAlchemy Declarative (D3).
    """

    __tablename__ = "lifecycle_events"
    __table_args__: tuple[Any, ...] = (
        sa.Index("ix_lifecycle_events_tier_id_performed_at", "tier_id", "performed_at"),
        {"schema": "tiers"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    tier_id: Mapped[uuid.UUID] = mapped_column(UUID, sa.ForeignKey(FK_TIER), nullable=False)
    event_type: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    previous_status: Mapped[str | None] = mapped_column(sa.String(30))
    new_status: Mapped[str | None] = mapped_column(sa.String(30))
    reason: Mapped[str | None] = mapped_column(sa.Text())
    performed_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    performed_by: Mapped[uuid.UUID] = mapped_column(UUID, sa.ForeignKey(FK_USER), nullable=False)
    event_metadata: Mapped[dict[str, Any] | None] = mapped_column(postgresql.JSONB())


class IdentityDocument(Base):
    """Pièce d'identité d'un tiers (T2). Plusieurs par tiers ; au plus une principale vivante.

    Numéro NON unique en base (une attestation de quartier peut légitimement répéter un numéro
    d'ordre) : l'unicité, quand le type l'exige, est portée par le service (T2c).
    """

    __tablename__ = "identity_documents"
    __table_args__ = (
        sa.Index(
            "uq_identity_documents_primary",
            "tier_id",
            unique=True,
            postgresql_where=sa.text("is_primary AND deleted_at IS NULL"),
        ),
        sa.Index(
            "ix_identity_documents_tier_id",
            "tier_id",
            postgresql_where=sa.text("deleted_at IS NULL"),
        ),
        sa.Index("ix_identity_documents_document_number", "document_number"),
        {"schema": "tiers"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    tier_id: Mapped[uuid.UUID] = mapped_column(UUID, sa.ForeignKey(FK_TIER), nullable=False)
    document_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("parameters.identity_document_types.id"), nullable=False
    )
    document_number: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    issuing_country_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("parameters.countries.id")
    )
    issuing_authority: Mapped[str | None] = mapped_column(sa.String(200))
    date_of_issue: Mapped[date | None] = mapped_column(sa.Date())
    expiry_date: Mapped[date | None] = mapped_column(sa.Date())
    is_primary: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=sa.false()
    )
    is_verified: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=sa.false()
    )
    verified_at: Mapped[datetime | None] = mapped_column(TS)
    verified_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    verification_notes: Mapped[str | None] = mapped_column(sa.Text())
    scanned_document_id: Mapped[uuid.UUID | None] = mapped_column(UUID)  # placeholder T5
    notes: Mapped[str | None] = mapped_column(sa.Text())
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    deleted_at: Mapped[datetime | None] = mapped_column(TS)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    deletion_reason: Mapped[str | None] = mapped_column(sa.Text())


class Contact(Base):
    """Coordonnée d'un tiers (T2) — téléphone / email / adresse, discriminé par contact_type.

    Une seule table : l'adresse porte formel ET repère libre dans la même ligne. Le champ
    email_address est en citext (insensible à la casse, comme users.email). Une principale par
    type (un tél principal, une adresse principale) : uq_contacts_primary_par_type.
    """

    __tablename__ = "contacts"
    __table_args__ = (
        sa.Index(
            "uq_contacts_primary_par_type",
            "tier_id",
            "contact_type",
            unique=True,
            postgresql_where=sa.text("is_primary AND deleted_at IS NULL"),
        ),
        sa.Index(
            "ix_contacts_tier_type",
            "tier_id",
            "contact_type",
            postgresql_where=sa.text("deleted_at IS NULL"),
        ),
        sa.Index(
            "ix_contacts_phone",
            "phone_number",
            postgresql_where=sa.text("phone_number IS NOT NULL AND deleted_at IS NULL"),
        ),
        sa.Index(
            "ix_contacts_email",
            "email_address",
            postgresql_where=sa.text("email_address IS NOT NULL AND deleted_at IS NULL"),
        ),
        {"schema": "tiers"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    tier_id: Mapped[uuid.UUID] = mapped_column(UUID, sa.ForeignKey(FK_TIER), nullable=False)
    contact_type: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    contact_subtype: Mapped[str | None] = mapped_column(sa.String(30))
    phone_number: Mapped[str | None] = mapped_column(sa.String(20))  # E.164
    phone_raw: Mapped[str | None] = mapped_column(sa.String(50))  # saisie originale
    phone_country_code: Mapped[str | None] = mapped_column(sa.String(5))
    email_address: Mapped[str | None] = mapped_column(postgresql.CITEXT())
    address_line1: Mapped[str | None] = mapped_column(sa.String(300))
    address_line2: Mapped[str | None] = mapped_column(sa.String(300))
    quarter: Mapped[str | None] = mapped_column(sa.String(200))
    landmark: Mapped[str | None] = mapped_column(sa.String(300))  # point de repère
    city_id: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey("parameters.cities.id"))
    region_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("parameters.regions.id")
    )
    country_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("parameters.countries.id")
    )
    postal_code: Mapped[str | None] = mapped_column(sa.String(20))
    latitude: Mapped[Decimal | None] = mapped_column(sa.Numeric(10, 7))
    longitude: Mapped[Decimal | None] = mapped_column(sa.Numeric(10, 7))
    is_primary: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=sa.false()
    )
    is_verified: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=sa.false()
    )
    verified_at: Mapped[datetime | None] = mapped_column(TS)
    verified_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    verification_method: Mapped[str | None] = mapped_column(sa.String(50))
    valid_from: Mapped[date | None] = mapped_column(sa.Date())
    valid_to: Mapped[date | None] = mapped_column(sa.Date())
    notes: Mapped[str | None] = mapped_column(sa.Text())
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    deleted_at: Mapped[datetime | None] = mapped_column(TS)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    deletion_reason: Mapped[str | None] = mapped_column(sa.Text())
