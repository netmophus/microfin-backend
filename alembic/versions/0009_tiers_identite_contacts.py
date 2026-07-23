"""Pièces d'identité et coordonnées des tiers (bloc T2a).

Deux tables, PUR SCHÉMA — aucun CRUD, aucune normalisation, aucune donnée migrée (tout cela
est T2b/T2c). Le champ tiers.tiers.primary_phone reste intact : rien ne casse ici.

tiers.identity_documents — pièces MULTIPLES par tiers (CNI, passeport, carte d'électeur…),
chacune avec sa validité, son émetteur, et qui l'a vérifiée. Le type vient du référentiel
parameters.identity_document_types (T0). AU PLUS UNE pièce PRINCIPALE vivante par tiers : index
unique partiel uq_identity_documents_primary — la base refuse la seconde ; le service (T2c) fait
le basculement proprement. Le numéro n'est PAS unique en base (une attestation de quartier peut
légitimement porter le même numéro d'ordre) : l'unicité, quand le type l'exige (enforce_unique),
est portée par le service.

tiers.contacts — téléphones / emails / adresses, UNE SEULE table discriminée par contact_type.
  - ADRESSE EN ZONE RURALE : formel (pays/région/ville/quartier/rue) ET repère libre (landmark)
    dans la même ligne. ck_contacts_address accepte une adresse dès qu'elle a une RUE OU un
    REPÈRE : « derrière la mosquée, à côté de la boutique de Salif » suffit, jamais rejeté faute
    de rue — sans quoi le produit serait inutilisable dans la majorité des zones d'intervention.
  - TÉLÉPHONE : phone_raw (exactement ce que l'agent a saisi, traçabilité) + phone_number
    (E.164, pour la recherche et la dédup de T4) + phone_country_code. ck_contacts_phone exige
    phone_number : tout numéro stocké est normalisé, donc cherchable. La table naît vide ; c'est
    T2b (avec phonenumbers) qui la remplira et backfillera les primary_phone existants.
  - email_address en CITEXT (comme security.users.email) : insensibilité à la casse NATIVE,
    donc un index simple suffit — pas d'index fonctionnel LOWER() que « alembic check » compare
    mal. citext est déjà installé (migration 0001).
  - UNE principale PAR TYPE (un tél principal, une adresse principale) : uq_contacts_primary.

Suppression LOGIQUE avec MOTIF sur les deux tables : deleted_at + deleted_by + deletion_reason.

DOWNGRADE : DROP des deux tables (aucune dépendance entre elles).

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")
FK_USER = "security.users.id"


def _colonnes_audit_et_suppression() -> tuple[sa.Column, ...]:
    """Traçabilité + soft delete AVEC motif, communes aux deux tables."""
    return (
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_by", UUID, nullable=True),
        sa.Column("deleted_at", TS, nullable=True),
        sa.Column("deleted_by", UUID, nullable=True),
        sa.Column("deletion_reason", sa.Text(), nullable=True),
    )


def upgrade() -> None:
    # --- tiers.identity_documents --------------------------------------------
    op.create_table(
        "identity_documents",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("tier_id", UUID, nullable=False),
        sa.Column("document_type_id", UUID, nullable=False),
        sa.Column("document_number", sa.String(50), nullable=False),
        sa.Column("issuing_country_id", UUID, nullable=True),
        sa.Column("issuing_authority", sa.String(200), nullable=True),
        sa.Column("date_of_issue", sa.Date(), nullable=True),
        sa.Column("expiry_date", sa.Date(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_verified", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("verified_at", TS, nullable=True),
        sa.Column("verified_by", UUID, nullable=True),
        sa.Column("verification_notes", sa.Text(), nullable=True),
        sa.Column("scanned_document_id", UUID, nullable=True),  # placeholder tiers.documents (T5)
        sa.Column("notes", sa.Text(), nullable=True),
        *_colonnes_audit_et_suppression(),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "expiry_date IS NULL OR date_of_issue IS NULL OR expiry_date >= date_of_issue",
            name="dates",
        ),
        sa.CheckConstraint(
            "date_of_issue IS NULL OR date_of_issue <= CURRENT_DATE", name="issue_past"
        ),
        sa.ForeignKeyConstraint(["tier_id"], ["tiers.tiers.id"]),
        sa.ForeignKeyConstraint(["document_type_id"], ["parameters.identity_document_types.id"]),
        sa.ForeignKeyConstraint(["issuing_country_id"], ["parameters.countries.id"]),
        sa.ForeignKeyConstraint(["verified_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["created_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["updated_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["deleted_by"], [FK_USER]),
        schema="tiers",
    )
    # Au plus UNE pièce principale vivante par tiers ; le service fait le swap, ceci est le rempart.
    op.create_index(
        "uq_identity_documents_primary",
        "identity_documents",
        ["tier_id"],
        schema="tiers",
        unique=True,
        postgresql_where=sa.text("is_primary AND deleted_at IS NULL"),
    )
    op.create_index(
        "ix_identity_documents_tier_id",
        "identity_documents",
        ["tier_id"],
        schema="tiers",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_identity_documents_document_number",
        "identity_documents",
        ["document_number"],
        schema="tiers",
    )

    # --- tiers.contacts ------------------------------------------------------
    op.create_table(
        "contacts",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("tier_id", UUID, nullable=False),
        sa.Column("contact_type", sa.String(20), nullable=False),
        sa.Column("contact_subtype", sa.String(30), nullable=True),
        # Téléphone
        sa.Column("phone_number", sa.String(20), nullable=True),  # E.164
        sa.Column("phone_raw", sa.String(50), nullable=True),  # saisie originale
        sa.Column("phone_country_code", sa.String(5), nullable=True),
        # Email — citext : insensible à la casse nativement (comme users.email).
        sa.Column("email_address", postgresql.CITEXT(), nullable=True),
        # Adresse : formel + repère libre.
        sa.Column("address_line1", sa.String(300), nullable=True),
        sa.Column("address_line2", sa.String(300), nullable=True),
        sa.Column("quarter", sa.String(200), nullable=True),
        sa.Column("landmark", sa.String(300), nullable=True),
        sa.Column("city_id", UUID, nullable=True),
        sa.Column("region_id", UUID, nullable=True),
        sa.Column("country_id", UUID, nullable=True),
        sa.Column("postal_code", sa.String(20), nullable=True),
        sa.Column("latitude", sa.Numeric(10, 7), nullable=True),
        sa.Column("longitude", sa.Numeric(10, 7), nullable=True),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_verified", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("verified_at", TS, nullable=True),
        sa.Column("verified_by", UUID, nullable=True),
        sa.Column("verification_method", sa.String(50), nullable=True),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        *_colonnes_audit_et_suppression(),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("contact_type IN ('phone','email','address')", name="type"),
        # Le bon jeu de champs selon le type. La ligne clé pour la zone rurale : une adresse est
        # valide dès qu'elle a une RUE OU un REPÈRE.
        sa.CheckConstraint("contact_type <> 'phone' OR phone_number IS NOT NULL", name="phone"),
        sa.CheckConstraint("contact_type <> 'email' OR email_address IS NOT NULL", name="email"),
        sa.CheckConstraint(
            "contact_type <> 'address' OR (address_line1 IS NOT NULL OR landmark IS NOT NULL)",
            name="address",
        ),
        sa.ForeignKeyConstraint(["tier_id"], ["tiers.tiers.id"]),
        sa.ForeignKeyConstraint(["city_id"], ["parameters.cities.id"]),
        sa.ForeignKeyConstraint(["region_id"], ["parameters.regions.id"]),
        sa.ForeignKeyConstraint(["country_id"], ["parameters.countries.id"]),
        sa.ForeignKeyConstraint(["verified_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["created_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["updated_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["deleted_by"], [FK_USER]),
        schema="tiers",
    )
    # Une principale PAR TYPE (un tél principal, une adresse principale…).
    op.create_index(
        "uq_contacts_primary_par_type",
        "contacts",
        ["tier_id", "contact_type"],
        schema="tiers",
        unique=True,
        postgresql_where=sa.text("is_primary AND deleted_at IS NULL"),
    )
    op.create_index(
        "ix_contacts_tier_type",
        "contacts",
        ["tier_id", "contact_type"],
        schema="tiers",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    # Recherche / dédup (T4) par téléphone normalisé et par email (citext).
    op.create_index(
        "ix_contacts_phone",
        "contacts",
        ["phone_number"],
        schema="tiers",
        postgresql_where=sa.text("phone_number IS NOT NULL AND deleted_at IS NULL"),
    )
    op.create_index(
        "ix_contacts_email",
        "contacts",
        ["email_address"],
        schema="tiers",
        postgresql_where=sa.text("email_address IS NOT NULL AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("contacts", schema="tiers")
    op.drop_table("identity_documents", schema="tiers")
