"""Routes des personnes morales et groupements : personnes liées et modification du profil.

  lire les personnes liées             -> tiers.read
  ajouter / modifier / retirer / PATCH -> tiers.update
  fiche inconnue ou hors périmètre     -> 404 (jamais 403) ; lien d'une AUTRE fiche -> 404
  règle métier violée                  -> 422 avec le message de l'écran
"""

import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.security.autorisation import UtilisateurCourant, exige
from app.modules.security.router import _contexte
from app.modules.tiers import personnes_liees as liens
from app.modules.tiers import profils
from app.modules.tiers.consultation import dernier_risque, telephone_principal
from app.modules.tiers.router import MESSAGE_INTROUVABLE, _vers_fiche
from app.modules.tiers.schemas import (
    CreationLien,
    FicheTier,
    LienItem,
    ModificationGroupement,
    ModificationLien,
    ModificationPersonneMorale,
    OrigineRisque,
    PersonnesLiees,
    RisqueHerite,
    SuppressionLien,
)

router = APIRouter(prefix="/tiers", tags=["tiers"])

MESSAGE_PERSONNE_INTROUVABLE = "Personne introuvable."
MESSAGE_LIEN_INTROUVABLE = "Lien introuvable."
MESSAGE_TYPE_PM = "Cette fiche n'est pas une personne morale."
MESSAGE_TYPE_GROUPEMENT = "Cette fiche n'est pas un groupement."
MESSAGE_REFERENCE = "Une référence fournie (pays ou devise) est invalide."


def _non_trouve(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)


def _refus(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=message)


def _traduire_lien(erreur: Exception) -> HTTPException:
    if isinstance(erreur, liens.TierIntrouvableError):
        return _non_trouve(MESSAGE_INTROUVABLE)
    if isinstance(erreur, liens.PersonneIntrouvableError):
        return _non_trouve(MESSAGE_PERSONNE_INTROUVABLE)
    if isinstance(erreur, liens.LienIntrouvableError):
        return _non_trouve(MESSAGE_LIEN_INTROUVABLE)
    if isinstance(erreur, liens.LienInvalideError):
        return _refus(str(erreur))
    raise erreur


def _vers_item(ligne: liens.LigneLien, seuil: Decimal | None) -> LienItem:
    lien = ligne.lien
    atteint = (
        seuil is not None
        and lien.role == "beneficiaire_effectif"
        and lien.pourcentage_detention is not None
        and lien.pourcentage_detention >= seuil
    )
    return LienItem(
        id=lien.id,
        individual_tier_id=lien.individual_tier_id,
        nom=ligne.nom,
        tier_number=ligne.numero,
        statut=ligne.statut,
        niveau_risque=ligne.niveau_risque,
        role=lien.role,
        fonction=lien.fonction,
        pourcentage_detention=lien.pourcentage_detention,
        pouvoir_signature=lien.pouvoir_signature,
        date_debut=lien.date_debut,
        date_fin=lien.date_fin,
        en_cours=ligne.en_cours,
        atteint_le_seuil=bool(atteint),
    )


def _vue(db: Session, courant: UtilisateurCourant, tier_id: uuid.UUID) -> PersonnesLiees:
    vue = liens.lister_liens(db, courant, tier_id)
    origine = vue.origine
    return PersonnesLiees(
        items=[_vers_item(ligne, vue.seuil) for ligne in vue.lignes],
        journee_reference=vue.journee_reference,
        total_beneficiaires=vue.total_beneficiaires,
        seuil=vue.seuil,
        seuil_provisoire=vue.seuil_provisoire,
        risque=RisqueHerite(
            niveau=vue.niveau_herite,
            explication=vue.explication_risque,
            origine=(
                OrigineRisque(
                    nom=origine["nom"],
                    numero=origine["numero"],
                    role=origine["role"],
                    niveau=origine["niveau"],
                )
                if origine
                else None
            ),
        ),
    )


@router.get("/{tier_id}/personnes-liees", response_model=PersonnesLiees)
def lister_personnes_liees(
    tier_id: uuid.UUID,
    courant: Annotated[UtilisateurCourant, Depends(exige("tiers.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> PersonnesLiees:
    """Personnes liées d'une PM ou d'un groupement, avec le risque hérité et le seuil LBC/FT."""
    try:
        return _vue(db, courant, tier_id)
    except Exception as erreur:
        raise _traduire_lien(erreur) from None


@router.post(
    "/{tier_id}/personnes-liees", response_model=PersonnesLiees, status_code=status.HTTP_201_CREATED
)
def ajouter_personne_liee(
    tier_id: uuid.UUID,
    corps: CreationLien,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("tiers.update"))],
    db: Annotated[Session, Depends(get_db)],
) -> PersonnesLiees:
    try:
        liens.ajouter_lien(
            db, courant, tier_id, liens.DonneesLien(**corps.model_dump()), _contexte(request)
        )
        return _vue(db, courant, tier_id)
    except Exception as erreur:
        raise _traduire_lien(erreur) from None


@router.patch("/{tier_id}/personnes-liees/{lien_id}", response_model=PersonnesLiees)
def modifier_personne_liee(
    tier_id: uuid.UUID,
    lien_id: uuid.UUID,
    corps: ModificationLien,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("tiers.update"))],
    db: Annotated[Session, Depends(get_db)],
) -> PersonnesLiees:
    try:
        liens.modifier_lien(
            db,
            courant,
            tier_id,
            lien_id,
            corps.model_dump(exclude_unset=True),
            _contexte(request),
        )
        return _vue(db, courant, tier_id)
    except Exception as erreur:
        raise _traduire_lien(erreur) from None


@router.delete("/{tier_id}/personnes-liees/{lien_id}", response_model=PersonnesLiees)
def retirer_personne_liee(
    tier_id: uuid.UUID,
    lien_id: uuid.UUID,
    corps: SuppressionLien,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("tiers.update"))],
    db: Annotated[Session, Depends(get_db)],
) -> PersonnesLiees:
    """Suppression LOGIQUE : le motif est obligatoire et conservé."""
    try:
        liens.retirer_lien(db, courant, tier_id, lien_id, corps.motif, _contexte(request))
        return _vue(db, courant, tier_id)
    except Exception as erreur:
        raise _traduire_lien(erreur) from None


# --- modification du profil ------------------------------------------------------------


def _traduire_profil(erreur: Exception, message_type: str) -> HTTPException:
    if isinstance(erreur, profils.TierIntrouvableError):
        return _non_trouve(MESSAGE_INTROUVABLE)
    if isinstance(erreur, profils.TypeFicheError):
        return _refus(message_type)
    if isinstance(erreur, profils.IdentifiantDejaUtiliseError):
        return _refus(str(erreur))
    if isinstance(erreur, profils.ReferenceInvalideError):
        return _refus(MESSAGE_REFERENCE)
    raise erreur


@router.patch("/{tier_id}/legal-entity", response_model=FicheTier)
def modifier_personne_morale(
    tier_id: uuid.UUID,
    corps: ModificationPersonneMorale,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("tiers.update"))],
    db: Annotated[Session, Depends(get_db)],
) -> FicheTier:
    """Modifie le profil d'une personne morale (champs envoyés uniquement), avec audit."""
    try:
        tier = profils.modifier_profil(
            db, courant, tier_id, "legal_entity", corps.model_dump(exclude_unset=True),
            _contexte(request),
        )
    except Exception as erreur:
        raise _traduire_profil(erreur, MESSAGE_TYPE_PM) from None
    return _vers_fiche(tier, telephone_principal(db, tier_id), dernier_risque(db, tier_id))


@router.patch("/{tier_id}/group", response_model=FicheTier)
def modifier_groupement(
    tier_id: uuid.UUID,
    corps: ModificationGroupement,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("tiers.update"))],
    db: Annotated[Session, Depends(get_db)],
) -> FicheTier:
    """Modifie le profil d'un groupement (champs envoyés uniquement), avec audit."""
    try:
        tier = profils.modifier_profil(
            db, courant, tier_id, "group", corps.model_dump(exclude_unset=True),
            _contexte(request),
        )
    except Exception as erreur:
        raise _traduire_profil(erreur, MESSAGE_TYPE_GROUPEMENT) from None
    return _vers_fiche(tier, telephone_principal(db, tier_id), dernier_risque(db, tier_id))
