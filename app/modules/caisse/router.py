"""Endpoints HTTP du module Caisse — CA1 : ouverture, fermeture, lecture ; CA-lettre : consulter
la session d'un autre caissier (manquant), lister les manquants.

Permissions (exige) : ouvrir -> caisse.session.open ; fermer -> caisse.session.close. Lire une
session par id, et lister les manquants, acceptent caisse.session.read OU
caisse.session.read.autres (exige_une_de) — le contrôle FIN (la sienne, ou dans son périmètre
avec .autres) se fait à l'objet dans le service, jamais ici : voir
caisse/service.py::_condition_lecture.

TABLE DES ERREURS :
  - permission absente                          -> 403 (exige()/exige_une_de(), en amont)
  - session hors périmètre ou inexistante        -> 404 (jamais 403 : IDOR, on ne révèle rien)
  - session déjà ouverte / déjà fermée           -> 422
  - agence sans compte de caisse rattaché        -> 422
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.caisse.models import CaisseSession
from app.modules.caisse.schemas import (
    FermetureSession,
    LigneSessionManquante,
    OuvertureSession,
    PageSessionsManquantes,
    SessionCaisse,
)
from app.modules.caisse.service import (
    TAILLE_PAGE_DEFAUT,
    TAILLE_PAGE_MAX,
    PosteAmbiguError,
    RattachementManquantError,
    SessionDejaFermeeError,
    SessionDejaOuverteError,
    SessionIntrouvableError,
    calculer_solde_theorique,
    fermer_session,
    lire_session,
    lister_sessions_manquantes,
    ouvrir_session,
    session_ouverte_de_lacteur,
)
from app.modules.comptabilite.models import Account
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant, exige, exige_une_de
from app.modules.security.models import User
from app.modules.security.router import _contexte

router = APIRouter(tags=["caisse"])

MESSAGE_SESSION_INTROUVABLE = "Session de caisse introuvable."
MESSAGE_MANQUANT_SEUL = "Seul manquant=true est pris en charge pour l'instant."


def _vers_schema(db: Session, session: CaisseSession) -> SessionCaisse:
    # UNE requête combinée (compte + caissier + agence) plutôt que trois — les trois tables
    # n'ont aucun lien direct entre elles pour cet usage ; jointures explicites sur un id FIXE
    # (pas une colonne d'une autre table du FROM) pour qu'aucune ne se lise comme un produit
    # cartésien, chacune ne rendant qu'UNE ligne puisqu'elle porte sur une clé primaire.
    nom_complet = func.concat_ws(" ", User.first_name, User.last_name)
    numero, caissier_nom, agency_nom = db.execute(
        select(Account.account_number, nom_complet, Agency.name)
        .select_from(Account)
        .join(User, User.id == session.caissier_id)
        .join(Agency, Agency.id == session.agency_id)
        .where(Account.id == session.compte_caisse_id)
    ).one()
    # EN DIRECT tant que la session est ouverte (même calcul que la fermeture, sans figer) ;
    # None une fois fermée — le chiffre figé est solde_theorique_cloture, pas la peine de le
    # répéter sous un second nom.
    actuel = calculer_solde_theorique(db, session) if session.status == "ouverte" else None
    return SessionCaisse(
        id=session.id,
        agency_id=session.agency_id,
        agency_nom=agency_nom,
        caissier_id=session.caissier_id,
        caissier_nom=caissier_nom,
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
    except (SessionDejaOuverteError, RattachementManquantError, PosteAmbiguError) as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    return _vers_schema(db, session)


@router.get("/caisse/sessions", response_model=PageSessionsManquantes)
def lister_sessions_manquantes_endpoint(
    courant: Annotated[
        UtilisateurCourant,
        Depends(exige_une_de("caisse.session.read", "caisse.session.read.autres")),
    ],
    db: Annotated[Session, Depends(get_db)],
    manquant: Annotated[
        bool, Query(description="Seule valeur prise en charge pour l'instant : true.")
    ],
    page: Annotated[int, Query(ge=1)] = 1,
    taille: Annotated[int, Query(ge=1, le=TAILLE_PAGE_MAX)] = TAILLE_PAGE_DEFAUT,
) -> PageSessionsManquantes:
    """Sessions fermées avec un MANQUANT (écart < 0) : les SIENNES pour un caissier
    (caisse.session.read seul suffit), plus celles de son périmètre s'il détient
    caisse.session.read.autres (responsable : son agence ; audit/direction : tout le réseau).
    Sert à retrouver une lettre de demande d'explication sans dépendre d'un lien reçu au
    moment de la fermeture. `manquant` est requis et explicite : pas de listing général des
    sessions pour l'instant (hors périmètre de cette fonctionnalité)."""
    if not manquant:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=MESSAGE_MANQUANT_SEUL
        )
    resultat = lister_sessions_manquantes(db, courant, page=page, taille=taille)
    return PageSessionsManquantes(
        lignes=[
            LigneSessionManquante(
                id=ligne.id,
                caissier_id=ligne.caissier_id,
                caissier_nom=ligne.caissier_nom,
                agency_id=ligne.agency_id,
                agency_nom=ligne.agency_nom,
                compte_caisse_number=ligne.compte_caisse_number,
                fonds_initial=ligne.fonds_initial,
                opened_at=ligne.opened_at,
                closed_at=ligne.closed_at,
                montant_reel_cloture=ligne.montant_reel_cloture,
                solde_theorique_cloture=ligne.solde_theorique_cloture,
                ecart=ligne.ecart,
            )
            for ligne in resultat.lignes
        ],
        total=resultat.total,
        page=resultat.page,
        taille=resultat.taille,
    )


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
    courant: Annotated[
        UtilisateurCourant,
        Depends(exige_une_de("caisse.session.read", "caisse.session.read.autres")),
    ],
    db: Annotated[Session, Depends(get_db)],
) -> SessionCaisse:
    """La sienne toujours ; une autre SEULEMENT avec caisse.session.read.autres ET dans le
    périmètre (`lire_session`, objet — pas seulement la route)."""
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
