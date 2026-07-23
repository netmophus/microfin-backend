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

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.security.autorisation import UtilisateurCourant, exige
from app.modules.security.router import _contexte
from app.modules.tiers.consultation import (
    TAILLE_PAGE_DEFAUT,
    TAILLE_PAGE_MAX,
    FiltresTiers,
    lire_complet,
    lire_resume,
    lister,
    telephone_principal,
    timeline,
)
from app.modules.tiers.contacts import (
    ContactIntrouvableError,
    DonneesAdresse,
    TierIntrouvableError,
    ajouter_adresse,
    ajouter_email,
    ajouter_telephone,
    definir_principal,
    lister_contacts,
    supprimer_contact,
)
from app.modules.tiers.cycle_de_vie import (
    ActivationImpossibleError,
    CibleIntrouvableError,
    TransitionIllegaleError,
    TypeIncompatibleError,
    activer,
    executer_transition,
    message_transition_illegale,
)
from app.modules.tiers.models import (
    Contact,
    GroupProfile,
    IdentityDocument,
    IndividualProfile,
    LegalEntityProfile,
    Tier,
)
from app.modules.tiers.pieces import (
    DonneesPiece,
    DoublonPieceError,
    PieceIntrouvableError,
    SuppressionPrincipaleError,
    ajouter_piece,
    definir_principale,
    etat_validite,
    lister_pieces,
    supprimer_piece,
    verifier_piece,
)
from app.modules.tiers.schemas import (
    ContactItem,
    CorpsTransition,
    CreationAdresse,
    CreationEmail,
    CreationGroupement,
    CreationIndividu,
    CreationPersonneMorale,
    CreationPiece,
    CreationTelephone,
    EvenementTimeline,
    FicheTier,
    GroupementDetail,
    IndividuDetail,
    PageTiers,
    PersonneMoraleDetail,
    PieceItem,
    SuppressionContact,
    SuppressionPiece,
    TierResume,
    VerificationPiece,
)
from app.modules.tiers.service import (
    AgenceHorsPerimetreError,
    AgenceRequiseError,
    creer_groupement,
    creer_individu,
    creer_personne_morale,
)
from app.modules.tiers.telephone import TelephoneInvalideError

router = APIRouter(prefix="/tiers", tags=["tiers"])

MESSAGE_INTROUVABLE = "Fiche tiers introuvable."
# La permission qui débloque la fiche COMPLÈTE. Sa présence, et RIEN d'autre (aucun paramètre
# de requête), détermine le niveau de détail servi.
PERMISSION_FICHE_COMPLETE = "tiers.read"


def _vers_fiche(tier: Tier, primary_phone: str | None = None) -> FicheTier:
    # primary_phone est fourni par l'appelant (LU DEPUIS LES CONTACTS, T2b) sur la lecture de fiche.
    # Sur les réponses de création/transition, il n'est pas recalculé : la colonne legacy sert de
    # repli (nul pour une fiche neuve — le numéro vit dans les contacts, lu via GET /contacts).
    fiche = FicheTier(
        id=tier.id,
        tier_number=tier.tier_number,
        tier_type=tier.tier_type,
        status=tier.status,
        primary_agency_id=tier.primary_agency_id,
        primary_phone=primary_phone if primary_phone is not None else tier.primary_phone,
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


# --- lecture (T1d) ---------------------------------------------------------------------


@router.get("", response_model=PageTiers)
def lister_tiers(
    courant: Annotated[UtilisateurCourant, Depends(exige("tiers.read.basic"))],
    db: Annotated[Session, Depends(get_db)],
    q: Annotated[str | None, Query(description="Recherche — numéro ou nom.")] = None,
    tier_type: Annotated[
        str | None, Query(description="individual | legal_entity | group.")
    ] = None,
    statut: Annotated[str | None, Query(description="Filtre sur le statut.")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    taille: Annotated[int, Query(ge=1, le=TAILLE_PAGE_MAX)] = TAILLE_PAGE_DEFAUT,
) -> PageTiers:
    """Liste de RÉSUMÉS, cloisonnée par agence. Accessible dès tiers.read.basic."""
    return lister(
        db,
        courant,
        FiltresTiers(q=q, tier_type=tier_type, status=statut),
        page=page,
        taille=taille,
    )


@router.get("/{tier_id}", response_model=None)
def lire_tier(
    tier_id: uuid.UUID,
    courant: Annotated[UtilisateurCourant, Depends(exige("tiers.read.basic"))],
    db: Annotated[Session, Depends(get_db)],
) -> FicheTier | TierResume:
    """Fiche d'un tiers — UNE route, un seul point de décision.

    Le niveau de détail est déterminé UNIQUEMENT par la permission de l'appelant, JAMAIS par
    ce qu'il demande : aucun paramètre de requête ne l'influence. Un porteur de tiers.read
    reçoit la fiche complète ; sinon (read.basic seul, ex. le caissier) il reçoit le résumé,
    dont les champs sensibles ne sont même pas chargés en base.

    Hors périmètre -> 404 (n'existe pas de mon point de vue), jamais 403.
    """
    if PERMISSION_FICHE_COMPLETE in courant.permissions:
        tier = lire_complet(db, courant, tier_id)
        if tier is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_INTROUVABLE)
        return _vers_fiche(tier, telephone_principal(db, tier_id))

    resume = lire_resume(db, courant, tier_id)
    if resume is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_INTROUVABLE)
    return resume


@router.get("/{tier_id}/timeline", response_model=list[EvenementTimeline])
def timeline_tier(
    tier_id: uuid.UUID,
    courant: Annotated[UtilisateurCourant, Depends(exige("tiers.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[EvenementTimeline]:
    """Frise chronologique d'une fiche (détail -> tiers.read). Hors périmètre -> 404."""
    evenements = timeline(db, courant, tier_id)
    if evenements is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_INTROUVABLE)
    return evenements


# --- cycle de vie (T1e) ----------------------------------------------------------------
#
# TABLE DES ERREURS, un seul endroit :
#   hors périmètre / inexistant  -> 404 (jamais 403 : « n'existe pas de mon point de vue »)
#   transition illégale (statut)  -> 409, en nommant le statut courant
#   type incompatible (D4)        -> 409, message dédié (décès sur une PM, etc.)
#   activation impossible (stub)  -> 412, avec TOUTES les conditions manquantes


def _traduire_cycle(erreur: Exception) -> HTTPException:
    if isinstance(erreur, CibleIntrouvableError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_INTROUVABLE)
    if isinstance(erreur, TransitionIllegaleError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=message_transition_illegale(erreur.statut),
        )
    if isinstance(erreur, TypeIncompatibleError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=erreur.message)
    if isinstance(erreur, ActivationImpossibleError):
        return HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail={
                "message": "L'activation de la fiche requiert des conditions non remplies.",
                "conditions_manquantes": [
                    {"code": c.code, "libelle": c.libelle} for c in erreur.conditions
                ],
            },
        )
    raise erreur


def _transition(
    nom: str,
    tier_id: uuid.UUID,
    corps: CorpsTransition | None,
    request: Request,
    courant: UtilisateurCourant,
    db: Session,
) -> FicheTier:
    try:
        tier = executer_transition(
            db, courant, tier_id, nom, _contexte(request), motif=corps.motif if corps else None
        )
    except Exception as erreur:
        raise _traduire_cycle(erreur) from None
    return _vers_fiche(tier)


@router.post("/{tier_id}/suspend", response_model=FicheTier)
def suspendre(
    tier_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("tiers.suspend"))],
    db: Annotated[Session, Depends(get_db)],
    corps: Annotated[CorpsTransition | None, Body()] = None,
) -> FicheTier:
    """Suspend une fiche active (pièce à régulariser, absence…). Réversible."""
    return _transition("suspend", tier_id, corps, request, courant, db)


@router.post("/{tier_id}/reactivate", response_model=FicheTier)
def reactiver(
    tier_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("tiers.suspend"))],
    db: Annotated[Session, Depends(get_db)],
    corps: Annotated[CorpsTransition | None, Body()] = None,
) -> FicheTier:
    """Lève une suspension temporaire : la fiche redevient active."""
    return _transition("reactivate", tier_id, corps, request, courant, db)


@router.post("/{tier_id}/mark-deceased", response_model=FicheTier)
def marquer_decede(
    tier_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("tiers.suspend"))],
    db: Annotated[Session, Depends(get_db)],
    corps: Annotated[CorpsTransition | None, Body()] = None,
) -> FicheTier:
    """Enregistre le décès (personne physique uniquement). La fiche RESTE visible (succession)."""
    return _transition("mark_deceased", tier_id, corps, request, courant, db)


@router.post("/{tier_id}/mark-dissolved", response_model=FicheTier)
def marquer_dissous(
    tier_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("tiers.suspend"))],
    db: Annotated[Session, Depends(get_db)],
    corps: Annotated[CorpsTransition | None, Body()] = None,
) -> FicheTier:
    """Enregistre la dissolution (personne morale ou groupement). La fiche RESTE visible."""
    return _transition("mark_dissolved", tier_id, corps, request, courant, db)


@router.post("/{tier_id}/deactivate", response_model=FicheTier)
def desactiver(
    tier_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("tiers.deactivate"))],
    db: Annotated[Session, Depends(get_db)],
    corps: Annotated[CorpsTransition | None, Body()] = None,
) -> FicheTier:
    """Désactive une fiche (SOFT DELETE) : elle sort de l'annuaire. Réservé au responsable."""
    return _transition("deactivate", tier_id, corps, request, courant, db)


@router.post("/{tier_id}/activate", response_model=FicheTier)
def activer_endpoint(
    tier_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("tiers.validate"))],
    db: Annotated[Session, Depends(get_db)],
) -> FicheTier:
    """Active une fiche prospect après validation KYC. STUB : renvoie 412 avec les conditions
    manquantes tant que le module KYC (T3) n'est pas là."""
    try:
        tier = activer(db, courant, tier_id, _contexte(request))
    except Exception as erreur:
        raise _traduire_cycle(erreur) from None
    return _vers_fiche(tier)


# --- coordonnées (T2b) -----------------------------------------------------------------
#
#   voir les coordonnées   -> tiers.read
#   les gérer (ajout/suppr/principal, y compris FORCER un numéro) -> tiers.update
#   hors périmètre / inconnu -> 404 (jamais 403)
#   téléphone refusé        -> 422 { message, forcable } ; forcable guide l'écran


def _vers_contact(contact: Contact) -> ContactItem:
    return ContactItem(
        id=contact.id,
        contact_type=contact.contact_type,
        contact_subtype=contact.contact_subtype,
        is_primary=contact.is_primary,
        is_verified=contact.is_verified,
        phone_number=contact.phone_number,
        phone_raw=contact.phone_raw,
        phone_country_code=contact.phone_country_code,
        phone_normalized=contact.phone_normalized,
        email_address=contact.email_address,
        address_line1=contact.address_line1,
        address_line2=contact.address_line2,
        quarter=contact.quarter,
        landmark=contact.landmark,
        city_id=contact.city_id,
        region_id=contact.region_id,
        country_id=contact.country_id,
        postal_code=contact.postal_code,
    )


def _traduire_contact(erreur: Exception) -> HTTPException:
    if isinstance(erreur, TierIntrouvableError | ContactIntrouvableError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_INTROUVABLE)
    if isinstance(erreur, TelephoneInvalideError):
        # forcable dit à l'écran s'il faut proposer « enregistrer quand même » (numéro de bonne
        # longueur, juste non reconnu) ou seulement « corriger » (charabia).
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": "Ce numéro ne semble pas valide. Vérifiez la saisie.",
                "forcable": erreur.forcable,
            },
        )
    raise erreur


@router.post("/{tier_id}/phones", response_model=ContactItem, status_code=status.HTTP_201_CREATED)
def ajouter_telephone_endpoint(
    tier_id: uuid.UUID,
    corps: CreationTelephone,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("tiers.update"))],
    db: Annotated[Session, Depends(get_db)],
) -> ContactItem:
    """Ajoute un téléphone normalisé. `forcer=true` enregistre au mieux un numéro refusé (tracé)."""
    try:
        contact = ajouter_telephone(
            db,
            courant,
            tier_id,
            phone=corps.phone,
            contact_subtype=corps.contact_subtype,
            is_primary=corps.is_primary,
            forcer=corps.forcer,
            contexte=_contexte(request),
        )
    except Exception as erreur:
        raise _traduire_contact(erreur) from None
    return _vers_contact(contact)


@router.post("/{tier_id}/emails", response_model=ContactItem, status_code=status.HTTP_201_CREATED)
def ajouter_email_endpoint(
    tier_id: uuid.UUID,
    corps: CreationEmail,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("tiers.update"))],
    db: Annotated[Session, Depends(get_db)],
) -> ContactItem:
    try:
        contact = ajouter_email(
            db,
            courant,
            tier_id,
            email=corps.email,
            contact_subtype=corps.contact_subtype,
            is_primary=corps.is_primary,
            contexte=_contexte(request),
        )
    except Exception as erreur:
        raise _traduire_contact(erreur) from None
    return _vers_contact(contact)


@router.post(
    "/{tier_id}/addresses", response_model=ContactItem, status_code=status.HTTP_201_CREATED
)
def ajouter_adresse_endpoint(
    tier_id: uuid.UUID,
    corps: CreationAdresse,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("tiers.update"))],
    db: Annotated[Session, Depends(get_db)],
) -> ContactItem:
    """Ajoute une adresse. Une rue OU un point de repère suffit (validé par le schéma)."""
    try:
        contact = ajouter_adresse(
            db,
            courant,
            tier_id,
            donnees=DonneesAdresse(
                address_line1=corps.address_line1,
                address_line2=corps.address_line2,
                quarter=corps.quarter,
                landmark=corps.landmark,
                city_id=corps.city_id,
                region_id=corps.region_id,
                country_id=corps.country_id,
                postal_code=corps.postal_code,
            ),
            contact_subtype=corps.contact_subtype,
            is_primary=corps.is_primary,
            contexte=_contexte(request),
        )
    except Exception as erreur:
        raise _traduire_contact(erreur) from None
    return _vers_contact(contact)


@router.get("/{tier_id}/contacts", response_model=list[ContactItem])
def lister_contacts_endpoint(
    tier_id: uuid.UUID,
    courant: Annotated[UtilisateurCourant, Depends(exige("tiers.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[ContactItem]:
    """Coordonnées d'un tiers. Hors périmètre -> 404."""
    contacts = lister_contacts(db, courant, tier_id)
    if contacts is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_INTROUVABLE)
    return [_vers_contact(c) for c in contacts]


@router.post("/{tier_id}/contacts/{contact_id}/set-primary", response_model=ContactItem)
def definir_principal_endpoint(
    tier_id: uuid.UUID,
    contact_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("tiers.update"))],
    db: Annotated[Session, Depends(get_db)],
) -> ContactItem:
    """Désigne une coordonnée comme principale ; l'ancienne du même type est débasculée."""
    try:
        contact = definir_principal(db, courant, tier_id, contact_id, _contexte(request))
    except Exception as erreur:
        raise _traduire_contact(erreur) from None
    return _vers_contact(contact)


@router.delete("/{tier_id}/contacts/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
def supprimer_contact_endpoint(
    tier_id: uuid.UUID,
    contact_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("tiers.update"))],
    db: Annotated[Session, Depends(get_db)],
    corps: Annotated[SuppressionContact | None, Body()] = None,
) -> None:
    """Suppression LOGIQUE avec motif : la coordonnée sort des listes, jamais effacée."""
    try:
        supprimer_contact(
            db, courant, tier_id, contact_id, corps.motif if corps else None, _contexte(request)
        )
    except Exception as erreur:
        raise _traduire_contact(erreur) from None


# --- pièces d'identité (T2c) -----------------------------------------------------------
#
#   voir / saisir / désigner principale / supprimer -> tiers.update (saisie) ou tiers.read (voir)
#   VÉRIFIER (acte de contrôle) -> tiers.identity.verify (responsable d'agence + LBC/FT)
#   doublon d'un numéro unique  -> 422 : fiche nommée SI dans le périmètre, générique sinon
#   supprimer la principale s'il en reste d'autres -> 409


def _vers_piece(piece: IdentityDocument) -> PieceItem:
    return PieceItem(
        id=piece.id,
        document_type_id=piece.document_type_id,
        document_number=piece.document_number,
        issuing_country_id=piece.issuing_country_id,
        issuing_authority=piece.issuing_authority,
        date_of_issue=piece.date_of_issue,
        expiry_date=piece.expiry_date,
        validite=etat_validite(piece.expiry_date),  # calculée à la lecture, jamais stockée
        is_primary=piece.is_primary,
        is_verified=piece.is_verified,
        verified_at=piece.verified_at,
        verification_notes=piece.verification_notes,
        notes=piece.notes,
    )


def _traduire_piece(erreur: Exception) -> HTTPException:
    if isinstance(erreur, TierIntrouvableError | PieceIntrouvableError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_INTROUVABLE)
    if isinstance(erreur, DoublonPieceError):
        if erreur.dans_perimetre:
            # L'agent a déjà le droit de voir cette fiche : la nommer l'aide à résoudre au guichet.
            detail = (
                f"Cette pièce est déjà enregistrée sur la fiche "
                f"{erreur.tier_number} ({erreur.nom})."
            )
        else:
            # Hors périmètre : refus strictement générique (comme le 404), aucune divulgation.
            detail = "Ce numéro de pièce est déjà utilisé."
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)
    if isinstance(erreur, SuppressionPrincipaleError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Désignez d'abord une autre pièce principale avant de supprimer celle-ci.",
        )
    raise erreur


@router.post(
    "/{tier_id}/identity-documents",
    response_model=PieceItem,
    status_code=status.HTTP_201_CREATED,
)
def ajouter_piece_endpoint(
    tier_id: uuid.UUID,
    corps: CreationPiece,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("tiers.update"))],
    db: Annotated[Session, Depends(get_db)],
) -> PieceItem:
    """Saisit une pièce. Une pièce périmée passe (l'agent constate) ; un numéro unique déjà pris
    est refusé (422)."""
    try:
        piece = ajouter_piece(
            db,
            courant,
            tier_id,
            donnees=DonneesPiece(
                document_type_id=corps.document_type_id,
                document_number=corps.document_number,
                issuing_country_id=corps.issuing_country_id,
                issuing_authority=corps.issuing_authority,
                date_of_issue=corps.date_of_issue,
                expiry_date=corps.expiry_date,
                notes=corps.notes,
            ),
            is_primary=corps.is_primary,
            contexte=_contexte(request),
        )
    except Exception as erreur:
        raise _traduire_piece(erreur) from None
    return _vers_piece(piece)


@router.get("/{tier_id}/identity-documents", response_model=list[PieceItem])
def lister_pieces_endpoint(
    tier_id: uuid.UUID,
    courant: Annotated[UtilisateurCourant, Depends(exige("tiers.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[PieceItem]:
    """Pièces d'un tiers, chacune avec son état de validité calculé. Hors périmètre -> 404."""
    pieces = lister_pieces(db, courant, tier_id)
    if pieces is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_INTROUVABLE)
    return [_vers_piece(p) for p in pieces]


@router.post("/{tier_id}/identity-documents/{piece_id}/set-primary", response_model=PieceItem)
def definir_piece_principale_endpoint(
    tier_id: uuid.UUID,
    piece_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("tiers.update"))],
    db: Annotated[Session, Depends(get_db)],
) -> PieceItem:
    try:
        piece = definir_principale(db, courant, tier_id, piece_id, _contexte(request))
    except Exception as erreur:
        raise _traduire_piece(erreur) from None
    return _vers_piece(piece)


@router.post("/{tier_id}/identity-documents/{piece_id}/verify", response_model=PieceItem)
def verifier_piece_endpoint(
    tier_id: uuid.UUID,
    piece_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("tiers.identity.verify"))],
    db: Annotated[Session, Depends(get_db)],
    corps: Annotated[VerificationPiece | None, Body()] = None,
) -> PieceItem:
    """Atteste qu'une pièce a été vue et validée — acte de contrôle réservé (pas la saisie)."""
    try:
        piece = verifier_piece(
            db, courant, tier_id, piece_id, corps.notes if corps else None, _contexte(request)
        )
    except Exception as erreur:
        raise _traduire_piece(erreur) from None
    return _vers_piece(piece)


@router.delete(
    "/{tier_id}/identity-documents/{piece_id}", status_code=status.HTTP_204_NO_CONTENT
)
def supprimer_piece_endpoint(
    tier_id: uuid.UUID,
    piece_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("tiers.update"))],
    db: Annotated[Session, Depends(get_db)],
    corps: Annotated[SuppressionPiece | None, Body()] = None,
) -> None:
    """Suppression LOGIQUE avec motif. Refuse de retirer la principale s'il en reste d'autres."""
    try:
        supprimer_piece(
            db, courant, tier_id, piece_id, corps.motif if corps else None, _contexte(request)
        )
    except Exception as erreur:
        raise _traduire_piece(erreur) from None
