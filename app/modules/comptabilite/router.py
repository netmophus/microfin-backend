"""Endpoints HTTP — Plan de comptes : Bloc 1 (consultation + gestion unitaire) + Bloc 2
(import/export CSV en masse) + Rapports (grand livre, balance) + Saisie manuelle d'écriture OD
(chantier P1, lot 1) + Clôture TECHNIQUE d'exercice (chantier P1, lot b1) + Affectation du
résultat (chantier P1, lot b2a) + À-nouveaux (chantier P1, lot b2b) + États financiers, bilan et
compte de résultat (chantier P1, dernier lot) + Journée comptable (chantier P1bis, lots 1-3) +
Calendrier des jours fériés (chantier P1bis, lot 4a).

TABLE DES ERREURS (un seul endroit) :
  - permission absente                          -> 403 (exige(), en amont)
  - compte/écriture/exercice/mapping inexistant(e) -> 404
  - numéro déjà utilisé / classe-numéro incohérente / parent invalide -> 422, message humain
  - garde-fou (système, mouvementé, enfants actifs) -> 422, message humain (service.py)
  - fichier CSV invalide / anomalies de validation -> 422, message humain (plan.py)
  - fichier changé entre l'aperçu et la confirmation -> 422, empreintes différentes
  - écriture : exercice fermé, pièce incomplète/déséquilibrée, déjà validée/contre-passée,
    compte de saisie invalide -> 422, message humain (ecritures.py / ecritures_od.py)
  - clôture : exercice déjà clos, brouillons en attente, rien à clôturer, compte 591
    introuvable -> 422, message humain (cloture_exercice.py)
  - affectation : exercice non clos, déjà affecté, rien à affecter, ventilation incorrecte,
    compte de destination introuvable -> 422, message humain (affectation_resultat.py)
  - à-nouveaux : exercice source non clos, exercice suivant absent/pas ouvert, déjà générés,
    rien à reporter, bilan déséquilibré, journal AN introuvable -> 422, message humain
    (a_nouveaux.py)
  - états financiers : aucune, bilan/compte de résultat se calculent toujours (un déséquilibre
    ou des comptes non mappés sont SIGNALÉS dans la réponse, jamais une erreur HTTP)
  - journée comptable : déjà une journée ouverte, date déjà utilisée, aucune journée ouverte à
    clôturer, caisse(s) encore ouverte(s) (chantier P1bis lot 2) -> 422, message humain
    (journee.py)
  - jour férié déjà saisi pour cette date -> 422 ; jour férié inexistant (suppression) -> 404
    (chantier P1bis lot 4a, calendrier.py)

Lecture (+ export) -> compta.plan.read. Écriture (créer, modifier, sens, désactiver, import
en 2 temps, mapping états financiers) -> compta.plan.manage. Rapports (grand livre, balance,
bilan, compte de résultat) -> compta.rapport.read. Saisie manuelle OD : lecture ->
compta.ecriture.read ; brouillon/validation/suppression -> compta.ecriture.post ;
contre-passation -> compta.ecriture.reverse (permissions existantes, déjà attribuées à
COMPTABLE — seed_security.py). Exercices (liste, clôture, affectation, à-nouveaux — aperçus
compris) -> compta.exercice.manage, lecture et écriture confondues : ces actes sont de la
gestion, pas une simple consultation. Journée comptable : CONSULTATION (liste, courante) ->
compta.journee.read ; OUVERTURE/CLÔTURE -> compta.journee.manage — scindées (réorganisation
RBAC post lot 4b, acte D'EXPLOITATION réservé à ADMIN_FONCTIONNEL, read resté à COMPTABLE
ET ADMIN_FONCTIONNEL, voir seed_security.py). Calendrier des jours fériés (liste, ajout,
suppression) -> compta.calendrier.manage, permission DISTINCTE de compta.journee.* :
paramétrage annuel, pas le cycle quotidien, même s'il l'alimente (prochaine_date_ouvree).
"""

import uuid
from datetime import date
from typing import Annotated, Literal, cast

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.audit.service import ecrire_audit
from app.modules.comptabilite import (
    a_nouveaux,
    affectation_resultat,
    calendrier,
    cloture_exercice,
    comptes,
    ecritures,
    ecritures_od,
    etats_financiers,
    journee,
    plan,
    rapports,
)
from app.modules.comptabilite.comptes import (
    TAILLE_PAGE_DEFAUT,
    TAILLE_PAGE_MAX,
    ChampInvalideError,
    CompteInvalideRattachementError,
    FiltresComptes,
    NumeroDejaUtiliseError,
    ParentIntrouvableError,
)
from app.modules.comptabilite.ecritures_od import (
    TAILLE_PAGE_DEFAUT as TAILLE_PAGE_ECRITURES_DEFAUT,
)
from app.modules.comptabilite.ecritures_od import (
    TAILLE_PAGE_MAX as TAILLE_PAGE_ECRITURES_MAX,
)
from app.modules.comptabilite.ecritures_od import JournalODIntrouvableError
from app.modules.comptabilite.models import (
    Account,
    Exercice,
    FinancialStatementMapping,
    JourFerie,
    JournalEntry,
    JourneeComptable,
)
from app.modules.comptabilite.rapports import TAILLE_PAGE_GRAND_LIVRE, CompteNonSaisieError
from app.modules.comptabilite.schemas import (
    AffectationResultatResultat,
    ANouveauxResultat,
    ApercuAffectation,
    ApercuANouveaux,
    ApercuCloture,
    ApercuImportComptes,
    Balance,
    BilanSchema,
    BrouillonBloquantSchema,
    ChangementSens,
    ClotureExerciceResultat,
    CompteApercuSchema,
    CompteDetail,
    CompteNonMappeSchema,
    CompteRapport,
    CompteResultatSchema,
    CompteResume,
    CompteSelecteur,
    CompteSelecteurRapport,
    ConfirmationImportComptes,
    CreationCompte,
    CreationEcritureOD,
    CreationJourFerie,
    DesactivationCompte,
    DiffChampSchema,
    EcritureODDetail,
    EcritureODResume,
    ExerciceResume,
    JourFerieResume,
    JourneeComptableResume,
    JourneeCouranteSchema,
    LigneANouveauxSchema,
    LigneBalance,
    LigneEcritureODDetail,
    LigneGrandLivre,
    LigneMappingAdmin,
    LignePosteSchema,
    LigneResultatCloture,
    ModificationCompte,
    ModificationMapping,
    OuvertureJournee,
    PageComptes,
    PageEcrituresOD,
    PageGrandLivre,
    VentilationAffectation,
    VerrouillageSaisie,
)
from app.modules.comptabilite.service import ModificationInterditeError
from app.modules.security.autorisation import UtilisateurCourant, exige
from app.modules.security.models import User
from app.modules.security.router import _contexte

router = APIRouter(prefix="/comptabilite", tags=["comptabilite"])

MESSAGE_INTROUVABLE = "Compte du plan introuvable."
MESSAGE_FICHIER_CHANGE = (
    "Le fichier a changé depuis l'aperçu. Relancez l'aperçu avant de confirmer."
)


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


# --- Import / export CSV (Bloc 2) -------------------------------------------------------
# AVANT /comptes/{compte_id} : sinon Starlette matcherait "export"/"import" comme un compte_id
# (routes évaluées dans l'ordre de déclaration, la première forme qui matche gagne).


def _vers_apercu(c: plan.CompteApercu) -> CompteApercuSchema:
    return CompteApercuSchema(
        account_number=c.account_number,
        name=c.name,
        diffs=[DiffChampSchema(champ=d.champ, avant=d.avant, apres=d.apres) for d in c.diffs],
    )


@router.post("/comptes/import/apercu", response_model=ApercuImportComptes)
def apercu_import_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.plan.manage"))],
    db: Annotated[Session, Depends(get_db)],
    fichier: Annotated[UploadFile, File(description="CSV du plan de comptes (« ; », UTF-8).")],
) -> ApercuImportComptes:
    """Lit et valide le fichier, SANS RIEN ÉCRIRE. Anomalies -> import bloqué (liste complète).
    Fichier propre -> diff compte par compte (créations, modifications avec avant/après) +
    une empreinte à reprendre telle quelle pour confirmer."""
    contenu = fichier.file.read()
    try:
        lignes = plan.lire_bytes(contenu)
    except plan.FichierInvalideError as erreur:
        raise _422(erreur) from None

    anomalies = plan.valider(lignes) or (
        plan.conflits_de_sens(db, lignes) + plan.conflits_de_nature(db, lignes)
    )
    if anomalies:
        return ApercuImportComptes(anomalies=[str(a) for a in anomalies])

    rapport = plan.previsualiser(db, lignes)
    return ApercuImportComptes(
        empreinte=plan.empreinte(contenu),
        a_creer=[_vers_apercu(c) for c in rapport.a_creer],
        a_modifier=[_vers_apercu(c) for c in rapport.a_modifier],
        inchanges=rapport.inchanges,
    )


@router.post("/comptes/import/confirmer", response_model=ConfirmationImportComptes)
def confirmer_import_endpoint(
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.plan.manage"))],
    db: Annotated[Session, Depends(get_db)],
    fichier: Annotated[UploadFile, File(description="LE MÊME fichier vu à l'aperçu.")],
    empreinte: Annotated[str, Form()],
    motif: Annotated[str, Form(min_length=3, max_length=500)],
    lever_provisoire: Annotated[
        bool, Form(description="Cette correction vaut validation définitive de l'expert.")
    ] = False,
) -> ConfirmationImportComptes:
    """Réécrit — exige la MÊME empreinte que l'aperçu (sinon un fichier différent aurait pu se
    substituer entre-temps) et un motif tracé. Tout ou rien, comme l'aperçu."""
    contenu = fichier.file.read()
    if plan.empreinte(contenu) != empreinte:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=MESSAGE_FICHIER_CHANGE
        )

    try:
        lignes = plan.lire_bytes(contenu)
        rapport = plan.importer_lignes(
            db, lignes, courant.user_id, lever_provisoire=lever_provisoire
        )
    except plan.FichierInvalideError as erreur:
        db.rollback()
        raise _422(erreur) from None
    except plan.ImportRefuseError as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=" ; ".join(str(a) for a in erreur.anomalies),
        ) from None

    ecrire_audit(
        db,
        action="compta.plan.imported",
        contexte=_contexte(request),
        acteur_id=courant.user_id,
        resource_type=comptes.RESSOURCE,
        new_values={
            "crees": rapport.crees,
            "mis_a_jour": rapport.mis_a_jour,
            "lever_provisoire": lever_provisoire,
            "motif": motif,
        },
    )
    db.commit()
    return ConfirmationImportComptes(
        crees=rapport.crees, mis_a_jour=rapport.mis_a_jour, provisoire_leve=lever_provisoire
    )


@router.get("/comptes/export")
def exporter_comptes_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.plan.read"))],
    db: Annotated[Session, Depends(get_db)],
    inclure_inactifs: Annotated[
        bool, Query(description="Inclure les comptes désactivés.")
    ] = True,
) -> Response:
    contenu = plan.exporter_csv(db, inclure_inactifs=inclure_inactifs)
    return Response(
        content="\N{ZERO WIDTH NO-BREAK SPACE}" + contenu,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="plan_comptable.csv"'},
    )


@router.get("/comptes/selecteur", response_model=list[CompteSelecteur])
def selecteur_comptes_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.plan.read"))],
    db: Annotated[Session, Depends(get_db)],
    q: Annotated[str | None, Query(description="Recherche — numéro ou libellé.")] = None,
) -> list[CompteSelecteur]:
    """Comptes proposables comme rattachement (Bloc 5, autres modules) — TOUJOURS de saisie et
    actifs, jamais un compte de regroupement ni désactivé (comptes.lister_pour_selecteur)."""
    return [
        CompteSelecteur(id=c.id, account_number=c.account_number, name=c.name)
        for c in comptes.lister_pour_selecteur(db, q)
    ]


@router.get("/comptes/selecteur-rapport", response_model=list[CompteSelecteurRapport])
def selecteur_rapport_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.rapport.read"))],
    db: Annotated[Session, Depends(get_db)],
    q: Annotated[str | None, Query(description="Recherche — numéro ou libellé.")] = None,
) -> list[CompteSelecteurRapport]:
    """Comptes proposables pour le grand livre — TOUJOURS de saisie, actifs OU désactivés
    (comptes.lister_pour_rapport) : un compte désactivé garde son historique consultable,
    à la différence du sélecteur de rattachement (/comptes/selecteur). is_active exposé pour
    que l'écran le signale clairement, y compris une fois le sélecteur refermé."""
    return [
        CompteSelecteurRapport(
            id=c.id, account_number=c.account_number, name=c.name, is_active=c.is_active
        )
        for c in comptes.lister_pour_rapport(db, q)
    ]


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


@router.post("/comptes/{compte_id}/verrouiller-saisie", response_model=CompteDetail)
def verrouiller_saisie_endpoint(
    compte_id: uuid.UUID,
    corps: VerrouillageSaisie,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.plan.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> CompteDetail:
    """Ferme la saisie d'un compte (is_posting -> FALSE), MÊME s'il est système ou mouvementé —
    voir comptes.verrouiller_saisie. Jamais l'inverse : pas d'endpoint pour rouvrir."""
    compte = _charger(db, compte_id)
    try:
        comptes.verrouiller_saisie(db, compte, corps.motif, courant.user_id, _contexte(request))
        db.commit()
    except ModificationInterditeError as erreur:
        db.rollback()
        raise _422(erreur) from None
    ligne = comptes.lire(db, compte_id)
    assert ligne is not None
    return _vers_detail(*ligne)


# --- Rapports (R1 grand livre, R2 balance) — lecture pure, compta.rapport.read --------------


MESSAGE_COMPTE_RAPPORT_INTROUVABLE = "Compte introuvable."


@router.get("/grand-livre", response_model=PageGrandLivre)
def grand_livre_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.rapport.read"))],
    db: Annotated[Session, Depends(get_db)],
    compte_id: uuid.UUID,
    date_debut: Annotated[date | None, Query(description="Borne basse (incluse).")] = None,
    date_fin: Annotated[date | None, Query(description="Borne haute (incluse).")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
) -> PageGrandLivre:
    compte = db.get(Account, compte_id)
    if compte is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_COMPTE_RAPPORT_INTROUVABLE
        )
    try:
        resultat = rapports.grand_livre(
            db, compte, date_debut=date_debut, date_fin=date_fin, page=page
        )
    except CompteNonSaisieError as erreur:
        raise _422(erreur) from None
    return PageGrandLivre(
        compte=CompteRapport(
            account_number=compte.account_number, name=compte.name, is_active=compte.is_active
        ),
        solde_ouverture=resultat.solde_ouverture,
        lignes=[
            LigneGrandLivre(
                entry_date=ligne.entry_date,
                entry_number=ligne.entry_number,
                journal_code=ligne.journal_code,
                label=ligne.label,
                side=ligne.side,
                amount=ligne.amount,
                solde_cumule=ligne.solde_cumule,
            )
            for ligne in resultat.lignes
        ],
        total=resultat.total,
        page=page,
        taille=TAILLE_PAGE_GRAND_LIVRE,
    )


@router.get("/balance", response_model=Balance)
def balance_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.rapport.read"))],
    db: Annotated[Session, Depends(get_db)],
    date_debut: Annotated[date | None, Query(description="Borne basse (incluse).")] = None,
    date_fin: Annotated[date | None, Query(description="Borne haute (incluse).")] = None,
    inclure_sans_mouvement: Annotated[
        bool, Query(description="Inclure les comptes sans mouvement sur la période.")
    ] = False,
) -> Balance:
    resultat = rapports.balance(
        db,
        date_debut=date_debut,
        date_fin=date_fin,
        inclure_sans_mouvement=inclure_sans_mouvement,
    )
    return Balance(
        date_debut=date_debut,
        date_fin=date_fin,
        lignes=[
            LigneBalance(
                account_number=ligne.compte.account_number,
                name=ligne.compte.name,
                solde_ouverture=ligne.solde_ouverture,
                total_debit=ligne.total_debit,
                total_credit=ligne.total_credit,
                solde_cloture=ligne.solde_cloture,
            )
            for ligne in resultat.lignes
        ],
        total_debit=resultat.total_debit,
        total_credit=resultat.total_credit,
        equilibree=(resultat.total_debit == resultat.total_credit),
    )


# --- Saisie manuelle d'écriture (OD), chantier P1 lot 1 -------------------------------------
# Journal OD UNIQUEMENT — jamais un champ accepté, voir ecritures_od.py. Permissions existantes,
# déjà attribuées à COMPTABLE (seed_security.py) : compta.ecriture.read/post/reverse.


MESSAGE_ECRITURE_INTROUVABLE = "Écriture introuvable."


def _vers_resume_ecriture(resultat: ecritures_od.EcritureAvecTotaux) -> EcritureODResume:
    entry = resultat.entry
    return EcritureODResume(
        id=entry.id,
        entry_number=entry.entry_number,
        entry_date=entry.entry_date,
        description=entry.description,
        status=cast(Literal["brouillon", "validee"], entry.status),
        nb_lignes=resultat.nb_lignes,
        total_debit=resultat.total_debit,
        total_credit=resultat.total_credit,
        equilibree=(resultat.total_debit == resultat.total_credit),
        est_contre_passation=entry.reversal_of_id is not None,
        deja_contre_passee=resultat.deja_contre_passee,
    )


def _vers_detail_ecriture(db: Session, entry: JournalEntry) -> EcritureODDetail:
    resultat = ecritures_od.avec_totaux(db, entry)
    base = _vers_resume_ecriture(resultat)
    lignes = ecritures_od.lignes_avec_compte(db, entry.id)
    return EcritureODDetail(
        **base.model_dump(),
        lignes=[
            LigneEcritureODDetail(
                account_number=ligne.account_number,
                name=ligne.name,
                side=cast(Literal["D", "C"], ligne.side),
                amount=ligne.amount,
                label=ligne.label,
            )
            for ligne in lignes
        ],
    )


def _charger_ecriture_od(db: Session, entry_id: uuid.UUID) -> JournalEntry:
    entry = ecritures_od.charger_od(db, entry_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_ECRITURE_INTROUVABLE
        )
    return entry


@router.get("/ecritures", response_model=PageEcrituresOD)
def lister_ecritures_od_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.ecriture.read"))],
    db: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    taille: Annotated[
        int, Query(ge=1, le=TAILLE_PAGE_ECRITURES_MAX)
    ] = TAILLE_PAGE_ECRITURES_DEFAUT,
) -> PageEcrituresOD:
    """Les pièces du journal OD (saisie manuelle) — JAMAIS celles des autres journaux (CA/BQ/AN,
    pilotées par les modules métier), voir ecritures_od.py."""
    resultats, total = ecritures_od.lister_od(db, page=page, taille=taille)
    return PageEcrituresOD(
        lignes=[_vers_resume_ecriture(r) for r in resultats], total=total, page=page, taille=taille
    )


@router.get("/ecritures/{entry_id}", response_model=EcritureODDetail)
def lire_ecriture_od_endpoint(
    entry_id: uuid.UUID,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.ecriture.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> EcritureODDetail:
    entry = _charger_ecriture_od(db, entry_id)
    return _vers_detail_ecriture(db, entry)


@router.post("/ecritures", response_model=EcritureODDetail, status_code=status.HTTP_201_CREATED)
def creer_ecriture_od_endpoint(
    corps: CreationEcritureOD,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.ecriture.post"))],
    db: Annotated[Session, Depends(get_db)],
) -> EcritureODDetail:
    """Crée un BROUILLON dans le journal OD — le SEUL journal autorisé à la saisie manuelle
    (jamais un champ du corps de requête : voir ecritures_od.py). L'équilibre n'est pas exigé
    ici (brouillon = espace de travail) — voir POST .../validation."""
    try:
        journal_id = ecritures_od.journal_od_id(db)
        lignes = ecritures_od.resoudre_lignes(
            db,
            [
                ecritures_od.LigneSaisieNumero(
                    account_number=ligne.account_number,
                    side=ligne.side,
                    amount=ligne.amount,
                    label=ligne.label,
                )
                for ligne in corps.lignes
            ],
        )
        entry = ecritures.creer_brouillon(
            db,
            journal_id=journal_id,
            entry_date=corps.entry_date,
            description=corps.description,
            lignes=lignes,
            par=courant.user_id,
        )
        db.commit()
    except (
        JournalODIntrouvableError,
        CompteInvalideRattachementError,
        ecritures.AucunExerciceOuvertError,
        ecritures.LigneInvalideError,
        ecritures.CompteNonSaisissableError,
    ) as erreur:
        db.rollback()
        raise _422(erreur) from None
    return _vers_detail_ecriture(db, entry)


@router.post("/ecritures/{entry_id}/validation", response_model=EcritureODDetail)
def valider_ecriture_od_endpoint(
    entry_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.ecriture.post"))],
    db: Annotated[Session, Depends(get_db)],
) -> EcritureODDetail:
    """Bascule le brouillon en pièce VALIDÉE, IMMUABLE — le moteur exige l'équilibre et alloue
    le numéro (voir ecritures.valider)."""
    entry = _charger_ecriture_od(db, entry_id)
    try:
        ecritures.valider(db, entry, courant.user_id, contexte=_contexte(request))
        db.commit()
    except (
        ecritures.PieceDejaValideeError,
        ecritures.ExerciceError,
        ecritures.PieceIncompleteError,
        ecritures.PieceDesequilibreeError,
    ) as erreur:
        db.rollback()
        raise _422(erreur) from None
    return _vers_detail_ecriture(db, entry)


@router.post(
    "/ecritures/{entry_id}/contre-passation",
    response_model=EcritureODDetail,
    status_code=status.HTTP_201_CREATED,
)
def contre_passer_ecriture_od_endpoint(
    entry_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.ecriture.reverse"))],
    db: Annotated[Session, Depends(get_db)],
) -> EcritureODDetail:
    """Contre-passe une pièce VALIDÉE du journal OD : pose et valide la pièce inverse (D↔C),
    la pièce d'origine reste intacte. Renvoie la pièce inverse (nouvelle ressource, 201)."""
    entry = _charger_ecriture_od(db, entry_id)
    try:
        inverse = ecritures.contre_passer(db, entry, courant.user_id, contexte=_contexte(request))
        db.commit()
    except (
        ecritures.PieceNonValideeError,
        ecritures.PieceDejaContrePasseeError,
        ecritures.AucunExerciceOuvertError,
        journee.AucuneJourneeOuverteError,
    ) as erreur:
        db.rollback()
        raise _422(erreur) from None
    return _vers_detail_ecriture(db, inverse)


@router.delete("/ecritures/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def supprimer_ecriture_od_endpoint(
    entry_id: uuid.UUID,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.ecriture.post"))],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    """Supprime un BROUILLON (jamais une pièce validée — contre-passer, voir ecritures.py)."""
    entry = _charger_ecriture_od(db, entry_id)
    try:
        ecritures.supprimer_brouillon(db, entry)
        db.commit()
    except ecritures.PieceDejaValideeError as erreur:
        db.rollback()
        raise _422(erreur) from None


# --- Clôture d'exercice, chantier P1 lot (b1) ------------------------------------------------
# Clôture TECHNIQUE uniquement (comptes 6/7 -> 591). compta.exercice.manage pour les trois
# routes : consulter la liste des exercices ou l'aperçu de clôture est déjà un acte de gestion
# sur ce périmètre, pas une simple lecture comptable (à la différence de compta.rapport.read).


MESSAGE_EXERCICE_INTROUVABLE = "Exercice introuvable."


def _charger_exercice(db: Session, exercice_id: uuid.UUID) -> Exercice:
    exercice = db.get(Exercice, exercice_id)
    if exercice is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_EXERCICE_INTROUVABLE
        )
    return exercice


def _vers_resume_exercice(exercice: Exercice) -> ExerciceResume:
    return ExerciceResume(
        id=exercice.id,
        code=exercice.code,
        label=exercice.label,
        date_debut=exercice.date_debut,
        date_fin=exercice.date_fin,
        status=cast(Literal["ouvert", "clos"], exercice.status),
        resultat_affecte=exercice.resultat_affecte_at is not None,
        a_nouveaux_generes=exercice.a_nouveaux_generes_at is not None,
    )


@router.get("/exercices", response_model=list[ExerciceResume])
def lister_exercices_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.exercice.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[ExerciceResume]:
    return [_vers_resume_exercice(e) for e in cloture_exercice.lister_exercices(db)]


@router.get("/exercices/{exercice_id}/previsualisation-cloture", response_model=ApercuCloture)
def previsualiser_cloture_endpoint(
    exercice_id: uuid.UUID,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.exercice.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> ApercuCloture:
    """Dry-run obligatoire avant toute clôture : résultat calculé, détail par compte, brouillons
    bloquants éventuels. Ne pose rien, ne modifie rien."""
    exercice = _charger_exercice(db, exercice_id)
    apercu = cloture_exercice.previsualiser_cloture(db, exercice)
    return ApercuCloture(
        exercice=_vers_resume_exercice(exercice),
        resultat=apercu.resultat,
        compte_resultat=cloture_exercice.COMPTE_RESULTAT_INSTANCE,
        lignes=[
            LigneResultatCloture(
                account_number=ligne.account_number,
                name=ligne.name,
                account_class=ligne.account_class,
                total_debit=ligne.total_debit,
                total_credit=ligne.total_credit,
                side=cast(Literal["D", "C"], ligne.side),
                amount=ligne.amount,
            )
            for ligne in apercu.lignes
        ],
        brouillons_bloquants=[
            BrouillonBloquantSchema(
                entry_id=b.entry_id,
                journal_code=b.journal_code,
                entry_date=b.entry_date,
                description=b.description,
            )
            for b in apercu.brouillons_bloquants
        ],
        cloturable=apercu.cloturable,
    )


@router.post("/exercices/{exercice_id}/cloture", response_model=ClotureExerciceResultat)
def cloturer_exercice_endpoint(
    exercice_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.exercice.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> ClotureExerciceResultat:
    """Exécute la clôture technique : voir cloture_exercice.cloturer_exercice pour l'ordre et les
    refus possibles. Aucune confirmation supplémentaire côté API — l'aperçu (dry-run) ci-dessus
    est la confirmation attendue avant cet appel, portée par l'écran."""
    exercice = _charger_exercice(db, exercice_id)
    try:
        resultat = cloture_exercice.cloturer_exercice(
            db, exercice, courant.user_id, contexte=_contexte(request)
        )
        db.commit()
    except (
        cloture_exercice.ExerciceDejaClosError,
        cloture_exercice.BrouillonsBloquantsError,
        cloture_exercice.RienAClorerError,
        cloture_exercice.CompteResultatIntrouvableError,
        ecritures.ExerciceError,
        ecritures.CompteNonSaisissableError,
        ecritures.LigneInvalideError,
        ecritures.PieceIncompleteError,
        ecritures.PieceDesequilibreeError,
    ) as erreur:
        db.rollback()
        raise _422(erreur) from None
    entry_number = resultat.entry.entry_number
    assert entry_number is not None  # valider() l'alloue toujours avant de rendre la main
    return ClotureExerciceResultat(
        exercice=_vers_resume_exercice(exercice),
        entry_number=entry_number,
        resultat=resultat.resultat,
    )


# --- Affectation du résultat, chantier P1 lot b2a --------------------------------------------
# Ventilation à la main (591 -> réserves et/ou 58, jamais 592 — voir affectation_resultat.py).
# compta.exercice.manage, même raisonnement que la clôture : affecter un résultat est un acte de
# gestion, pas une simple lecture.


@router.get(
    "/exercices/{exercice_id}/previsualisation-affectation", response_model=ApercuAffectation
)
def previsualiser_affectation_endpoint(
    exercice_id: uuid.UUID,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.exercice.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> ApercuAffectation:
    """Dry-run : montant à affecter (lu depuis la pièce de clôture de CET exercice, jamais le
    solde courant de 591 — voir affectation_resultat.py), et si c'est déjà fait."""
    exercice = _charger_exercice(db, exercice_id)
    try:
        apercu = affectation_resultat.previsualiser_affectation(db, exercice)
    except affectation_resultat.PieceClotureAmbigueError as erreur:
        raise _422(erreur) from None
    return ApercuAffectation(
        exercice=_vers_resume_exercice(exercice),
        montant=apercu.montant,
        deja_affecte=apercu.deja_affecte,
        affectable=apercu.affectable,
    )


@router.post("/exercices/{exercice_id}/affectation", response_model=AffectationResultatResultat)
def affecter_resultat_endpoint(
    exercice_id: uuid.UUID,
    corps: VentilationAffectation,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.exercice.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> AffectationResultatResultat:
    """Exécute l'affectation : voir affectation_resultat.affecter_resultat pour l'ordre des
    contrôles et les refus possibles. Aucune confirmation supplémentaire côté API — l'aperçu
    (dry-run) ci-dessus est la confirmation attendue avant cet appel, portée par l'écran."""
    exercice = _charger_exercice(db, exercice_id)
    ventilation = affectation_resultat.VentilationResultat(
        reserve_generale=corps.reserve_generale,
        reserves_facultatives=corps.reserves_facultatives,
        autres_reserves=corps.autres_reserves,
        report_a_nouveau=corps.report_a_nouveau,
    )
    try:
        resultat = affectation_resultat.affecter_resultat(
            db, exercice, ventilation, courant.user_id, contexte=_contexte(request)
        )
        db.commit()
    except (
        affectation_resultat.ExerciceNonClosError,
        affectation_resultat.ResultatDejaAffecteError,
        affectation_resultat.RienAAffecterError,
        affectation_resultat.PieceClotureAmbigueError,
        affectation_resultat.VentilationIncorrecteError,
        affectation_resultat.CompteAffectationIntrouvableError,
        ecritures.ExerciceError,
        ecritures.CompteNonSaisissableError,
        ecritures.LigneInvalideError,
        ecritures.PieceIncompleteError,
        ecritures.PieceDesequilibreeError,
        journee.AucuneJourneeOuverteError,
    ) as erreur:
        db.rollback()
        raise _422(erreur) from None
    entry_number = resultat.entry.entry_number
    assert entry_number is not None  # valider() l'alloue toujours avant de rendre la main
    return AffectationResultatResultat(
        exercice=_vers_resume_exercice(exercice),
        entry_number=entry_number,
        montant=resultat.montant,
        ventilation=corps,
    )


# --- À-nouveaux, chantier P1 lot b2b -----------------------------------------------------------
# Report du bilan de clôture (classes 1-5) de l'exercice source vers l'exercice suivant, journal
# AN. compta.exercice.manage, même raisonnement que la clôture et l'affectation.


@router.get("/exercices/{exercice_id}/previsualisation-a-nouveaux", response_model=ApercuANouveaux)
def previsualiser_a_nouveaux_endpoint(
    exercice_id: uuid.UUID,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.exercice.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> ApercuANouveaux:
    """Dry-run : comptes à reporter, totaux, détection de l'exercice suivant et de son état
    (absent, pas ouvert, déjà pourvu) — voir a_nouveaux.py. Ne pose rien."""
    exercice = _charger_exercice(db, exercice_id)
    apercu = a_nouveaux.previsualiser_a_nouveaux(db, exercice)
    return ApercuANouveaux(
        exercice_source=_vers_resume_exercice(exercice),
        exercice_suivant=(
            _vers_resume_exercice(apercu.exercice_suivant)
            if apercu.exercice_suivant is not None
            else None
        ),
        lignes=[
            LigneANouveauxSchema(
                account_number=ligne.account_number,
                name=ligne.name,
                account_class=ligne.account_class,
                side=cast(Literal["D", "C"], ligne.side),
                amount=ligne.amount,
            )
            for ligne in apercu.lignes
        ],
        total_debit=apercu.total_debit,
        total_credit=apercu.total_credit,
        equilibre=apercu.equilibre,
        deja_genere=apercu.deja_genere,
        generable=apercu.generable,
    )


@router.post("/exercices/{exercice_id}/a-nouveaux", response_model=ANouveauxResultat)
def generer_a_nouveaux_endpoint(
    exercice_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.exercice.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> ANouveauxResultat:
    """Exécute la génération : voir a_nouveaux.generer_a_nouveaux pour l'ordre des contrôles et
    les refus possibles. Aucune confirmation supplémentaire côté API — l'aperçu (dry-run)
    ci-dessus est la confirmation attendue avant cet appel, portée par l'écran."""
    exercice = _charger_exercice(db, exercice_id)
    try:
        resultat = a_nouveaux.generer_a_nouveaux(
            db, exercice, courant.user_id, contexte=_contexte(request)
        )
        db.commit()
    except (
        a_nouveaux.ExerciceSourceNonClosError,
        a_nouveaux.ExerciceSuivantIntrouvableError,
        a_nouveaux.ExerciceSuivantNonOuvertError,
        a_nouveaux.ANouveauxDejaGeneresError,
        a_nouveaux.RienAReporterError,
        a_nouveaux.BilanDesequilibreError,
        a_nouveaux.JournalANIntrouvableError,
        ecritures.ExerciceError,
        ecritures.CompteNonSaisissableError,
        ecritures.LigneInvalideError,
        ecritures.PieceIncompleteError,
        ecritures.PieceDesequilibreeError,
    ) as erreur:
        db.rollback()
        raise _422(erreur) from None
    entry_number = resultat.entry.entry_number
    assert entry_number is not None  # valider() l'alloue toujours avant de rendre la main
    return ANouveauxResultat(
        exercice_suivant=_vers_resume_exercice(resultat.exercice_suivant),
        entry_number=entry_number,
        total=resultat.total,
    )


# --- États financiers : bilan + compte de résultat, chantier P1 dernier lot -------------------
# Lecture pure, compta.rapport.read — même périmètre que grand livre/balance.


def _vers_ligne_poste(ligne: etats_financiers.LignePoste) -> LignePosteSchema:
    return LignePosteSchema(
        poste_libelle=ligne.poste_libelle,
        poste_ordre=ligne.poste_ordre,
        masse=cast(
            Literal["ACTIF", "PASSIF", "CONTRA_ACTIF", "CHARGE", "PRODUIT"], ligne.masse
        ),
        montant=ligne.montant,
    )


def _vers_compte_non_mappe(compte: etats_financiers.CompteNonMappe) -> CompteNonMappeSchema:
    return CompteNonMappeSchema(
        account_number=compte.account_number,
        name=compte.name,
        account_class=compte.account_class,
        solde=compte.solde,
    )


@router.get("/etats/bilan", response_model=BilanSchema)
def bilan_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.rapport.read"))],
    db: Annotated[Session, Depends(get_db)],
    date_param: Annotated[
        date | None, Query(alias="date", description="Par défaut : aujourd'hui.")
    ] = None,
) -> BilanSchema:
    """Bilan à une date — solde cumulé depuis l'origine de chaque compte de bilan, CONTRA_ACTIF
    déduit de l'actif. Voir etats_financiers.bilan."""
    resultat = etats_financiers.bilan(db, date_param)
    return BilanSchema(
        date=resultat.date,
        actif=[_vers_ligne_poste(ligne) for ligne in resultat.actif],
        passif=[_vers_ligne_poste(ligne) for ligne in resultat.passif],
        total_actif_brut=resultat.total_actif_brut,
        total_contra_actif=resultat.total_contra_actif,
        total_actif_net=resultat.total_actif_net,
        total_passif=resultat.total_passif,
        ecart=resultat.ecart,
        equilibre=resultat.equilibre,
        comptes_non_mappes=[_vers_compte_non_mappe(c) for c in resultat.comptes_non_mappes],
    )


@router.get("/etats/compte-resultat", response_model=CompteResultatSchema)
def compte_resultat_endpoint(
    exercice_id: uuid.UUID,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.rapport.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> CompteResultatSchema:
    """Compte de résultat d'un exercice — agrégation 6/7 sur la période s'il est ouvert, résultat
    re-dérivé depuis la pièce de clôture (591) s'il est clos. Voir
    etats_financiers.compte_resultat."""
    exercice = _charger_exercice(db, exercice_id)
    resultat = etats_financiers.compte_resultat(db, exercice)
    return CompteResultatSchema(
        exercice=_vers_resume_exercice(exercice),
        date_debut=resultat.date_debut,
        date_fin=resultat.date_fin,
        exercice_clos=resultat.exercice_clos,
        charges=[_vers_ligne_poste(ligne) for ligne in resultat.charges],
        produits=[_vers_ligne_poste(ligne) for ligne in resultat.produits],
        total_charges=resultat.total_charges,
        total_produits=resultat.total_produits,
        resultat_net=resultat.resultat_net,
        source_resultat=cast(Literal["periode", "cloture"], resultat.source_resultat),
        comptes_non_mappes=[_vers_compte_non_mappe(c) for c in resultat.comptes_non_mappes],
    )


# --- Administration du mapping états financiers, compta.plan.manage ---------------------------


MESSAGE_MAPPING_INTROUVABLE = "Aucune ligne de mapping pour ce compte."


def _vers_ligne_mapping(compte: Account, mapping: FinancialStatementMapping) -> LigneMappingAdmin:
    return LigneMappingAdmin(
        account_id=mapping.account_id,
        account_number=compte.account_number,
        name=compte.name,
        account_class=compte.account_class,
        etat=cast(Literal["BILAN", "RESULTAT"], mapping.etat),
        masse=cast(
            Literal["ACTIF", "PASSIF", "CONTRA_ACTIF", "CHARGE", "PRODUIT", "MIXTE"],
            mapping.masse,
        ),
        poste_libelle=mapping.poste_libelle,
        poste_ordre=mapping.poste_ordre,
        gere_manuellement=mapping.gere_manuellement,
    )


@router.get("/etats/mapping", response_model=list[LigneMappingAdmin])
def lister_mapping_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.plan.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[LigneMappingAdmin]:
    return [
        _vers_ligne_mapping(compte, mapping)
        for compte, mapping in etats_financiers.lister_mapping(db)
    ]


@router.patch("/etats/mapping/{account_id}", response_model=LigneMappingAdmin)
def modifier_mapping_endpoint(
    account_id: uuid.UUID,
    corps: ModificationMapping,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.plan.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> LigneMappingAdmin:
    """Ajuste une ligne À LA MAIN — verrouille contre le seed (gere_manuellement = TRUE)."""
    try:
        mapping = etats_financiers.modifier_mapping(
            db,
            account_id,
            etat=corps.etat,
            masse=corps.masse,
            poste_libelle=corps.poste_libelle,
            poste_ordre=corps.poste_ordre,
            par=courant.user_id,
        )
        db.commit()
    except etats_financiers.MappingIntrouvableError as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_MAPPING_INTROUVABLE
        ) from erreur
    compte = db.get(Account, account_id)
    assert compte is not None
    return _vers_ligne_mapping(compte, mapping)


# --- Journée comptable, chantier P1bis lot 1 ---------------------------------------------------
# RÉORGANISATION RBAC (post lot 4b) : l'ouverture/fermeture de journée est un acte D'EXPLOITATION,
# pas comptable — compta.journee.read (consultation : liste, courante) est DISTINCTE de
# compta.journee.manage (ouverture, clôture). Ne concerne QUE l'API : la datation des opérations
# (lot 3, `comptabilite.journee.date_comptable_obligatoire`/`journee_ouverte`) est appelée côté
# SERVICE par les autres modules, jamais via ces permissions — aucun chemin métier n'en dépend.


def _noms_acteurs(db: Session, ids: set[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    """Résout un lot d'identifiants d'acteur en noms complets, UNE seule requête — jamais un
    aller-retour base par ligne d'historique."""
    ids_valides = {i for i in ids if i is not None}
    if not ids_valides:
        return {}
    nom_complet = func.concat_ws(" ", User.first_name, User.last_name)
    resultats = db.execute(
        select(User.id, nom_complet).where(User.id.in_(ids_valides))
    ).all()
    return {row[0]: row[1] for row in resultats}


def _vers_journee_resume(
    journee_obj: JourneeComptable, noms: dict[uuid.UUID, str]
) -> JourneeComptableResume:
    opened_by = journee_obj.opened_by
    closed_by = journee_obj.closed_by
    return JourneeComptableResume(
        id=journee_obj.id,
        date_comptable=journee_obj.date_comptable,
        status=cast(Literal["ouverte", "cloturee"], journee_obj.status),
        opened_at=journee_obj.opened_at,
        opened_par_nom=noms.get(opened_by) if opened_by is not None else None,
        closed_at=journee_obj.closed_at,
        closed_par_nom=noms.get(closed_by) if closed_by is not None else None,
    )


@router.get("/journees", response_model=list[JourneeComptableResume])
def lister_journees_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.journee.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[JourneeComptableResume]:
    journees = journee.lister_journees(db)
    ids = {j.opened_by for j in journees} | {j.closed_by for j in journees}
    noms = _noms_acteurs(db, ids)
    return [_vers_journee_resume(j, noms) for j in journees]


@router.get("/journees/courante", response_model=JourneeCouranteSchema)
def journee_courante_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.journee.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> JourneeCouranteSchema:
    """La journée ouverte (ou son absence) ET la prochaine date ouvrée proposée — tout ce dont
    le formulaire d'ouverture a besoin en un seul appel."""
    etat = journee.journee_courante(db)
    ids_acteurs: set[uuid.UUID | None] = (
        {etat.journee.opened_by, etat.journee.closed_by} if etat.journee else set()
    )
    noms = _noms_acteurs(db, ids_acteurs)
    return JourneeCouranteSchema(
        journee=_vers_journee_resume(etat.journee, noms) if etat.journee is not None else None,
        prochaine_date_proposee=etat.prochaine_date_proposee,
    )


@router.post(
    "/journees", response_model=JourneeComptableResume, status_code=status.HTTP_201_CREATED
)
def ouvrir_journee_endpoint(
    corps: OuvertureJournee,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.journee.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> JourneeComptableResume:
    try:
        nouvelle_journee = journee.ouvrir_journee(
            db, corps.date_comptable, courant.user_id, contexte=_contexte(request)
        )
        db.commit()
    except (journee.UneSeuleJourneeError, journee.JourneeExistanteError) as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    noms = _noms_acteurs(db, {nouvelle_journee.opened_by})
    return _vers_journee_resume(nouvelle_journee, noms)


@router.post("/journees/cloture", response_model=JourneeComptableResume)
def cloturer_journee_endpoint(
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.journee.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> JourneeComptableResume:
    """Clôture DÉFINITIVEMENT la journée ouverte — voir journee.cloturer_journee pour le détail
    (chantier P1bis lot 2 : refuse aussi s'il reste une session de caisse ouverte quelque part
    sur le réseau)."""
    try:
        journee_cloturee = journee.cloturer_journee(
            db, courant.user_id, contexte=_contexte(request)
        )
        db.commit()
    except (journee.AucuneJourneeOuverteError, journee.CaissesOuvertesError) as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    noms = _noms_acteurs(
        db, {journee_cloturee.opened_by, journee_cloturee.closed_by}
    )
    return _vers_journee_resume(journee_cloturee, noms)


# --- Calendrier des jours fériés, chantier P1bis lot 4a -----------------------------------------
# compta.calendrier.manage pour les trois routes, même raisonnement que compta.journee.manage :
# consulter la liste par année est déjà un acte de gestion sur ce périmètre.


MESSAGE_JOUR_FERIE_INTROUVABLE = "Ce jour férié n'existe pas."


def _vers_jour_ferie_resume(jour_ferie: JourFerie) -> JourFerieResume:
    return JourFerieResume(
        id=jour_ferie.id,
        date_feriee=jour_ferie.date_feriee,
        libelle=jour_ferie.libelle,
        created_at=jour_ferie.created_at,
    )


@router.get("/jours-feries", response_model=list[JourFerieResume])
def lister_jours_feries_endpoint(
    annee: Annotated[int, Query(ge=1900, le=2200, description="Année des fériés à lister.")],
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.calendrier.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[JourFerieResume]:
    return [_vers_jour_ferie_resume(j) for j in calendrier.lister_jours_feries(db, annee)]


@router.post(
    "/jours-feries", response_model=JourFerieResume, status_code=status.HTTP_201_CREATED
)
def ajouter_jour_ferie_endpoint(
    corps: CreationJourFerie,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.calendrier.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> JourFerieResume:
    try:
        jour_ferie = calendrier.ajouter_jour_ferie(
            db, corps.date_feriee, corps.libelle, courant.user_id, contexte=_contexte(request)
        )
        db.commit()
    except calendrier.JourFerieExistantError as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    return _vers_jour_ferie_resume(jour_ferie)


@router.delete("/jours-feries/{jour_ferie_id}", status_code=status.HTTP_204_NO_CONTENT)
def supprimer_jour_ferie_endpoint(
    jour_ferie_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.calendrier.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    try:
        calendrier.supprimer_jour_ferie(
            db, jour_ferie_id, courant.user_id, contexte=_contexte(request)
        )
        db.commit()
    except calendrier.JourFerieIntrouvableError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_JOUR_FERIE_INTROUVABLE
        ) from None

