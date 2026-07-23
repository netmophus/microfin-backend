"""Modèles du schéma « parameters » — version minimale requise par le socle Sécurité.

Mappe des tables créées par la migration 0001 (§3.3 du document de décisions v1.0). Ces
classes ne créent rien : toute structure vient des migrations.

Ce module est volontairement sans dépendance : ses FK vers security.users sont déclarées
par chaîne (« security.users.id »), et aucune relationship ne remonte vers User. Il peut
donc être importé seul, et security/audit peuvent l'importer sans cycle.
"""

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")


class Agency(Base):
    """Agence — cible de users.primary_agency_id et de user_agencies (§3.3).

    Version minimale : juste de quoi honorer les FK. Le CRUD complet et les colonnes
    métier (adresse, responsable, horaires…) relèvent du module Paramétrage, à venir.
    Les colonnes created_by / updated_by portent la traçabilité sans relationship : ce
    sont des références d'audit, pas des liens de navigation.

    use_alter sur ces deux FK : agencies et users se référencent mutuellement
    (users.primary_agency_id -> agencies, agencies.created_by -> users). Sans use_alter,
    SQLAlchemy ne sait pas ordonner les deux tables et avertit d'un cycle irrésoluble.
    Le marqueur décrit exactement ce que fait la migration 0001 — créer agencies sans ces
    FK, puis les ajouter par ALTER une fois users créée.
    """

    __tablename__ = "agencies"
    __table_args__ = ({"schema": "parameters"},)

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    code: Mapped[str] = mapped_column(sa.String(30), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(sa.String(150), nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, server_default=sa.true())
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("security.users.id", use_alter=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("security.users.id", use_alter=True), nullable=True
    )

    def __repr__(self) -> str:
        return f"<Agency {self.code}>"


# --- Référentiels du module Tiers (migration 0007) ---------------------------------------
# Modèles de LECTURE : ils mappent des tables semées par migration et lues en FK. Pas de CRUD
# à ce stade (le module Paramétrage le portera). Ils existent pour tenir la parité modèles↔base
# (le méta-test alembic check) et servir de cibles aux FK des modèles Tiers.
#
# Les FK externes vers security.users (created_by/updated_by) sont déclarées comme sur Agency.
# Les FK vers d'autres tables de parameters sont internes au schéma et se résolvent ici même.

TRUE = sa.true()
FALSE = sa.false()
INT_100 = sa.text("100")
INT_0 = sa.text("0")


class Country(Base):
    """Pays — cible de nationality_id, birth_country_id, headquarters_country_id, etc."""

    __tablename__ = "countries"
    __table_args__ = (
        sa.UniqueConstraint("code"),
        {"schema": "parameters"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    code: Mapped[str] = mapped_column(sa.String(2), nullable=False)  # ISO 3166-1 alpha-2
    name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    is_gafi_high_risk: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=FALSE
    )
    is_active: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, server_default=TRUE)
    display_order: Mapped[int] = mapped_column(sa.Integer(), nullable=False, server_default=INT_100)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey("security.users.id"))
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey("security.users.id"))

    def __repr__(self) -> str:
        return f"<Country {self.code}>"


class Currency(Base):
    """Devise — cible de capital_currency_id et des montants futurs. decimal_places : XOF = 0."""

    __tablename__ = "currencies"
    __table_args__ = (
        sa.UniqueConstraint("code"),
        {"schema": "parameters"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    code: Mapped[str] = mapped_column(sa.String(3), nullable=False)  # ISO 4217
    name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    symbol: Mapped[str | None] = mapped_column(sa.String(8))
    decimal_places: Mapped[int] = mapped_column(
        sa.SmallInteger(), nullable=False, server_default=INT_0
    )
    is_active: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, server_default=TRUE)
    display_order: Mapped[int] = mapped_column(sa.Integer(), nullable=False, server_default=INT_100)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey("security.users.id"))
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey("security.users.id"))

    def __repr__(self) -> str:
        return f"<Currency {self.code}>"


class Region(Base):
    """Découpage administratif de 1er niveau. L'UNIQUE (id, country_id) est la cible du FK
    composite de City (cohérence pays/région)."""

    __tablename__ = "regions"
    __table_args__ = (
        sa.UniqueConstraint("country_id", "name"),
        sa.UniqueConstraint("id", "country_id"),
        {"schema": "parameters"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    country_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("parameters.countries.id"), nullable=False
    )
    code: Mapped[str | None] = mapped_column(sa.String(20))
    name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, server_default=TRUE)
    display_order: Mapped[int] = mapped_column(sa.Integer(), nullable=False, server_default=INT_100)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey("security.users.id"))
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey("security.users.id"))

    def __repr__(self) -> str:
        return f"<Region {self.name}>"


class City(Base):
    """Ville/localité. country_id obligatoire, region_id facultatif ; le FK composite garantit
    la cohérence pays/région quand une région est renseignée."""

    __tablename__ = "cities"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["region_id", "country_id"],
            ["parameters.regions.id", "parameters.regions.country_id"],
        ),
        sa.Index("ix_cities_country_id", "country_id"),
        sa.Index("ix_cities_region_id", "region_id"),
        {"schema": "parameters"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    country_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("parameters.countries.id"), nullable=False
    )
    region_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, server_default=TRUE)
    display_order: Mapped[int] = mapped_column(sa.Integer(), nullable=False, server_default=INT_100)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey("security.users.id"))
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey("security.users.id"))

    def __repr__(self) -> str:
        return f"<City {self.name}>"


class IdentityDocumentType(Base):
    """Type de pièce d'identité — cible de identity_documents.document_type_id (bloc T2)."""

    __tablename__ = "identity_document_types"
    __table_args__ = (
        sa.UniqueConstraint("code"),
        {"schema": "parameters"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    code: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    country_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("parameters.countries.id")
    )
    requires_expiry_date: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=TRUE
    )
    requires_issuer: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, server_default=TRUE)
    enforce_unique: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, server_default=TRUE)
    format_regex: Mapped[str | None] = mapped_column(sa.String(200))
    format_example: Mapped[str | None] = mapped_column(sa.String(100))
    acceptance_level: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, server_default=sa.text("'standard'")
    )
    is_active: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, server_default=TRUE)
    display_order: Mapped[int] = mapped_column(sa.Integer(), nullable=False, server_default=INT_100)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey("security.users.id"))
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey("security.users.id"))

    def __repr__(self) -> str:
        return f"<IdentityDocumentType {self.code}>"
