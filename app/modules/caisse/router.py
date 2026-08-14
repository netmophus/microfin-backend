"""Endpoints HTTP du module Caisse — CA1 : ouverture, fermeture, lecture ; CA-lettre : consulter
la session d'un autre caissier (manquant), lister les manquants ; Bloc C : sélection explicite
du poste à l'ouverture ; CA2 : seuil de tolérance, motif obligatoire au-delà, validation a
posteriori du responsable.

Permissions (exige) : ouvrir -> caisse.session.open ; fermer -> caisse.session.close. Lire une
session par id, lister les manquants, et lister les sessions à valider, acceptent
caisse.session.read OU caisse.session.read.autres (exige_une_de) — le contrôle FIN (la sienne,
ou dans son périmètre avec .autres) se fait à l'objet dans le service, jamais ici : voir
caisse/service.py::_condition_lecture. Seuil de tolérance : LECTURE via compta.plan.manage OU
caisse.session.close (le caissier doit savoir s'il devra motiver AVANT de fermer) ; ÉCRITURE
réservée à compta.plan.manage seul (même permission que les autres paramètres du Bloc 5).
Valider un écart -> caisse.session.valider.

TABLE DES ERREURS :
  - permission absente                          -> 403 (exige()/exige_une_de(), en amont)
  - session hors périmètre ou inexistante        -> 404 (jamais 403 : IDOR, on ne révèle rien)
  - poste hors périmètre, inactif, ou non assigné à l'acteur -> 404 (même discipline IDOR)
  - seuil de tolérance non paramétré             -> 404
  - session déjà ouverte / déjà fermée           -> 422
  - poste sans compte de caisse rattaché         -> 422
  - écart au-delà du seuil sans motif (CA2)      -> 422
  - écart déjà validé / non significatif (CA2)   -> 422
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.core.database import get_db
from app.modules.caisse import postes
from app.modules.caisse.models import CaisseSession, Poste
from app.modules.caisse.parametres import ParametrageManquantError
from app.modules.caisse.parametres import lire as lire_parametres
from app.modules.caisse.parametres import modifier as modifier_parametres
from app.modules.caisse.postes import (
    CodeDejaUtiliseError,
    PosteEnUsageError,
    PosteIntrouvableError,
    UtilisateurHorsPerimetreError,
)
from app.modules.caisse.schemas import (
    ActivationPoste,
    AssignationCreation,
    CreationPoste,
    FermetureSession,
    LigneSessionAValider,
    LigneSessionManquante,
    ModificationParametresCaisse,
    ModificationPoste,
    OuvertureSession,
    PageSessionsAValider,
    PageSessionsManquantes,
    ParametresCaisse,
    PosteCaisse,
    RattachementComptePoste,
    SessionCaisse,
    UtilisateurAssigne,
)
from app.modules.caisse.service import (
    TAILLE_PAGE_DEFAUT,
    TAILLE_PAGE_MAX,
    EcartNonSignificatifError,
    MotifRequisError,
    RattachementManquantError,
    SessionDejaFermeeError,
    SessionDejaOuverteError,
    SessionDejaValideeError,
    SessionIntrouvableError,
    calculer_solde_theorique,
    fermer_session,
    lire_session,
    lister_sessions_a_valider,
    lister_sessions_manquantes,
    ouvrir_session,
    session_a_valider,
    session_ouverte_de_lacteur,
    seuil_tolerance,
    valider_ecart,
)
from app.modules.comptabilite.comptes import CompteInvalideRattachementError
from app.modules.comptabilite.models import Account
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant, exige, exige_une_de
from app.modules.security.models import User
from app.modules.security.router import _contexte

router = APIRouter(tags=["caisse"])

MESSAGE_SESSION_INTROUVABLE = "Session de caisse introuvable."
MESSAGE_MANQUANT_SEUL = "Seul manquant=true est pris en charge pour l'instant."
MESSAGE_POSTE_INTROUVABLE = "Poste de caisse introuvable."


def _vers_schema(db: Session, session: CaisseSession) -> SessionCaisse:
    # UNE requête combinée (compte + caissier + agence + validateur) plutôt que quatre — jointures
    # explicites sur un id FIXE (pas une colonne d'une autre table du FROM) pour qu'aucune ne se
    # lise comme un produit cartésien. validateur en LEFT JOIN : `valide_par` est NULL tant que
    # personne n'a validé, la ligne doit survivre malgré tout.
    validateur = aliased(User)
    nom_complet = func.concat_ws(" ", User.first_name, User.last_name)
    nom_validateur = func.concat_ws(" ", validateur.first_name, validateur.last_name)
    numero, caissier_nom, agency_nom, valide_par_nom = db.execute(
        select(Account.account_number, nom_complet, Agency.name, nom_validateur)
        .select_from(Account)
        .join(User, User.id == session.caissier_id)
        .join(Agency, Agency.id == session.agency_id)
        .outerjoin(validateur, validateur.id == session.valide_par)
        .where(Account.id == session.compte_caisse_id)
    ).one()
    # EN DIRECT tant que la session est ouverte (même calcul que la fermeture, sans figer) ;
    # None une fois fermée — le chiffre figé est solde_theorique_cloture, pas la peine de le
    # répéter sous un second nom.
    actuel = calculer_solde_theorique(db, session) if session.status == "ouverte" else None
    seuil = seuil_tolerance(db)
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
        motif_ecart=session.motif_ecart,
        valide_le=session.valide_le,
        valide_par_nom=valide_par_nom,
        a_valider=session_a_valider(session, seuil),
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
    toujours dérivé du jeton. `poste_id`, lui, est TOUJOURS soumis par le client (Bloc C) —
    jamais déduit ici."""
    try:
        session = ouvrir_session(
            db,
            courant,
            poste_id=corps.poste_id,
            fonds_initial=corps.fonds_initial,
            contexte=_contexte(request),
        )
        db.commit()
    except PosteIntrouvableError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_POSTE_INTROUVABLE
        ) from None
    except (SessionDejaOuverteError, RattachementManquantError) as erreur:
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
    (CA2) : un motif est exigé au-delà du seuil de tolérance (422 si absent), jamais un refus
    de fermer. Ne pose aucune écriture (CA3)."""
    try:
        resultat = fermer_session(
            db,
            courant,
            session_id,
            montant_reel=corps.montant_reel,
            motif=corps.motif,
            contexte=_contexte(request),
        )
        db.commit()
    except SessionIntrouvableError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_SESSION_INTROUVABLE
        ) from None
    except (SessionDejaFermeeError, MotifRequisError) as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    return _vers_schema(db, resultat.session)


@router.get("/caisse/parametres", response_model=ParametresCaisse)
def lire_parametres_endpoint(
    courant: Annotated[
        UtilisateurCourant, Depends(exige_une_de("compta.plan.manage", "caisse.session.close"))
    ],
    db: Annotated[Session, Depends(get_db)],
) -> ParametresCaisse:
    """Le seuil de tolérance courant (CA2) — LECTURE élargie au caissier (caisse.session.close) :
    il doit savoir, AVANT de fermer, si son écart franchira le seuil et exigera un motif — ce
    n'est qu'un nombre, pas une donnée sensible. L'ÉCRITURE reste réservée compta.plan.manage
    seul (voir modifier_parametres_endpoint), même permission que les autres paramètres du
    Bloc 5 : c'est le comptable qui gère la config d'institution."""
    try:
        config = lire_parametres(db)
    except ParametrageManquantError as erreur:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(erreur)
        ) from None
    return ParametresCaisse(
        seuil_tolerance=config.seuil_tolerance, is_provisional=config.is_provisional
    )


@router.put("/caisse/parametres", response_model=ParametresCaisse)
def modifier_parametres_endpoint(
    corps: ModificationParametresCaisse,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.plan.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> ParametresCaisse:
    """Modifie le seuil — motif obligatoire, tracé avant/après (comme tout Bloc 5)."""
    try:
        config = lire_parametres(db)
    except ParametrageManquantError as erreur:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(erreur)
        ) from None
    config = modifier_parametres(
        db,
        config,
        seuil_tolerance=corps.seuil_tolerance,
        motif=corps.motif,
        par=courant.user_id,
        contexte=_contexte(request),
    )
    db.commit()
    return ParametresCaisse(
        seuil_tolerance=config.seuil_tolerance, is_provisional=config.is_provisional
    )


@router.get("/caisse/sessions-a-valider", response_model=PageSessionsAValider)
def lister_sessions_a_valider_endpoint(
    courant: Annotated[
        UtilisateurCourant,
        Depends(exige_une_de("caisse.session.read", "caisse.session.read.autres")),
    ],
    db: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    taille: Annotated[int, Query(ge=1, le=TAILLE_PAGE_MAX)] = TAILLE_PAGE_DEFAUT,
) -> PageSessionsAValider:
    """Sessions fermées avec un écart au-delà du seuil de tolérance, pas encore validées (CA2)
    — la file d'attente du responsable. Même règle de périmètre que le reste du module : le
    caissier voit les SIENNES, un responsable/audit/direction celles de son périmètre."""
    resultat = lister_sessions_a_valider(db, courant, page=page, taille=taille)
    return PageSessionsAValider(
        lignes=[
            LigneSessionAValider(
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
                motif_ecart=ligne.motif_ecart,
            )
            for ligne in resultat.lignes
        ],
        total=resultat.total,
        page=resultat.page,
        taille=resultat.taille,
        seuil_tolerance=resultat.seuil_tolerance,
    )


@router.post("/caisse/sessions/{session_id}/validation-ecart", response_model=SessionCaisse)
def valider_ecart_endpoint(
    session_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("caisse.session.valider"))],
    db: Annotated[Session, Depends(get_db)],
) -> SessionCaisse:
    """Validation A POSTERIORI de l'écart par le responsable (CA2) — une TRACE, jamais un
    blocage : ne change rien d'autre que valide_le/valide_par. Réservé au périmètre de l'acteur
    (vérifié dans le service, au niveau de l'objet)."""
    try:
        session = valider_ecart(db, courant, session_id, contexte=_contexte(request))
        db.commit()
    except SessionIntrouvableError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_SESSION_INTROUVABLE
        ) from None
    except (SessionDejaValideeError, EcartNonSignificatifError) as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    return _vers_schema(db, session)


# --- Postes de caisse (Bloc B) ---------------------------------------------------------------
# CRUD (création/renommage/(dés)activation/assignation) : caisse.poste.manage, SON agence.
# Rattachement comptable : compta.plan.manage (existant), institution entière — comme les 3
# autres écrans Bloc 5.


def _vers_schema_poste(db: Session, poste: Poste) -> PosteCaisse:
    agence_nom = db.execute(select(Agency.name).where(Agency.id == poste.agency_id)).scalar_one()
    compte = db.get(Account, poste.compte_caisse_id) if poste.compte_caisse_id else None
    return PosteCaisse(
        id=poste.id,
        agency_id=poste.agency_id,
        agency_nom=agence_nom,
        code=poste.code,
        libelle=poste.libelle,
        compte_caisse_number=compte.account_number if compte else None,
        compte_caisse_name=compte.name if compte else None,
        is_active=poste.is_active,
    )


def _vers_schema_utilisateur(user: User) -> UtilisateurAssigne:
    nom = f"{user.first_name or ''} {user.last_name or ''}".strip() or user.username
    return UtilisateurAssigne(
        id=user.id, matricule=user.matricule, username=user.username, nom_complet=nom
    )


@router.get("/caisse/postes", response_model=list[PosteCaisse])
def lister_postes_endpoint(
    courant: Annotated[
        UtilisateurCourant, Depends(exige_une_de("caisse.poste.manage", "compta.plan.manage"))
    ],
    db: Annotated[Session, Depends(get_db)],
) -> list[PosteCaisse]:
    """Institution entière pour compta.plan.manage (comme les autres écrans Bloc 5) ; SON
    agence sinon (RESPONSABLE_AGENCE)."""
    return [_vers_schema_poste(db, p) for p in postes.lister(db, courant)]


@router.post("/caisse/postes", response_model=PosteCaisse, status_code=status.HTTP_201_CREATED)
def creer_poste_endpoint(
    corps: CreationPoste,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("caisse.poste.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> PosteCaisse:
    """Crée un poste pour l'agence COURANTE de l'acteur — jamais une agence soumise par le
    client."""
    try:
        poste = postes.creer(
            db,
            courant,
            code=corps.code,
            libelle=corps.libelle,
            motif=corps.motif,
            contexte=_contexte(request),
        )
        db.commit()
    except CodeDejaUtiliseError as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    return _vers_schema_poste(db, poste)


@router.patch("/caisse/postes/{poste_id}", response_model=PosteCaisse)
def modifier_poste_endpoint(
    poste_id: uuid.UUID,
    corps: ModificationPoste,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("caisse.poste.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> PosteCaisse:
    try:
        poste = postes.charger_poste_gere(db, courant, poste_id)
    except PosteIntrouvableError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_POSTE_INTROUVABLE
        ) from None
    try:
        postes.renommer(
            db,
            courant,
            poste,
            code=corps.code,
            libelle=corps.libelle,
            motif=corps.motif,
            contexte=_contexte(request),
        )
        db.commit()
    except CodeDejaUtiliseError as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    return _vers_schema_poste(db, poste)


@router.patch("/caisse/postes/{poste_id}/activation", response_model=PosteCaisse)
def activation_poste_endpoint(
    poste_id: uuid.UUID,
    corps: ActivationPoste,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("caisse.poste.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> PosteCaisse:
    """(Dés)active — la désactivation refuse si une session est actuellement ouverte dessus."""
    try:
        poste = postes.charger_poste_gere(db, courant, poste_id)
    except PosteIntrouvableError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_POSTE_INTROUVABLE
        ) from None
    try:
        postes.changer_activation(
            db,
            courant,
            poste,
            is_active=corps.is_active,
            motif=corps.motif,
            contexte=_contexte(request),
        )
        db.commit()
    except PosteEnUsageError as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    return _vers_schema_poste(db, poste)


@router.patch("/caisse/postes/{poste_id}/compte-caisse", response_model=PosteCaisse)
def rattacher_compte_poste_endpoint(
    poste_id: uuid.UUID,
    corps: RattachementComptePoste,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.plan.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> PosteCaisse:
    """Institution entière : le comptable configure le plan de comptes du réseau, pas une seule
    agence — même portée que les 3 autres écrans de rattachement Bloc 5."""
    try:
        poste = postes.charger_poste_pour_rattachement(db, poste_id)
    except PosteIntrouvableError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_POSTE_INTROUVABLE
        ) from None
    try:
        postes.rattacher_compte(
            db,
            courant,
            poste,
            compte_caisse_number=corps.compte_caisse,
            motif=corps.motif,
            contexte=_contexte(request),
        )
        db.commit()
    except CompteInvalideRattachementError as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    return _vers_schema_poste(db, poste)


@router.get("/caisse/postes/{poste_id}/assignations", response_model=list[UtilisateurAssigne])
def lister_assignations_endpoint(
    poste_id: uuid.UUID,
    courant: Annotated[UtilisateurCourant, Depends(exige("caisse.poste.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[UtilisateurAssigne]:
    try:
        poste = postes.charger_poste_gere(db, courant, poste_id)
    except PosteIntrouvableError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_POSTE_INTROUVABLE
        ) from None
    return [_vers_schema_utilisateur(u) for u in postes.lister_assignations(db, poste)]


@router.post(
    "/caisse/postes/{poste_id}/assignations",
    response_model=list[UtilisateurAssigne],
    status_code=status.HTTP_201_CREATED,
)
def assigner_endpoint(
    poste_id: uuid.UUID,
    corps: AssignationCreation,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("caisse.poste.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[UtilisateurAssigne]:
    try:
        poste = postes.charger_poste_gere(db, courant, poste_id)
    except PosteIntrouvableError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_POSTE_INTROUVABLE
        ) from None
    try:
        postes.assigner(db, courant, poste, user_id=corps.user_id, contexte=_contexte(request))
        db.commit()
    except UtilisateurHorsPerimetreError as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    return [_vers_schema_utilisateur(u) for u in postes.lister_assignations(db, poste)]


@router.delete(
    "/caisse/postes/{poste_id}/assignations/{user_id}", status_code=status.HTTP_204_NO_CONTENT
)
def revoquer_endpoint(
    poste_id: uuid.UUID,
    user_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("caisse.poste.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    try:
        poste = postes.charger_poste_gere(db, courant, poste_id)
    except PosteIntrouvableError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_POSTE_INTROUVABLE
        ) from None
    postes.revoquer(db, courant, poste, user_id=user_id, contexte=_contexte(request))
    db.commit()
