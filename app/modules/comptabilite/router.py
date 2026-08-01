"""Endpoints HTTP — Plan de comptes, Bloc 1 (consultation + gestion unitaire).

TABLE DES ERREURS (un seul endroit) :
  - permission absente                          -> 403 (exige(), en amont)
  - compte inexistant                           -> 404
  - numéro déjà utilisé / classe-numéro incohérente / parent invalide -> 422, message humain
  - garde-fou (système, mouvementé, enfants actifs) -> 422, message humain (service.py)

Lecture -> compta.plan.read. Écriture (créer, modifier, sens, désactiver) -> compta.plan.manage.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.comptabilite import comptes
from app.modules.comptabilite.comptes import (
    TAILLE_PAGE_DEFAUT,
    TAILLE_PAGE_MAX,
    ChampInvalideError,
    FiltresComptes,
    NumeroDejaUtiliseError,
    ParentIntrouvableError,
)
from app.modules.comptabilite.models import Account
from app.modules.comptabilite.schemas import (
    ChangementSens,
    CompteDetail,
    CompteResume,
    CreationCompte,
    DesactivationCompte,
    ModificationCompte,
    PageComptes,
)
from app.modules.comptabilite.service import ModificationInterditeError
from app.modules.security.autorisation import UtilisateurCourant, exige
from app.modules.security.router import _contexte

router = APIRouter(prefix="/comptabilite", tags=["comptabilite"])

MESSAGE_INTROUVABLE = "Compte du plan introuvable."


def _vers_resume(compte: Account, parent_number: str | None) -> CompteResume:
    return CompteResume(
        id=compte.id,
        account_number=compte.account_number,
        name=compte.name,
        short_name=compte.short_name,
        account_class=compte.account_class,
        parent_number=parent_number,
        normal_side=compte.normal_side,
        is_posting=compte.is_posting,
        is_system=compte.is_system,
        is_provisional=compte.is_provisional,
        is_active=compte.is_active,
    )


def _vers_detail(compte: Account, parent_number: str | None) -> CompteDetail:
    base = _vers_resume(compte, parent_number)
    return CompteDetail(
        **base.model_dump(),
        notes=compte.notes,
        created_at=compte.created_at,
        updated_at=compte.updated_at,
    )


def _422(erreur: Exception) -> HTTPException:
    """Refus -> 422 avec le message métier (dit POURQUOI, langage humain). Un seul endroit."""
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur))


def _charger(db: Session, compte_id: uuid.UUID) -> Account:
    compte = db.get(Account, compte_id)
    if compte is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_INTROUVABLE)
    return compte


@router.get("/comptes", response_model=PageComptes)
def lister_comptes_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.plan.read"))],
    db: Annotated[Session, Depends(get_db)],
    q: Annotated[str | None, Query(description="Recherche — numéro ou libellé.")] = None,
    classe: Annotated[int | None, Query(ge=1, le=9, description="Filtre par classe.")] = None,
    inclure_inactifs: Annotated[
        bool, Query(description="Inclure les comptes désactivés.")
    ] = False,
    page: Annotated[int, Query(ge=1)] = 1,
    taille: Annotated[int, Query(ge=1, le=TAILLE_PAGE_MAX)] = TAILLE_PAGE_DEFAUT,
) -> PageComptes:
    """Le plan de comptes est INSTITUTION-WIDE : aucun cloisonnement par agence (à la différence
    des tiers)."""
    resultat = comptes.lister(
        db, FiltresComptes(q=q, classe=classe, inclure_inactifs=inclure_inactifs),
        page=page, taille=taille,
    )
    return PageComptes(
        lignes=[_vers_resume(c, resultat.parents.get(c.id)) for c in resultat.comptes],
        total=resultat.total,
        page=page,
        taille=taille,
    )


@router.get("/comptes/{compte_id}", response_model=CompteDetail)
def lire_compte_endpoint(
    compte_id: uuid.UUID,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.plan.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> CompteDetail:
    ligne = comptes.lire(db, compte_id)
    if ligne is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_INTROUVABLE)
    compte, parent_number = ligne
    return _vers_detail(compte, parent_number)


@router.post("/comptes", response_model=CompteDetail, status_code=status.HTTP_201_CREATED)
def creer_compte_endpoint(
    corps: CreationCompte,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.plan.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> CompteDetail:
    try:
        compte = comptes.creer(
            db,
            account_number=corps.account_number,
            name=corps.name,
            short_name=corps.short_name,
            account_class=corps.account_class,
            parent_number=corps.parent_number,
            normal_side=corps.normal_side,
            is_posting=corps.is_posting,
            notes=corps.notes,
            par=courant.user_id,
            contexte=_contexte(request),
        )
        db.commit()
    except (ChampInvalideError, ParentIntrouvableError, NumeroDejaUtiliseError) as erreur:
        db.rollback()
        raise _422(erreur) from None
    except IntegrityError as erreur:
        # Filet de sécurité : deux créations concurrentes du même numéro (rare).
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Le compte « {corps.account_number} » existe déjà.",
        ) from erreur
    return _vers_detail(compte, corps.parent_number)


@router.patch("/comptes/{compte_id}", response_model=CompteDetail)
def modifier_compte_endpoint(
    compte_id: uuid.UUID,
    corps: ModificationCompte,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.plan.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> CompteDetail:
    """Modification PARTIELLE du libellé/notes. Le sens et la désactivation ont leurs propres
    actions dédiées (garde-fous, motif obligatoire) — jamais via ce PATCH générique."""
    compte = _charger(db, compte_id)
    try:
        comptes.modifier(db, compte, corps.modifications(), courant.user_id, _contexte(request))
        db.commit()
    except ChampInvalideError as erreur:
        db.rollback()
        raise _422(erreur) from None
    ligne = comptes.lire(db, compte_id)
    assert ligne is not None
    return _vers_detail(*ligne)


@router.post("/comptes/{compte_id}/sens", response_model=CompteDetail)
def changer_sens_endpoint(
    compte_id: uuid.UUID,
    corps: ChangementSens,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.plan.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> CompteDetail:
    compte = _charger(db, compte_id)
    try:
        comptes.changer_sens(
            db, compte, corps.normal_side, corps.motif, courant.user_id, _contexte(request)
        )
        db.commit()
    except ModificationInterditeError as erreur:
        db.rollback()
        raise _422(erreur) from None
    ligne = comptes.lire(db, compte_id)
    assert ligne is not None
    return _vers_detail(*ligne)


@router.post("/comptes/{compte_id}/desactiver", response_model=CompteDetail)
def desactiver_compte_endpoint(
    compte_id: uuid.UUID,
    corps: DesactivationCompte,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.plan.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> CompteDetail:
    compte = _charger(db, compte_id)
    try:
        comptes.desactiver_compte(db, compte, corps.motif, courant.user_id, _contexte(request))
        db.commit()
    except ModificationInterditeError as erreur:
        db.rollback()
        raise _422(erreur) from None
    ligne = comptes.lire(db, compte_id)
    assert ligne is not None
    return _vers_detail(*ligne)
