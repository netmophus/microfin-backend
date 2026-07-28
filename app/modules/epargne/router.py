"""Endpoints HTTP du module Épargne — F1 : consultation + ouverture.

Permissions (exige) : lecture -> epargne.account.read / epargne.product.read ; ouverture ->
epargne.account.open. Le cloisonnement fin est une règle du service/consultation, pas un code
d'erreur : hors périmètre -> 404 (n'existe pas de mon point de vue), jamais 403.

TABLE DES ERREURS (un seul endroit, _traduire) :
  - permission absente        -> 403 (exige(), en amont)
  - membre / compte hors périmètre ou inexistant -> 404
  - membre non actif (gate KYC) -> 422 avec message : un prospect n'a pas droit à un compte
  - produit inexistant/inactif  -> 422
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.epargne import consultation
from app.modules.epargne.schemas import (
    CompteEpargneDetail,
    CompteEpargneResume,
    MouvementResume,
    OuvertureCompte,
    ProduitEpargne,
)
from app.modules.epargne.service import (
    MembreNonActifError,
    ProduitIntrouvableError,
    ouvrir_compte,
)
from app.modules.security.autorisation import UtilisateurCourant, exige
from app.modules.security.router import _contexte
from app.modules.tiers.models import Tier

router = APIRouter(tags=["epargne"])

MESSAGE_COMPTE_INTROUVABLE = "Compte d'épargne introuvable."
MESSAGE_MEMBRE_INTROUVABLE = "Membre introuvable."


def _resume(compte: object, produit: object) -> CompteEpargneResume:
    return CompteEpargneResume(
        id=compte.id,
        account_number=compte.account_number,
        product_code=produit.code,
        product_name=produit.name,
        product_type=produit.type,
        currency=compte.currency,
        balance=compte.balance,
        status=compte.status,
        is_provisional=produit.is_provisional,
    )


@router.get("/epargne/produits", response_model=list[ProduitEpargne])
def lister_produits_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("epargne.product.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[ProduitEpargne]:
    """Les produits d'épargne actifs (pour le choix à l'ouverture)."""
    return [
        ProduitEpargne(
            id=p.id, code=p.code, name=p.name, type=p.type, is_provisional=p.is_provisional
        )
        for p in consultation.lister_produits(db)
    ]


@router.get("/tiers/{tier_id}/comptes-epargne", response_model=list[CompteEpargneResume])
def lister_comptes_membre_endpoint(
    tier_id: uuid.UUID,
    courant: Annotated[UtilisateurCourant, Depends(exige("epargne.account.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[CompteEpargneResume]:
    """Les comptes d'épargne d'un membre (cloisonné à l'agence de l'acteur)."""
    return [
        _resume(compte, produit)
        for compte, produit in consultation.lister_comptes_du_membre(db, courant, tier_id)
    ]


@router.get("/epargne/comptes/{compte_id}", response_model=CompteEpargneDetail)
def lire_compte_endpoint(
    compte_id: uuid.UUID,
    courant: Annotated[UtilisateurCourant, Depends(exige("epargne.account.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> CompteEpargneDetail:
    """Détail d'un compte + relevé des mouvements. 404 si hors périmètre."""
    ligne = consultation.lire_compte(db, courant, compte_id)
    if ligne is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_COMPTE_INTROUVABLE
        )
    compte, produit = ligne
    mouvements = [
        MouvementResume(
            id=m.id,
            sens=m.sens,
            amount=m.amount,
            balance_after=m.balance_after,
            operation_type=m.operation_type,
            label=m.label,
            created_at=m.created_at,
            entry_number=entry_number,
        )
        for m, entry_number in consultation.lister_mouvements(db, compte_id)
    ]
    base = _resume(compte, produit)
    return CompteEpargneDetail(
        **base.model_dump(),
        opened_at=compte.opened_at,
        closed_at=compte.closed_at,
        mouvements=mouvements,
    )


@router.post(
    "/tiers/{tier_id}/comptes-epargne",
    response_model=CompteEpargneResume,
    status_code=status.HTTP_201_CREATED,
)
def ouvrir_compte_endpoint(
    tier_id: uuid.UUID,
    corps: OuvertureCompte,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("epargne.account.open"))],
    db: Annotated[Session, Depends(get_db)],
) -> CompteEpargneResume:
    """Ouvre un compte d'épargne pour un membre ACTIF (gate KYC). Réservé chargé/responsable."""
    # Le membre doit être dans le périmètre de l'acteur ; on en tire aussi son agence.
    agency_id = db.execute(
        select(Tier.primary_agency_id).where(
            Tier.id == tier_id,
            Tier.deleted_at.is_(None),
            courant.condition_perimetre(Tier.primary_agency_id),
        )
    ).scalar_one_or_none()
    if agency_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_MEMBRE_INTROUVABLE
        )

    try:
        compte = ouvrir_compte(
            db,
            tier_id=tier_id,
            product_id=corps.product_id,
            agency_id=agency_id,
            par=courant.user_id,
            contexte=_contexte(request),
        )
        db.commit()
    except MembreNonActifError as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    except ProduitIntrouvableError as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None

    produit = next(
        p for p in consultation.lister_produits(db) if p.id == corps.product_id
    )
    return _resume(compte, produit)
