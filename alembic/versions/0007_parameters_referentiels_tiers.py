"""Référentiels « parameters » requis par le module Tiers (bloc T0).

CONTEXTE. Le module Tiers (premier module métier) référence en clé étrangère des
référentiels que la spec disait « déjà en place » — ils ne l'étaient pas. À l'ouverture
du module, parameters ne contenait que la table agencies (version minimale du socle).
Cette migration crée les cinq référentiels sans lesquels une fiche client est incomplète :
countries, currencies, regions, cities, identity_document_types. C'est le « DDL fantôme »
attrapé avant d'écrire le code métier.

PÉRIMÈTRE. Version MINIMALE de chaque référentiel : le strict nécessaire aux FK de Tiers,
pas le CRUD ni les colonnes métier étendues (celles-ci relèveront du module Paramétrage).
Comme agencies, ces tables se désactivent par is_active et n'ont PAS de deleted_at : un
référentiel ne se supprime pas, il se désactive.

TROIS PARTIS PRIS EXPLICITES :

  - countries.is_gafi_high_risk semé à FALSE partout. La liste grise/noire du GAFI change
    plusieurs fois par an ; la spec (§5.4) la confie à une mise à jour MANUELLE du
    responsable LBC/FT. Figer une classification en dur la rendrait périmée dès sa livraison.

  - identity_document_types.format_regex laissé NULL sur toutes les lignes. Un format de
    numéro de pièce inventé bloquerait un agent en production. Les regex sont À VÉRIFIER
    AVEC LES AUTORITÉS LOCALES avant d'être renseignés, IMF par IMF.

  - cities : country_id obligatoire, region_id FACULTATIF. En zone rurale, un agent connaît
    le village mais pas toujours son découpage administratif ; forcer une région bloquerait
    la saisie. Un FK composite (region_id, country_id) -> regions(id, country_id) empêche
    malgré tout l'incohérence « ville d'un pays, région d'un autre » sans recourir à un
    trigger : en MATCH SIMPLE, la contrainte ne mord que si region_id est renseigné.

SEED. countries (30 : 8 UEMOA + frontaliers non-UEMOA + diaspora), currencies (XOF/EUR/USD),
identity_document_types (types courants UEMOA). regions et cities sont créées mais NON
semées : ce sont des données de déploiement propres à chaque IMF (comme agencies), on ne
peut pas présumer le pays ni les localités d'exploitation.

DOWNGRADE. DROP des cinq tables dans l'ordre inverse des dépendances. Le schéma parameters
est conservé : il préexiste à cette migration (créé en 0001).

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")

# Ordre de création (le downgrade supprime en sens inverse) : countries en premier,
# tout le reste en dépend ; cities en dernier, il dépend de countries ET regions.
_TABLES_A_SUPPRIMER = (
    "cities",
    "identity_document_types",
    "regions",
    "currencies",
    "countries",
)


def _colonnes_audit() -> tuple[sa.Column, ...]:
    """Les quatre colonnes de traçabilité communes, à l'identique de parameters.agencies.

    created_by / updated_by sont des références d'audit vers security.users, nullables et
    sans relationship : elles portent la trace sans créer de lien de navigation. NULL pour
    les lignes semées par migration (aucun utilisateur à l'origine).
    """
    return (
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_by", UUID, nullable=True),
    )


def _fk_audit(table: str) -> tuple[sa.ForeignKeyConstraint, ...]:
    """Les deux FK created_by/updated_by -> security.users pour une table donnée."""
    return (
        sa.ForeignKeyConstraint(["created_by"], ["security.users.id"]),
        sa.ForeignKeyConstraint(["updated_by"], ["security.users.id"]),
    )


def upgrade() -> None:
    # --- countries -----------------------------------------------------------
    # Cible de nationality_id (obligatoire KYC), birth_country_id, headquarters_country_id,
    # issuing_country_id. Le flag GAFI prépare le scoring de risque du bloc T3.
    op.create_table(
        "countries",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("code", sa.String(2), nullable=False),  # ISO 3166-1 alpha-2
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("is_gafi_high_risk", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("display_order", sa.Integer(), server_default=sa.text("100"), nullable=False),
        *_colonnes_audit(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
        *_fk_audit("countries"),
        schema="parameters",
    )

    # --- currencies ----------------------------------------------------------
    # Cible de legal_entity_profiles.capital_currency_id et des montants futurs.
    # decimal_places : le franc CFA (XOF) a 0 décimale — le formater avec 2 est un bug de
    # rendu qu'on interdit à la source.
    op.create_table(
        "currencies",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("code", sa.String(3), nullable=False),  # ISO 4217
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("symbol", sa.String(8), nullable=True),
        sa.Column("decimal_places", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("display_order", sa.Integer(), server_default=sa.text("100"), nullable=False),
        *_colonnes_audit(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
        sa.CheckConstraint("decimal_places BETWEEN 0 AND 4", name="decimals"),
        *_fk_audit("currencies"),
        schema="parameters",
    )

    # --- regions -------------------------------------------------------------
    # Découpage administratif de 1er niveau, sous un pays. L'UNIQUE (id, country_id) n'est
    # pas redondant avec la PK : il sert de cible au FK composite de cities.
    op.create_table(
        "regions",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("country_id", UUID, nullable=False),
        # code nullable : toutes les régions n'ont pas de code officiel.
        sa.Column("code", sa.String(20), nullable=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("display_order", sa.Integer(), server_default=sa.text("100"), nullable=False),
        *_colonnes_audit(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("country_id", "name"),
        sa.UniqueConstraint("id", "country_id"),  # cible du FK composite de cities
        sa.ForeignKeyConstraint(["country_id"], ["parameters.countries.id"]),
        *_fk_audit("regions"),
        schema="parameters",
    )

    # --- cities --------------------------------------------------------------
    # country_id obligatoire, region_id facultatif (souplesse terrain, cf. docstring).
    # Le FK composite garantit la cohérence pays/région quand une région est renseignée.
    op.create_table(
        "cities",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("country_id", UUID, nullable=False),
        sa.Column("region_id", UUID, nullable=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("display_order", sa.Integer(), server_default=sa.text("100"), nullable=False),
        *_colonnes_audit(),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["country_id"], ["parameters.countries.id"]),
        sa.ForeignKeyConstraint(
            ["region_id", "country_id"],
            ["parameters.regions.id", "parameters.regions.country_id"],
        ),
        *_fk_audit("cities"),
        schema="parameters",
    )
    op.create_index("ix_cities_country_id", "cities", ["country_id"], schema="parameters")
    op.create_index("ix_cities_region_id", "cities", ["region_id"], schema="parameters")

    # --- identity_document_types --------------------------------------------
    # Cible de identity_documents.document_type_id (bloc T2). format_regex laissé NULL :
    # À VÉRIFIER AVEC AUTORITÉS LOCALES avant de renseigner un format, IMF par IMF.
    op.create_table(
        "identity_document_types",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("code", sa.String(50), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("country_id", UUID, nullable=True),  # NULL = type générique (ex. passeport)
        sa.Column("requires_expiry_date", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("requires_issuer", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("enforce_unique", sa.Boolean(), server_default=sa.true(), nullable=False),
        # À VÉRIFIER AVEC AUTORITÉS LOCALES — aucun format inventé, laissé NULL au seed.
        sa.Column("format_regex", sa.String(200), nullable=True),
        sa.Column("format_example", sa.String(100), nullable=True),
        sa.Column(
            "acceptance_level", sa.String(20), server_default=sa.text("'standard'"), nullable=False
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("display_order", sa.Integer(), server_default=sa.text("100"), nullable=False),
        *_colonnes_audit(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
        sa.CheckConstraint(
            "acceptance_level IN ('standard','reduced','exceptional')", name="acceptance"
        ),
        sa.ForeignKeyConstraint(["country_id"], ["parameters.countries.id"]),
        *_fk_audit("identity_document_types"),
        schema="parameters",
    )

    _seed(op)


def _seed(op) -> None:
    """Données de départ : countries, currencies, identity_document_types.

    regions et cities restent vides : données de déploiement propres à chaque IMF.
    """
    countries = sa.table(
        "countries",
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("display_order", sa.Integer),
        schema="parameters",
    )
    # UEMOA en tête (display_order 10-80), le reste à 100 (tri alphabétique en UI).
    # is_gafi_high_risk garde son défaut FALSE — classification manuelle par le resp. LBC/FT.
    op.bulk_insert(
        countries,
        [
            {"code": "BJ", "name": "Bénin", "display_order": 10},
            {"code": "BF", "name": "Burkina Faso", "display_order": 20},
            {"code": "CI", "name": "Côte d'Ivoire", "display_order": 30},
            {"code": "GW", "name": "Guinée-Bissau", "display_order": 40},
            {"code": "ML", "name": "Mali", "display_order": 50},
            {"code": "NE", "name": "Niger", "display_order": 60},
            {"code": "SN", "name": "Sénégal", "display_order": 70},
            {"code": "TG", "name": "Togo", "display_order": 80},
            # Frontaliers non-UEMOA (commerce transfrontalier quotidien) + diaspora.
            {"code": "DE", "name": "Allemagne", "display_order": 100},
            {"code": "SA", "name": "Arabie saoudite", "display_order": 100},
            {"code": "BE", "name": "Belgique", "display_order": 100},
            {"code": "CM", "name": "Cameroun", "display_order": 100},
            {"code": "CA", "name": "Canada", "display_order": 100},
            {"code": "CN", "name": "Chine", "display_order": 100},
            {"code": "AE", "name": "Émirats arabes unis", "display_order": 100},
            {"code": "ES", "name": "Espagne", "display_order": 100},
            {"code": "US", "name": "États-Unis", "display_order": 100},
            {"code": "FR", "name": "France", "display_order": 100},
            {"code": "GM", "name": "Gambie", "display_order": 100},
            {"code": "GH", "name": "Ghana", "display_order": 100},
            {"code": "GN", "name": "Guinée", "display_order": 100},
            {"code": "IT", "name": "Italie", "display_order": 100},
            {"code": "LR", "name": "Liberia", "display_order": 100},
            {"code": "MA", "name": "Maroc", "display_order": 100},
            {"code": "MR", "name": "Mauritanie", "display_order": 100},
            {"code": "NG", "name": "Nigéria", "display_order": 100},
            {"code": "QA", "name": "Qatar", "display_order": 100},
            {"code": "GB", "name": "Royaume-Uni", "display_order": 100},
            {"code": "SL", "name": "Sierra Leone", "display_order": 100},
            {"code": "TD", "name": "Tchad", "display_order": 100},
        ],
    )

    currencies = sa.table(
        "currencies",
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("symbol", sa.String),
        sa.column("decimal_places", sa.SmallInteger),
        sa.column("display_order", sa.Integer),
        schema="parameters",
    )
    op.bulk_insert(
        currencies,
        [
            {
                "code": "XOF",
                "name": "Franc CFA (BCEAO)",
                "symbol": "FCFA",
                "decimal_places": 0,
                "display_order": 10,
            },
            {
                "code": "EUR",
                "name": "Euro",
                "symbol": "€",
                "decimal_places": 2,
                "display_order": 100,
            },
            {
                "code": "USD",
                "name": "Dollar américain",
                "symbol": "$",
                "decimal_places": 2,
                "display_order": 100,
            },
        ],
    )

    doc_types = sa.table(
        "identity_document_types",
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("requires_expiry_date", sa.Boolean),
        sa.column("requires_issuer", sa.Boolean),
        sa.column("enforce_unique", sa.Boolean),
        sa.column("acceptance_level", sa.String),
        sa.column("display_order", sa.Integer),
        schema="parameters",
    )
    # country_id NULL (types génériques), format_regex NULL (À VÉRIFIER AUTORITÉS LOCALES).
    # Les niveaux reduced/exceptional traduisent la faible bancarisation : une attestation
    # de chef de quartier ne vaut pas une CNI, et le scoring KYC (T3) devra le savoir.
    op.bulk_insert(
        doc_types,
        [
            {
                "code": "CNI",
                "name": "Carte nationale d'identité",
                "requires_expiry_date": True,
                "requires_issuer": True,
                "enforce_unique": True,
                "acceptance_level": "standard",
                "display_order": 10,
            },
            {
                "code": "PASSPORT",
                "name": "Passeport",
                "requires_expiry_date": True,
                "requires_issuer": True,
                "enforce_unique": True,
                "acceptance_level": "standard",
                "display_order": 20,
            },
            {
                "code": "CARTE_CONSULAIRE",
                "name": "Carte consulaire",
                "requires_expiry_date": True,
                "requires_issuer": True,
                "enforce_unique": True,
                "acceptance_level": "standard",
                "display_order": 30,
            },
            {
                "code": "PERMIS_CONDUIRE",
                "name": "Permis de conduire",
                "requires_expiry_date": True,
                "requires_issuer": True,
                "enforce_unique": True,
                "acceptance_level": "standard",
                "display_order": 40,
            },
            {
                "code": "CARTE_ELECTEUR",
                "name": "Carte d'électeur",
                "requires_expiry_date": False,
                "requires_issuer": True,
                "enforce_unique": True,
                "acceptance_level": "reduced",
                "display_order": 50,
            },
            {
                "code": "ATTESTATION_NAISSANCE",
                "name": "Attestation de naissance",
                "requires_expiry_date": False,
                "requires_issuer": True,
                "enforce_unique": False,
                "acceptance_level": "reduced",
                "display_order": 60,
            },
            {
                "code": "ATTESTATION_QUARTIER",
                "name": "Attestation du chef de quartier",
                "requires_expiry_date": False,
                "requires_issuer": True,
                "enforce_unique": False,
                "acceptance_level": "exceptional",
                "display_order": 70,
            },
        ],
    )


def downgrade() -> None:
    for table in _TABLES_A_SUPPRIMER:
        op.drop_table(table, schema="parameters")
