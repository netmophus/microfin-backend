"""Contrats d'entrée/sortie de l'API Tiers (T1c).

Les noms de champs des schémas d'ENTRÉE sont alignés sur les colonnes des modèles : le
service construit l'objet ORM par `**corps.model_dump(...)`, sans recopier 15 champs à la
main. Les énumérations (genre, forme juridique…) sont des Literal : une valeur hors liste
échoue en 422 à la validation, avant d'atteindre le CHECK de la base.

La SORTIE est construite champ par champ dans le router (aucun from_attributes) : ce qui
n'est pas écrit explicitement ne sort pas.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Genre = Literal["M", "F"]
EtatCivil = Literal["celibataire", "marie", "divorce", "veuf", "union_libre", "autre"]
FormeJuridique = Literal[
    "SA", "SARL", "SAS", "SNC", "GIE", "ASSOCIATION", "COOPERATIVE", "ONG", "EI", "AUTRE"
]
TypeGroupement = Literal[
    "caution_solidaire", "tontine", "association_locale", "cooperative_villageoise", "autre"
]


class _CommunEntree(BaseModel):
    """Champs communs à la création de tout type de tiers."""

    primary_phone: str | None = Field(default=None, max_length=30)
    language_preference: str | None = Field(default=None, max_length=10)
    # Agence de rattachement. IGNORÉE puis dérivée du claim pour un utilisateur cloisonné
    # (il ne peut créer que dans SON agence) ; requise pour une portée réseau. Voir service.
    primary_agency_id: uuid.UUID | None = None


class CreationIndividu(_CommunEntree):
    last_name: str = Field(min_length=1, max_length=100)
    first_name: str = Field(min_length=1, max_length=100)
    middle_names: str | None = Field(default=None, max_length=200)
    name_at_birth: str | None = Field(default=None, max_length=200)
    birth_date: date
    birth_place: str | None = Field(default=None, max_length=200)
    birth_country_id: uuid.UUID | None = None
    gender: Genre
    nationality_id: uuid.UUID
    secondary_nationality_id: uuid.UUID | None = None
    marital_status: EtatCivil | None = None
    dependents_count: int = Field(default=0, ge=0)
    profession: str | None = Field(default=None, max_length=200)
    monthly_income_estimate: Decimal | None = None
    is_literate: bool = True


class CreationPersonneMorale(_CommunEntree):
    legal_name: str = Field(min_length=1, max_length=300)
    commercial_name: str | None = Field(default=None, max_length=300)
    legal_form: FormeJuridique
    rccm_number: str | None = Field(default=None, max_length=50)
    nif_number: str | None = Field(default=None, max_length=50)
    constitution_date: date
    capital_amount: Decimal | None = None
    capital_currency_id: uuid.UUID | None = None
    business_purpose: str | None = None
    headquarters_country_id: uuid.UUID


class CreationGroupement(_CommunEntree):
    group_name: str = Field(min_length=1, max_length=300)
    group_type: TypeGroupement
    constitution_date: date
    intervention_zone: str | None = Field(default=None, max_length=200)
    group_purpose: str | None = None
    expected_member_count: int | None = Field(default=None, gt=0)


# --- sortie --------------------------------------------------------------------------------


class IndividuDetail(BaseModel):
    last_name: str
    first_name: str
    middle_names: str | None
    name_at_birth: str | None
    birth_date: date
    birth_place: str | None
    birth_country_id: uuid.UUID | None
    gender: str
    nationality_id: uuid.UUID
    secondary_nationality_id: uuid.UUID | None
    marital_status: str | None
    dependents_count: int
    profession: str | None
    monthly_income_estimate: Decimal | None
    is_literate: bool


class PersonneMoraleDetail(BaseModel):
    legal_name: str
    commercial_name: str | None
    legal_form: str
    rccm_number: str | None
    nif_number: str | None
    constitution_date: date
    capital_amount: Decimal | None
    capital_currency_id: uuid.UUID | None
    business_purpose: str | None
    headquarters_country_id: uuid.UUID


class GroupementDetail(BaseModel):
    group_name: str
    group_type: str
    constitution_date: date
    intervention_zone: str | None
    group_purpose: str | None
    expected_member_count: int | None


class CorpsTransition(BaseModel):
    """Corps OPTIONNEL d'une transition de cycle de vie : un motif libre (suspension,
    désactivation, décès…), tracé dans le lifecycle_event."""

    motif: str | None = Field(default=None, max_length=500)


# --- coordonnées (T2b) -----------------------------------------------------------------

TypeTelephone = Literal["mobile", "landline", "professional", "emergency", "spouse", "other"]
TypeAdresse = Literal["home", "work", "postal", "permanent", "temporary", "other"]
TypeEmail = Literal["personal", "professional", "other"]


class CreationTelephone(BaseModel):
    """`forcer` = enregistrer un numéro que la bibliothèque refuse (échappatoire tracée)."""

    phone: str = Field(min_length=1, max_length=50)
    contact_subtype: TypeTelephone | None = None
    is_primary: bool = False
    forcer: bool = False


class CreationEmail(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    contact_subtype: TypeEmail | None = None
    is_primary: bool = False


class CreationAdresse(BaseModel):
    address_line1: str | None = Field(default=None, max_length=300)
    address_line2: str | None = Field(default=None, max_length=300)
    quarter: str | None = Field(default=None, max_length=200)
    landmark: str | None = Field(default=None, max_length=300)
    city_id: uuid.UUID | None = None
    region_id: uuid.UUID | None = None
    country_id: uuid.UUID | None = None
    postal_code: str | None = Field(default=None, max_length=20)
    contact_subtype: TypeAdresse | None = None
    is_primary: bool = False

    @model_validator(mode="after")
    def _rue_ou_repere(self) -> "CreationAdresse":
        # Miroir du CHECK : une adresse est valide dès qu'elle a une rue OU un repère. Message
        # clair à la validation plutôt qu'un IntegrityError opaque. Jamais rejetée faute de rue.
        if not (self.address_line1 or self.landmark):
            raise ValueError("Renseignez au moins une rue ou un point de repère.")
        return self


class SuppressionContact(BaseModel):
    motif: str | None = Field(default=None, max_length=500)


class ContactItem(BaseModel):
    """Une coordonnée, telle que la fiche l'affiche. Construite champ par champ (règle projet)."""

    id: uuid.UUID
    contact_type: str
    contact_subtype: str | None
    is_primary: bool
    is_verified: bool
    # téléphone
    phone_number: str | None
    phone_raw: str | None
    phone_country_code: str | None
    phone_normalized: bool
    # email
    email_address: str | None
    # adresse
    address_line1: str | None
    address_line2: str | None
    quarter: str | None
    landmark: str | None
    city_id: uuid.UUID | None
    region_id: uuid.UUID | None
    country_id: uuid.UUID | None
    postal_code: str | None


class FicheTier(BaseModel):
    """Fiche COMPLÈTE. Un seul des trois détails est peuplé, selon le type.

    Servie derrière tiers.read uniquement : c'est le seul type qui porte les blocs KYC/socio-
    éco. Un porteur de read.basic ne l'obtient jamais — la route ne la construit pas pour lui.
    """

    id: uuid.UUID
    tier_number: str
    tier_type: str
    status: str
    primary_agency_id: uuid.UUID
    primary_phone: str | None
    language_preference: str | None
    created_at: datetime
    updated_at: datetime
    individu: IndividuDetail | None = None
    personne_morale: PersonneMoraleDetail | None = None
    groupement: GroupementDetail | None = None


class TierResume(BaseModel):
    """Vue RÉSUMÉE (read.basic) — identification au guichet, RIEN de sensible.

    Ce type ne DÉCLARE aucun champ KYC, PPE, revenu ou nationalité : il ne peut physiquement
    pas les porter. Le service qui l'alimente ne SELECT même pas ces colonnes. Sûr par
    construction : il n'y a rien à faire fuiter puisque rien n'est chargé.
    """

    id: uuid.UUID
    tier_number: str
    tier_type: str
    display_name: str
    status: str
    primary_agency_id: uuid.UUID


class PageTiers(BaseModel):
    """Liste paginée de résumés. Le total suit le filtre de cloisonnement (pas de fuite)."""

    lignes: list[TierResume]
    total: int
    page: int
    taille: int


class EvenementTimeline(BaseModel):
    """Un événement de la frise. event_type est un CODE brut, traduit côté front (comme l'audit)."""

    occurred_at: datetime
    event_type: str
    previous_status: str | None
    new_status: str | None
    reason: str | None
    auteur_nom: str | None
