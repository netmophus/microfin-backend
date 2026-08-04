"""Endpoints HTTP du module Crédit — CR1 : produits (lecture), demande, décision.
CR3 : décaissement (pièce comptable + échéancier persisté) et lecture de l'échéancier.
CR4 : remboursement (une échéance à la fois, montant exact).

Permissions (exige) : lecture produits -> credit.product.read ; créer une demande ->
credit.demande.create ; lire -> credit.demande.read ; décider -> credit.demande.decide ;
décaisser -> credit.decaissement.create (séparée de decide) ; rembourser ->
credit.remboursement.create (voir seed_security).

TABLE DES ERREURS (un seul endroit) :
  - permission absente                    -> 403 (exige(), en amont)
  - tiers / demande hors périmètre ou inexistant -> 404
  - tiers non actif (gate KYC, création, approbation ou décaissement) -> 422
  - produit inexistant/inactif             -> 422
  - demande déjà décidée / montant décidé invalide -> 422
  - demande non approuvée, rattachement comptable manquant, échéancier impossible -> 422
  - aucune échéance à régler (non décaissé ou déjà soldé), montant incorrect -> 422
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.comptabilite.models import Account
from app.modules.credit import consultation
from app.modules.credit.decaissement import (
    DemandeNonApprouveeError,
    RattachementManquantError,
    decaisser,
)
from app.modules.credit.demandes import (
    DemandeDejaDecideeError,
    MontantDecideInvalideError,
    ProduitIntrouvableError,
    TierNonActifError,
    creer_demande,
    decider,
)
from app.modules.credit.echeancier import EcheancierImpossibleError
from app.modules.credit.models import Application, Installment
from app.modules.credit.remboursement import (
    AucuneEcheanceAReglerError,
    MontantIncorrectError,
    rembourser,
)
from app.modules.credit.schemas import (
    CreationDemande,
    Decision,
    DemandeDecaissee,
    DemandeDetail,
    DemandeResume,
    EcheanceLigne,
    Remboursement,
    RemboursementRecu,
)
from app.modules.security.autorisation import UtilisateurCourant, exige
from app.modules.security.router import _contexte
from app.modules.tiers.models import Tier

router = APIRouter(tags=["credit"])

MESSAGE_TIER_INTROUVABLE = "Tiers introuvable."
MESSAGE_DEMANDE_INTROUVABLE = "Demande de crédit introuvable."


def _resume(ligne: tuple) -> DemandeResume:
    demande, produit, tier_number, tier_nom = ligne
    return DemandeResume(
        id=demande.id,
        application_number=demande.application_number,
        tier_number=tier_number,
        tier_nom=tier_nom,
        product_code=produit.code,
        product_name=produit.name,
        montant_demande=demande.montant_demande,
        duree_echeances=demande.duree_echeances,
        status=demande.status,
        created_at=demande.created_at,
    )


@router.get("/credit/produits")
def lister_produits_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("credit.product.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[dict[str, object]]:
    """Produits de crédit actifs — pour le choix à la demande."""
    return [
        {"id": p.id, "code": p.code, "name": p.name, "is_provisional": p.is_provisional}
        for p in consultation.lister_produits(db)
    ]


@router.get("/credit/demandes", response_model=list[DemandeResume])
def lister_demandes_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("credit.demande.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[DemandeResume]:
    """Les demandes de crédit dans le périmètre de l'acteur."""
    return [_resume(ligne) for ligne in consultation.lister_demandes(db, courant)]


@router.get("/credit/demandes/{application_id}", response_model=DemandeDetail)
def lire_demande_endpoint(
    application_id: uuid.UUID,
    courant: Annotated[UtilisateurCourant, Depends(exige("credit.demande.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> DemandeDetail:
    ligne = consultation.lire_demande(db, courant, application_id)
    if ligne is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_DEMANDE_INTROUVABLE
        )
    demande, produit, tier_number, tier_nom = ligne
    base = _resume(ligne)
    return DemandeDetail(
        **base.model_dump(),
        objet=demande.objet,
        montant_decide=demande.montant_decide,
        decided_at=demande.decided_at,
        motif_decision=demande.motif_decision,
    )


@router.post(
    "/tiers/{tier_id}/demandes-credit",
    response_model=DemandeResume,
    status_code=status.HTTP_201_CREATED,
)
def creer_demande_endpoint(
    tier_id: uuid.UUID,
    corps: CreationDemande,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("credit.demande.create"))],
    db: Annotated[Session, Depends(get_db)],
) -> DemandeResume:
    """Crée une demande de crédit pour un tiers ACTIF (gate KYC), dans le périmètre de l'acteur."""
    agency_id = db.execute(
        select(Tier.primary_agency_id).where(
            Tier.id == tier_id,
            Tier.deleted_at.is_(None),
            courant.condition_perimetre(Tier.primary_agency_id),
        )
    ).scalar_one_or_none()
    if agency_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_TIER_INTROUVABLE
        )

    try:
        demande = creer_demande(
            db,
            tier_id=tier_id,
            agency_id=agency_id,
            product_id=corps.product_id,
            montant_demande=corps.montant_demande,
            duree_echeances=corps.duree_echeances,
            objet=corps.objet,
            par=courant.user_id,
            contexte=_contexte(request),
        )
        db.commit()
    except (TierNonActifError, ProduitIntrouvableError) as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None

    ligne = consultation.lire_demande(db, courant, demande.id)
    assert ligne is not None
    return _resume(ligne)


@router.post("/credit/demandes/{application_id}/decision", response_model=DemandeDetail)
def decider_endpoint(
    application_id: uuid.UUID,
    corps: Decision,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("credit.demande.decide"))],
    db: Annotated[Session, Depends(get_db)],
) -> DemandeDetail:
    """Approuve ou refuse une demande — motif obligatoire dans les deux sens, décision définitive."""
    ligne = consultation.lire_demande(db, courant, application_id)
    if ligne is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_DEMANDE_INTROUVABLE
        )
    demande = db.get(Application, application_id)
    assert demande is not None

    try:
        decider(
            db,
            demande,
            decision=corps.decision,
            montant_decide=corps.montant_decide,
            motif=corps.motif,
            par=courant.user_id,
            contexte=_contexte(request),
        )
        db.commit()
    except (
        DemandeDejaDecideeError,
        TierNonActifError,
        MontantDecideInvalideError,
    ) as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None

    ligne = consultation.lire_demande(db, courant, application_id)
    assert ligne is not None
    base = _resume(ligne)
    return DemandeDetail(
        **base.model_dump(),
        objet=demande.objet,
        montant_decide=demande.montant_decide,
        decided_at=demande.decided_at,
        motif_decision=demande.motif_decision,
    )


@router.post(
    "/credit/demandes/{application_id}/decaissement", response_model=DemandeDecaissee
)
def decaisser_endpoint(
    application_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("credit.decaissement.create"))],
    db: Annotated[Session, Depends(get_db)],
) -> DemandeDecaissee:
    """Décaisse une demande APPROUVÉE : pièce comptable + échéancier persisté, en une
    transaction unique. Réservé au responsable d'agence (séparation des tâches avec le
    chargé de prêt qui a monté le dossier)."""
    ligne = consultation.lire_demande(db, courant, application_id)
    if ligne is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_DEMANDE_INTROUVABLE
        )
    demande = db.get(Application, application_id)
    assert demande is not None

    try:
        decaisser(db, demande, par=courant.user_id, contexte=_contexte(request))
        db.commit()
    except (
        DemandeNonApprouveeError,
        TierNonActifError,
        ProduitIntrouvableError,
        RattachementManquantError,
        EcheancierImpossibleError,
    ) as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None

    ligne = consultation.lire_demande(db, courant, application_id)
    assert ligne is not None
    base = _resume(ligne)
    echeances = consultation.lire_echeancier(db, courant, application_id)
    compte_credit_number = db.execute(
        select(Account.account_number).where(Account.id == demande.compte_credit_id)
    ).scalar_one_or_none()
    return DemandeDecaissee(
        **base.model_dump(),
        objet=demande.objet,
        montant_decide=demande.montant_decide,
        decided_at=demande.decided_at,
        motif_decision=demande.motif_decision,
        disbursed_at=demande.disbursed_at,
        compte_credit_number=compte_credit_number,
        nb_echeances=len(echeances),
        premiere_echeance_le=echeances[0].due_date if echeances else None,
        derniere_echeance_le=echeances[-1].due_date if echeances else None,
    )


@router.get("/credit/demandes/{application_id}/echeancier", response_model=list[EcheanceLigne])
def lire_echeancier_endpoint(
    application_id: uuid.UUID,
    courant: Annotated[UtilisateurCourant, Depends(exige("credit.demande.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[EcheanceLigne]:
    """L'échéancier persisté d'une demande décaissée (vide si pas encore décaissée)."""
    ligne = consultation.lire_demande(db, courant, application_id)
    if ligne is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_DEMANDE_INTROUVABLE
        )
    return [
        EcheanceLigne(
            numero=e.numero,
            due_date=e.due_date,
            capital=e.capital,
            interets=e.interets,
            total=e.total,
            capital_restant_du=e.capital_restant_du,
            status=e.status,
        )
        for e in consultation.lire_echeancier(db, courant, application_id)
    ]


@router.post(
    "/credit/demandes/{application_id}/remboursement", response_model=RemboursementRecu
)
def rembourser_endpoint(
    application_id: uuid.UUID,
    corps: Remboursement,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("credit.remboursement.create"))],
    db: Annotated[Session, Depends(get_db)],
) -> RemboursementRecu:
    """Règle la prochaine échéance impayée, pour son montant EXACT. Aucun gate KYC (encaisser
    de l'argent qui rentre ne présente aucun risque)."""
    ligne = consultation.lire_demande(db, courant, application_id)
    if ligne is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_DEMANDE_INTROUVABLE
        )
    demande = db.get(Application, application_id)
    assert demande is not None

    try:
        echeance = rembourser(
            db, demande, montant=corps.montant, par=courant.user_id, contexte=_contexte(request)
        )
        db.commit()
    except (
        AucuneEcheanceAReglerError,
        MontantIncorrectError,
        RattachementManquantError,
    ) as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None

    restantes = db.execute(
        select(func.count())
        .select_from(Installment)
        .where(Installment.application_id == application_id, Installment.status == "a_echoir")
    ).scalar_one()
    return RemboursementRecu(
        numero=echeance.numero,
        due_date=echeance.due_date,
        capital=echeance.capital,
        interets=echeance.interets,
        montant_total=echeance.total,
        paid_at=echeance.paid_at,
        echeances_restantes=restantes,
    )
