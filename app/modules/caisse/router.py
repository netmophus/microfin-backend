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
  - aucune journée comptable ouverte (ouverture de caisse, lot 2 ; régularisation d'écart,
    lot 3) -> 422
  - poste sans compte de caisse rattaché         -> 422
  - écart au-delà du seuil sans motif (CA2)      -> 422
  - écart déjà validé / non significatif (CA2)   -> 422
  - compte de l'écart non rattaché (CA3)         -> 422

TRANSFERTS (sous-chantier 2, Lot 2) : gardé caisse.transfert.initier (POST /caisse/transferts) /
caisse.transfert.valider (POST .../reception) ; lecture (liste + fiche) acceptent l'une OU
l'autre (exige_une_de). Cloisonné à l'agence dans les DEUX cas (condition_perimetre, dans le
service) — jamais perimetre.reseau, aucun rôle de ce sous-chantier ne le porte. Le contrôle à
l'objet côté secondaire (poste = session ouverte de l'ACTEUR) est vérifié dans
`transferts.py`, jamais court-circuité ici : ce routeur délègue et traduit, il n'autorise rien
lui-même au-delà de la permission de route. Table de traduction dédiée : `_traduire_transfert`
(même patron que `tiers/router.py::_traduire`).
  - transfert hors périmètre ou inexistant                    -> 404 (IDOR)
  - poste soumis inexistant/inactif/hors agence                -> 404 (IDOR)
  - adjacence invalide, poste requis/inattendu                 -> 422
  - niveau non paramétré (coffre/principale) ou poste sans compte -> 422
  - acteur non titulaire de la session ouverte sur le poste     -> 422
  - compte de transit / d'écart de transfert non paramétré      -> 422
  - transfert déjà réceptionné                                  -> 422
  - double regard (receveur = envoyeur)                         -> 422
  - responsabilité des niveaux (sous-chantier 3) : caissier principal non désigné, mauvais
    caissier principal, ou responsable coffre non autorisé      -> 422
  - aucune journée comptable ouverte (chantier P1bis, lot 3)     -> 422

CAISSIER PRINCIPAL (sous-chantier 3, Lot B) : GET/PUT/DELETE gardés par caisse.principale.manage
(RESPONSABLE_AGENCE, SON agence — ÉGALITÉ STRICTE, même discipline que le coffre, voir
transferts.py). Hors périmètre -> 404 (IDOR), jamais 403. Désigner exige un utilisateur habilité
à l'agence ET détenant le rôle Caissier (refus clair sinon, table de traduction dédiée
`_traduire_caissier_principal`).
"""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.core.database import get_db
from app.modules.caisse import caissiers_principaux, postes
from app.modules.caisse import niveaux as niveaux_caisse
from app.modules.caisse import transferts as transferts_caisse
from app.modules.caisse.ecart_operations import RattachementEcartManquantError
from app.modules.caisse.models import CaisseParametres, CaisseSession, Poste, Transfert
from app.modules.caisse.niveaux import NiveauInvalideError
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
    AgenceNiveauxCaisse,
    AssignationCreation,
    CaissierPrincipalAgence,
    CompteRattachementEcart,
    CreationPoste,
    DesignationCaissierPrincipal,
    FermetureSession,
    LigneSessionAValider,
    LigneSessionManquante,
    ModificationParametresCaisse,
    ModificationPoste,
    NiveauCaisseItem,
    OuvertureSession,
    PageSessionsAValider,
    PageSessionsManquantes,
    PageTransferts,
    ParametresCaisse,
    PosteAssigne,
    PosteCaisse,
    RattachementComptePoste,
    RattachementNiveauCaisse,
    ReceptionTransfert,
    SessionCaisse,
    TransfertCreation,
    TransfertDetail,
    UtilisateurAssigne,
)
from app.modules.caisse.service import (
    TAILLE_PAGE_DEFAUT,
    TAILLE_PAGE_MAX,
    EcartNonSignificatifError,
    JourneeFermeeError,
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
from app.modules.comptabilite.comptes import CompteHorsCaisseError, CompteInvalideRattachementError
from app.modules.comptabilite.journee import AucuneJourneeOuverteError
from app.modules.comptabilite.models import Account
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant, exige, exige_une_de
from app.modules.security.models import User
from app.modules.security.router import _contexte

router = APIRouter(tags=["caisse"])

MESSAGE_SESSION_INTROUVABLE = "Session de caisse introuvable."
MESSAGE_MANQUANT_SEUL = "Seul manquant=true est pris en charge pour l'instant."
MESSAGE_POSTE_INTROUVABLE = "Poste de caisse introuvable."
MESSAGE_AGENCE_INTROUVABLE = "Agence introuvable."
MESSAGE_TRANSFERT_INTROUVABLE = "Transfert introuvable."


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


def _compte_rattachement_ecart(
    db: Session, account_id: uuid.UUID | None
) -> CompteRattachementEcart | None:
    if account_id is None:
        return None
    compte = db.get(Account, account_id)
    if compte is None:
        return None
    return CompteRattachementEcart(account_number=compte.account_number, name=compte.name)


def _vers_schema_parametres(db: Session, config: CaisseParametres) -> ParametresCaisse:
    return ParametresCaisse(
        seuil_tolerance=config.seuil_tolerance,
        compte_ecart_manquant=_compte_rattachement_ecart(db, config.compte_ecart_manquant_id),
        compte_ecart_excedent=_compte_rattachement_ecart(db, config.compte_ecart_excedent_id),
        compte_transit=_compte_rattachement_ecart(db, config.compte_transit_id),
        compte_ecart_transfert_manquant=_compte_rattachement_ecart(
            db, config.compte_ecart_transfert_manquant_id
        ),
        compte_ecart_transfert_excedent=_compte_rattachement_ecart(
            db, config.compte_ecart_transfert_excedent_id
        ),
        is_provisional=config.is_provisional,
    )


@router.get("/caisse/sessions/mes-postes", response_model=list[PosteAssigne])
def lister_mes_postes_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("caisse.session.open"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[PosteAssigne]:
    """Postes proposables à L'ACTEUR pour ouvrir une session (Bloc C) — SES postes assignés,
    actifs, dans SON agence courante. Gardé par `caisse.session.open` (que tout caissier
    détient déjà), jamais `caisse.poste.manage`/`compta.plan.manage` (gestion, hors de portée
    d'un caissier) : cet écran ne montre que ce qui le concerne, pas la liste de gestion.
    Distinct de `GET /caisse/postes`, réservé aux rôles de gestion."""
    return [
        PosteAssigne(id=p.id, code=p.code, libelle=p.libelle)
        for p in postes.lister_mes_postes(db, courant)
    ]


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
    except (JourneeFermeeError, SessionDejaOuverteError, RattachementManquantError) as erreur:
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
    """Le seuil de tolérance courant (CA2) et le rattachement de l'écart (CA3) — LECTURE élargie
    au caissier (caisse.session.close) : il doit savoir, AVANT de fermer, si son écart
    franchira le seuil et exigera un motif — ce n'est qu'un nombre, pas une donnée sensible.
    L'ÉCRITURE reste réservée compta.plan.manage seul (voir modifier_parametres_endpoint),
    même permission que les autres paramètres du Bloc 5 : c'est le comptable qui gère la
    config d'institution."""
    try:
        config = lire_parametres(db)
    except ParametrageManquantError as erreur:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(erreur)
        ) from None
    return _vers_schema_parametres(db, config)


@router.put("/caisse/parametres", response_model=ParametresCaisse)
def modifier_parametres_endpoint(
    corps: ModificationParametresCaisse,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.plan.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> ParametresCaisse:
    """Modifie le seuil et/ou le rattachement de l'écart — motif obligatoire, tracé avant/après
    (comme tout Bloc 5)."""
    try:
        config = lire_parametres(db)
    except ParametrageManquantError as erreur:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(erreur)
        ) from None
    try:
        config = modifier_parametres(
            db,
            config,
            seuil_tolerance=corps.seuil_tolerance,
            compte_ecart_manquant_number=corps.compte_ecart_manquant,
            compte_ecart_excedent_number=corps.compte_ecart_excedent,
            compte_transit_number=corps.compte_transit,
            compte_ecart_transfert_manquant_number=corps.compte_ecart_transfert_manquant,
            compte_ecart_transfert_excedent_number=corps.compte_ecart_transfert_excedent,
            motif=corps.motif,
            par=courant.user_id,
            contexte=_contexte(request),
        )
    except CompteInvalideRattachementError as erreur:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    db.commit()
    return _vers_schema_parametres(db, config)


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
    (vérifié dans le service, au niveau de l'objet). CA3 : pose AUSSI la pièce de
    régularisation, dans la MÊME transaction — si le comptable n'a pas encore rattaché le
    compte de l'écart, tout est refusé (422), y compris la trace de validation elle-même."""
    try:
        session = valider_ecart(db, courant, session_id, contexte=_contexte(request))
        db.commit()
    except SessionIntrouvableError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_SESSION_INTROUVABLE
        ) from None
    except (
        SessionDejaValideeError,
        EcartNonSignificatifError,
        RattachementEcartManquantError,
        AucuneJourneeOuverteError,
    ) as erreur:
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


# --- Niveaux caisse (chantier coffre-fort/caisses, sous-chantier 1, Bloc 1) ----------------
# Coffre / principale par agence — PAS le niveau secondaire, qui reste sur les postes
# ci-dessus. Institution entière, compta.plan.manage — même portée que les autres écrans
# Bloc 5 (voir caisse/niveaux.py).


def _vers_schema_niveaux(db: Session, agence: Agency) -> AgenceNiveauxCaisse:
    niveaux = niveaux_caisse.lire_niveaux(db, agence.id)
    return AgenceNiveauxCaisse(
        agency_id=agence.id,
        agency_nom=agence.name,
        niveaux=[
            NiveauCaisseItem(
                niveau=niveau,
                compte_caisse=(
                    CompteRattachementEcart(
                        account_number=compte.account_number, name=compte.name
                    )
                    if ligne is not None
                    and ligne.compte_caisse_id is not None
                    and (compte := db.get(Account, ligne.compte_caisse_id)) is not None
                    else None
                ),
            )
            for niveau, ligne in niveaux.items()
        ],
    )


@router.get("/caisse/niveaux", response_model=list[AgenceNiveauxCaisse])
def lister_niveaux_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.plan.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[AgenceNiveauxCaisse]:
    """Coffre/principale de CHAQUE agence active. Un niveau non paramétré (compte_caisse=None)
    est un état LISIBLE, pas une erreur — voir caisse/niveaux.py."""
    agences = db.execute(select(Agency).where(Agency.is_active).order_by(Agency.name)).scalars()
    return [_vers_schema_niveaux(db, a) for a in agences]


@router.patch(
    "/caisse/agences/{agency_id}/niveaux/{niveau}", response_model=AgenceNiveauxCaisse
)
def rattacher_niveau_endpoint(
    agency_id: uuid.UUID,
    niveau: str,
    corps: RattachementNiveauCaisse,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.plan.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> AgenceNiveauxCaisse:
    """Rattache (ou vide) le compte d'un niveau pour une agence. Le compte doit être un compte
    de saisie actif ET descendre de la rubrique 1011 (comptes.compte_caisse_valide) —
    contrainte propre à la caisse, refus clair sinon."""
    agence = db.get(Agency, agency_id)
    if agence is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_AGENCE_INTROUVABLE
        )
    try:
        niveaux_caisse.rattacher_niveau(
            db,
            agency_id,
            niveau,
            compte_caisse_number=corps.compte_caisse,
            motif=corps.motif,
            par=courant.user_id,
            contexte=_contexte(request),
        )
        db.commit()
    except NiveauInvalideError as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    except (CompteInvalideRattachementError, CompteHorsCaisseError) as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    return _vers_schema_niveaux(db, agence)


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


# --- Transferts (chantier coffre-fort/caisses, sous-chantier 2, Lot 2) -----------------------
# Mouvement de fonds entre deux niveaux ADJACENTS de caisse (coffre/principale/secondaire) d'une
# même agence — voir caisse/transferts.py pour l'adjacence, le double regard et le contrôle à
# l'objet côté secondaire (poste = session ouverte de l'ACTEUR). Ce routeur ne fait que déléguer
# et traduire : aucune autorisation n'est décidée ici au-delà de la permission de route.


def _vers_schema_transfert(db: Session, transfert: Transfert) -> TransfertDetail:
    envoyeur = aliased(User)
    receveur = aliased(User)
    compte_source = aliased(Account)
    compte_destination = aliased(Account)
    nom_envoyeur = func.concat_ws(" ", envoyeur.first_name, envoyeur.last_name)
    nom_receveur = func.concat_ws(" ", receveur.first_name, receveur.last_name)
    (
        agency_nom,
        compte_source_number,
        compte_destination_number,
        envoye_par_nom,
        receptionne_par_nom,
    ) = db.execute(
        select(
            Agency.name,
            compte_source.account_number,
            compte_destination.account_number,
            nom_envoyeur,
            nom_receveur,
        )
        .select_from(Transfert)
        .join(Agency, Agency.id == Transfert.agency_id)
        .join(compte_source, compte_source.id == Transfert.compte_source_id)
        .join(compte_destination, compte_destination.id == Transfert.compte_destination_id)
        .join(envoyeur, envoyeur.id == Transfert.envoye_par)
        .outerjoin(receveur, receveur.id == Transfert.receptionne_par)
        .where(Transfert.id == transfert.id)
    ).one()
    return TransfertDetail(
        id=transfert.id,
        agency_id=transfert.agency_id,
        agency_nom=agency_nom,
        niveau_source=transfert.niveau_source,
        niveau_destination=transfert.niveau_destination,
        compte_source_number=compte_source_number,
        compte_destination_number=compte_destination_number,
        montant_envoye=transfert.montant_envoye,
        montant_compte=transfert.montant_compte,
        statut=transfert.statut,
        envoye_par_nom=envoye_par_nom,
        envoye_le=transfert.envoye_le,
        # None tant que non réceptionné : concat_ws(' ', NULL, NULL) rendrait '' (pas NULL) sur
        # la jointure externe — on tranche depuis l'objet ORM, pas depuis le résultat SQL brut.
        receptionne_par_nom=(
            receptionne_par_nom if transfert.receptionne_par is not None else None
        ),
        receptionne_le=transfert.receptionne_le,
        motif=transfert.motif,
    )


def _traduire_transfert(erreur: Exception) -> HTTPException:
    """Traduit une erreur de `transferts.py` en réponse HTTP. Un seul endroit pour cette table
    (même patron que `tiers/router.py::_traduire`)."""
    if isinstance(erreur, transferts_caisse.TransfertIntrouvableError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_TRANSFERT_INTROUVABLE
        )
    if isinstance(erreur, transferts_caisse.PosteIntrouvableError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_POSTE_INTROUVABLE
        )
    if isinstance(
        erreur,
        (
            transferts_caisse.AdjacenceInvalideError,
            transferts_caisse.PosteRequisError,
            transferts_caisse.PosteInattenduError,
            transferts_caisse.CompteNonParametreError,
            transferts_caisse.SessionCaissierRequiseError,
            transferts_caisse.MontantInvalideError,
            transferts_caisse.CompteTransitNonParametreError,
            transferts_caisse.CompteEcartTransfertNonParametreError,
            transferts_caisse.TransfertDejaReceptionneError,
            transferts_caisse.DoubleRegardError,
            # Sous-chantier 3 : responsabilité des niveaux — les endpoints existent déjà
            # (sous-chantier 2, Lot 2) et appellent ces mêmes fonctions ; sans cette entrée, un
            # refus légitime (coffre/principale non autorisés) remonterait en 500, pas en 422.
            # Nécessaire dès le Lot A, pas différé au Lot B.
            transferts_caisse.CaissierPrincipalNonDesigneError,
            transferts_caisse.CaissierPrincipalRequisError,
            transferts_caisse.ResponsableCoffreRequisError,
            # Chantier P1bis, lot 3 : aucune journée comptable ouverte (_jour -> initiation ET
            # réception) — sans cette entrée, ce refus légitime remonterait en 500, pas en 422.
            AucuneJourneeOuverteError,
        ),
    ):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        )
    raise erreur


@router.post(
    "/caisse/transferts", response_model=TransfertDetail, status_code=status.HTTP_201_CREATED
)
def initier_transfert_endpoint(
    corps: TransfertCreation,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("caisse.transfert.initier"))],
    db: Annotated[Session, Depends(get_db)],
) -> TransfertDetail:
    """Initie un transfert pour l'agence COURANTE de l'acteur. Côté « secondaire », l'acteur
    doit être le caissier titulaire de la session ouverte sur le poste visé (contrôlé dans
    `transferts.py`, jamais ici)."""
    try:
        transfert = transferts_caisse.initier_transfert(
            db,
            courant,
            niveau_source=corps.niveau_source,
            niveau_destination=corps.niveau_destination,
            poste_id=corps.poste_id,
            montant_envoye=corps.montant_envoye,
            motif=corps.motif,
            contexte=_contexte(request),
        )
        db.commit()
    except Exception as erreur:
        db.rollback()
        raise _traduire_transfert(erreur) from None
    return _vers_schema_transfert(db, transfert)


@router.post("/caisse/transferts/{transfert_id}/reception", response_model=TransfertDetail)
def receptionner_transfert_endpoint(
    transfert_id: uuid.UUID,
    corps: ReceptionTransfert,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("caisse.transfert.valider"))],
    db: Annotated[Session, Depends(get_db)],
) -> TransfertDetail:
    """Réceptionne un transfert « en transit » — double regard (receveur != envoyeur) et,
    côté « secondaire », caissier titulaire de la session ouverte sur le poste visé : les deux
    contrôlés dans `transferts.py`, jamais ici."""
    try:
        transfert = transferts_caisse.receptionner_transfert(
            db,
            courant,
            transfert_id,
            montant_compte=corps.montant_compte,
            contexte=_contexte(request),
        )
        db.commit()
    except Exception as erreur:
        db.rollback()
        raise _traduire_transfert(erreur) from None
    return _vers_schema_transfert(db, transfert)


@router.get("/caisse/transferts", response_model=PageTransferts)
def lister_transferts_endpoint(
    courant: Annotated[
        UtilisateurCourant,
        Depends(exige_une_de("caisse.transfert.initier", "caisse.transfert.valider")),
    ],
    db: Annotated[Session, Depends(get_db)],
    statut: Annotated[
        Literal["en_transit", "receptionne", "tous"],
        Query(
            description=(
                "Par défaut : seulement les transferts en transit. « tous » pour l'historique "
                "(besoin d'audit, Lot 2c) — un transfert réceptionné reste consultable."
            )
        ),
    ] = "en_transit",
    niveau: Annotated[
        str | None, Query(description="Filtre sur le niveau source OU destination.")
    ] = None,
    agency_id: Annotated[uuid.UUID | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    taille: Annotated[int, Query(ge=1, le=TAILLE_PAGE_MAX)] = TAILLE_PAGE_DEFAUT,
) -> PageTransferts:
    """Transferts dans le périmètre de l'acteur (agence) — en transit par défaut (la file
    d'attente à réceptionner). `statut=tous` lève le filtre (historique, jamais atteignable en
    omettant le paramètre : le défaut reste « en transit », un client doit le demander
    explicitement). `agency_id`/`niveau` s'AJOUTENT au cloisonnement, ne le remplacent jamais.
    Pagination serveur (même discipline que les listes de sessions)."""
    resultat = transferts_caisse.lister_transferts(
        db,
        courant,
        statut=None if statut == "tous" else statut,
        niveau=niveau,
        agency_id=agency_id,
        page=page,
        taille=taille,
    )
    return PageTransferts(
        lignes=[
            TransfertDetail(
                id=ligne.id,
                agency_id=ligne.agency_id,
                agency_nom=ligne.agency_nom,
                niveau_source=ligne.niveau_source,
                niveau_destination=ligne.niveau_destination,
                compte_source_number=ligne.compte_source_number,
                compte_destination_number=ligne.compte_destination_number,
                montant_envoye=ligne.montant_envoye,
                montant_compte=ligne.montant_compte,
                statut=ligne.statut,
                envoye_par_nom=ligne.envoye_par_nom,
                envoye_le=ligne.envoye_le,
                receptionne_par_nom=ligne.receptionne_par_nom,
                receptionne_le=ligne.receptionne_le,
                motif=ligne.motif,
            )
            for ligne in resultat.lignes
        ],
        total=resultat.total,
        page=resultat.page,
        taille=resultat.taille,
    )


@router.get("/caisse/transferts/{transfert_id}", response_model=TransfertDetail)
def lire_transfert_endpoint(
    transfert_id: uuid.UUID,
    courant: Annotated[
        UtilisateurCourant,
        Depends(exige_une_de("caisse.transfert.initier", "caisse.transfert.valider")),
    ],
    db: Annotated[Session, Depends(get_db)],
) -> TransfertDetail:
    """Un transfert, dans le périmètre de l'acteur — hors périmètre ou inexistant -> 404
    (IDOR, jamais 403)."""
    try:
        transfert = transferts_caisse.lire_transfert(db, courant, transfert_id)
    except transferts_caisse.TransfertIntrouvableError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_TRANSFERT_INTROUVABLE
        ) from None
    return _vers_schema_transfert(db, transfert)


# --- Caissier principal (sous-chantier 3, Lot B) ----------------------------------------------
# Désignation de LA personne responsable de la caisse principale d'une agence — organisationnel,
# jamais comptable (voir caissiers_principaux.py). ÉGALITÉ STRICTE d'agence (pas
# condition_perimetre), même discipline que le coffre dans transferts.py : un rôle réseau ne
# gère pas cette agence au quotidien.


def _charger_agence_geree(
    db: Session, courant: UtilisateurCourant, agency_id: uuid.UUID
) -> Agency:
    """L'agence, SEULEMENT si c'est celle de l'acteur — jamais condition_perimetre (qui
    tolérerait voit_tout). Hors périmètre ou inexistante -> même 404 (IDOR, on ne distingue
    pas « n'existe pas » de « n'est pas la vôtre »)."""
    if courant.agency_id != agency_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_AGENCE_INTROUVABLE
        )
    agence = db.get(Agency, agency_id)
    if agence is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_AGENCE_INTROUVABLE
        )
    return agence


def _vers_schema_caissier_principal(db: Session, agence: Agency) -> CaissierPrincipalAgence:
    designation = caissiers_principaux.lire(db, agence.id)
    caissier = None
    if designation is not None:
        utilisateur = db.get(User, designation.user_id)
        if utilisateur is not None:
            caissier = _vers_schema_utilisateur(utilisateur)
    return CaissierPrincipalAgence(
        agency_id=agence.id, agency_nom=agence.name, caissier_principal=caissier
    )


def _traduire_caissier_principal(erreur: Exception) -> HTTPException:
    """Traduit une erreur de `caissiers_principaux.py` en réponse HTTP. Un seul endroit pour
    cette table (même patron que `_traduire_transfert`)."""
    if isinstance(
        erreur,
        (
            caissiers_principaux.UtilisateurHorsPerimetreError,
            caissiers_principaux.RoleCaissierRequisError,
        ),
    ):
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur))
    raise erreur


@router.get(
    "/caisse/agences/{agency_id}/caissier-principal", response_model=CaissierPrincipalAgence
)
def lire_caissier_principal_endpoint(
    agency_id: uuid.UUID,
    courant: Annotated[UtilisateurCourant, Depends(exige("caisse.principale.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> CaissierPrincipalAgence:
    """`caissier_principal` à `null` est un état LISIBLE (aucune désignation encore faite),
    jamais une erreur."""
    agence = _charger_agence_geree(db, courant, agency_id)
    return _vers_schema_caissier_principal(db, agence)


@router.put(
    "/caisse/agences/{agency_id}/caissier-principal", response_model=CaissierPrincipalAgence
)
def designer_caissier_principal_endpoint(
    agency_id: uuid.UUID,
    corps: DesignationCaissierPrincipal,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("caisse.principale.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> CaissierPrincipalAgence:
    """Désigne (ou remplace) LE caissier principal — MOTIF obligatoire. Refuse si l'utilisateur
    n'est pas habilité à cette agence, ou ne détient pas le rôle Caissier (422, message clair)."""
    agence = _charger_agence_geree(db, courant, agency_id)
    try:
        caissiers_principaux.designer(
            db,
            agency_id,
            corps.user_id,
            motif=corps.motif,
            par=courant.user_id,
            contexte=_contexte(request),
        )
        db.commit()
    except Exception as erreur:
        db.rollback()
        raise _traduire_caissier_principal(erreur) from None
    return _vers_schema_caissier_principal(db, agence)


@router.delete(
    "/caisse/agences/{agency_id}/caissier-principal", status_code=status.HTTP_204_NO_CONTENT
)
def retirer_caissier_principal_endpoint(
    agency_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("caisse.principale.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    """Retire la désignation — idempotent : aucune désignation -> ne fait rien (même discipline
    que `postes.revoquer`)."""
    _charger_agence_geree(db, courant, agency_id)
    caissiers_principaux.retirer(db, agency_id, par=courant.user_id, contexte=_contexte(request))
    db.commit()
