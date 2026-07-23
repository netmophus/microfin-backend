"""Endpoints HTTP du module Tiers — création des trois types (T1c).

Chaque route exige `tiers.create` : 401 sans jeton, 403 avec un jeton dépourvu de la
permission (rendu par exige()). Le cloisonnement fin, lui, n'est pas un code d'erreur mais
une règle du service : un cloisonné ne crée que dans SON agence (§3 du service).

CONVERSION EXPLICITE. _vers_fiche construit la sortie champ par champ selon le sous-type —
aucun from_attributes. Ce qui n'est pas écrit ici ne sort pas.

TABLE DES ERREURS (un seul endroit) :
  - permission absente        -> 403 (exige(), en amont)
  - agence forcée hors périm. -> 422 : requête recevable, valeur invalide
  - portée réseau sans agence -> 422 : « toutes les agences » n'est pas un rattachement
  - référence FK invalide     -> 422 : agence, pays ou devise inexistant
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.security.autorisation import UtilisateurCourant, exige
from app.modules.security.router import _contexte
from app.modules.tiers.models import GroupProfile, IndividualProfile, LegalEntityProfile, Tier
from app.modules.tiers.schemas import (
    CreationGroupement,
    CreationIndividu,
    CreationPersonneMorale,
    FicheTier,
    GroupementDetail,
    IndividuDetail,
    PersonneMoraleDetail,
)
from app.modules.tiers.service import (
    AgenceHorsPerimetreError,
    AgenceRequiseError,
    creer_groupement,
    creer_individu,
    creer_personne_morale,
)

router = APIRouter(prefix="/tiers", tags=["tiers"])


def _vers_fiche(tier: Tier) -> FicheTier:
    fiche = FicheTier(
        id=tier.id,
        tier_number=tier.tier_number,
        tier_type=tier.tier_type,
        status=tier.status,
        primary_agency_id=tier.primary_agency_id,
        primary_phone=tier.primary_phone,
        language_preference=tier.language_preference,
        created_at=tier.created_at,
        updated_at=tier.updated_at,
    )
    if isinstance(tier, IndividualProfile):
        fiche.individu = IndividuDetail(
            last_name=tier.last_name,
            first_name=tier.first_name,
            middle_names=tier.middle_names,
            name_at_birth=tier.name_at_birth,
            birth_date=tier.birth_date,
            birth_place=tier.birth_place,
            birth_country_id=tier.birth_country_id,
            gender=tier.gender,
            nationality_id=tier.nationality_id,
            secondary_nationality_id=tier.secondary_nationality_id,
            marital_status=tier.marital_status,
            dependents_count=tier.dependents_count,
            profession=tier.profession,
            monthly_income_estimate=tier.monthly_income_estimate,
            is_literate=tier.is_literate,
        )
    elif isinstance(tier, LegalEntityProfile):
        fiche.personne_morale = PersonneMoraleDetail(
            legal_name=tier.legal_name,
            commercial_name=tier.commercial_name,
            legal_form=tier.legal_form,
            rccm_number=tier.rccm_number,
            nif_number=tier.nif_number,
            constitution_date=tier.constitution_date,
            capital_amount=tier.capital_amount,
            capital_currency_id=tier.capital_currency_id,
            business_purpose=tier.business_purpose,
            headquarters_country_id=tier.headquarters_country_id,
        )
    elif isinstance(tier, GroupProfile):
        fiche.groupement = GroupementDetail(
            group_name=tier.group_name,
            group_type=tier.group_type,
            constitution_date=tier.constitution_date,
            intervention_zone=tier.intervention_zone,
            group_purpose=tier.group_purpose,
            expected_member_count=tier.expected_member_count,
        )
    return fiche


def _traduire(erreur: Exception) -> HTTPException:
    """Traduit une erreur du service en réponse HTTP. Un seul endroit pour cette table."""
    if isinstance(erreur, AgenceHorsPerimetreError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="L'agence choisie n'est pas dans votre périmètre.",
        )
    if isinstance(erreur, AgenceRequiseError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Précisez l'agence de rattachement de la fiche.",
        )
    raise erreur


def _sur_integrite(db: Session, erreur: IntegrityError) -> HTTPException:
    """Une violation de FK à la création = une référence fournie n'existe pas."""
    db.rollback()
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail="Une référence fournie (agence, pays ou devise) est invalide.",
    )


@router.post("/individuals", response_model=FicheTier, status_code=status.HTTP_201_CREATED)
def creer_individu_endpoint(
    corps: CreationIndividu,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("tiers.create"))],
    db: Annotated[Session, Depends(get_db)],
) -> FicheTier:
    """Crée une personne physique en statut 'prospect'."""
    try:
        tier = creer_individu(db, courant, corps, _contexte(request))
    except IntegrityError as erreur:
        raise _sur_integrite(db, erreur) from erreur
    except Exception as erreur:
        raise _traduire(erreur) from None
    return _vers_fiche(tier)


@router.post("/legal-entities", response_model=FicheTier, status_code=status.HTTP_201_CREATED)
def creer_personne_morale_endpoint(
    corps: CreationPersonneMorale,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("tiers.create"))],
    db: Annotated[Session, Depends(get_db)],
) -> FicheTier:
    """Crée une personne morale en statut 'prospect'."""
    try:
        tier = creer_personne_morale(db, courant, corps, _contexte(request))
    except IntegrityError as erreur:
        raise _sur_integrite(db, erreur) from erreur
    except Exception as erreur:
        raise _traduire(erreur) from None
    return _vers_fiche(tier)


@router.post("/groups", response_model=FicheTier, status_code=status.HTTP_201_CREATED)
def creer_groupement_endpoint(
    corps: CreationGroupement,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("tiers.create"))],
    db: Annotated[Session, Depends(get_db)],
) -> FicheTier:
    """Crée un groupement solidaire en statut 'prospect'."""
    try:
        tier = creer_groupement(db, courant, corps, _contexte(request))
    except IntegrityError as erreur:
        raise _sur_integrite(db, erreur) from erreur
    except Exception as erreur:
        raise _traduire(erreur) from None
    return _vers_fiche(tier)
