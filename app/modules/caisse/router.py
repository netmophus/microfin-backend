"""Endpoints HTTP du module Caisse — CA1 : ouverture, fermeture, lecture.

Permissions (exige) : ouvrir -> caisse.session.open ; fermer -> caisse.session.close ; lire ->
caisse.session.read. Les trois sont réservées au CAISSIER lui-même — une session appartient à
SON caissier (`caissier_id`), vérifié à l'objet (IDOR), jamais seulement à la route.

TABLE DES ERREURS :
  - permission absente                          -> 403 (exige(), en amont)
  - session hors périmètre ou inexistante        -> 404 (jamais 403 : IDOR, on ne révèle rien)
  - session déjà ouverte / déjà fermée           -> 422
  - agence sans compte de caisse rattaché        -> 422
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.caisse.models import CaisseSession
from app.modules.caisse.schemas import FermetureSession, OuvertureSession, SessionCaisse
from app.modules.caisse.service import (
    RattachementManquantError,
    SessionDejaFermeeError,
    SessionDejaOuverteError,
    SessionIntrouvableError,
    calculer_solde_theorique,
    fermer_session,
    lire_session,
    ouvrir_session,
    session_ouverte_de_lacteur,
)
from app.modules.comptabilite.models import Account
from app.modules.security.autorisation import UtilisateurCourant, exige
from app.modules.security.router import _contexte

router = APIRouter(tags=["caisse"])

MESSAGE_SESSION_INTROUVABLE = "Session de caisse introuvable."


def _vers_schema(db: Session, session: CaisseSession) -> SessionCaisse:
    numero = db.execute(
        select(Account.account_number).where(Account.id == session.compte_caisse_id)
    ).scalar_one()
    # EN DIRECT tant que la session est ouverte (même calcul que la fermeture, sans figer) ;
    # None une fois fermée — le chiffre figé est solde_theorique_cloture, pas la peine de le
    # répéter sous un second nom.
    actuel = calculer_solde_theorique(db, session) if session.status == "ouverte" else None
    return SessionCaisse(
        id=session.id,
        agency_id=session.agency_id,
        caissier_id=session.caissier_id,
        compte_caisse_number=numero,
        fonds_initial=session.fonds_initial,
        opened_at=session.opened_at,
        closed_at=session.closed_at,
        solde_theorique_actuel=actuel,
        montant_reel_cloture=session.montant_reel_cloture,
        solde_theorique_cloture=session.solde_theorique_cloture,
        ecart=session.ecart,
        status=session.status,
    )


@router.post(
    "/caisse/sessions", response_model=SessionCaisse, status_code=status.HTTP_201_CREATED
)
def ouvrir_session_endpoint(
    corps: OuvertureSession,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("caisse.session.open"))],
    db: Annotated[Session, Depends(get_db)],
) -> SessionCaisse:
    """Ouvre une session pour L'ACTEUR — `caissier_id` n'est jamais dans le corps de la requête,
    toujours dérivé du jeton."""
    try:
        session = ouvrir_session(
            db, courant, fonds_initial=corps.fonds_initial, contexte=_contexte(request)
        )
        db.commit()
    except (SessionDejaOuverteError, RattachementManquantError) as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    return _vers_schema(db, session)


@router.get("/caisse/sessions/courante", response_model=SessionCaisse | None)
def session_courante_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("caisse.session.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> SessionCaisse | None:
    """La session actuellement ouverte de L'ACTEUR, ou null — pour qu'un écran sache s'il doit
    proposer « ouvrir » ou « fermer » sans deviner."""
    session = session_ouverte_de_lacteur(db, courant)
    return _vers_schema(db, session) if session is not None else None


@router.get("/caisse/sessions/{session_id}", response_model=SessionCaisse)
def lire_session_endpoint(
    session_id: uuid.UUID,
    courant: Annotated[UtilisateurCourant, Depends(exige("caisse.session.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> SessionCaisse:
    try:
        session = lire_session(db, courant, session_id)
    except SessionIntrouvableError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_SESSION_INTROUVABLE
        ) from None
    return _vers_schema(db, session)


@router.post("/caisse/sessions/{session_id}/fermeture", response_model=SessionCaisse)
def fermer_session_endpoint(
    session_id: uuid.UUID,
    corps: FermetureSession,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("caisse.session.close"))],
    db: Annotated[Session, Depends(get_db)],
) -> SessionCaisse:
    """Ferme la session de L'ACTEUR — calcule et FIGE l'écart. Ne bloque JAMAIS sur l'écart
    (CA2), ne pose aucune écriture (CA3)."""
    try:
        resultat = fermer_session(
            db, courant, session_id, montant_reel=corps.montant_reel, contexte=_contexte(request)
        )
        db.commit()
    except SessionIntrouvableError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_SESSION_INTROUVABLE
        ) from None
    except SessionDejaFermeeError as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    return _vers_schema(db, resultat.session)
