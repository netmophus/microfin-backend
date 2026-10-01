"""Endpoints HTTP — Plan de comptes : Bloc 1 (consultation + gestion unitaire) + Bloc 2
(import/export CSV en masse) + Rapports (grand livre, balance) + Saisie manuelle d'écriture OD
(chantier P1, lot 1) + Clôture TECHNIQUE d'exercice (chantier P1, lot b1).

TABLE DES ERREURS (un seul endroit) :
  - permission absente                          -> 403 (exige(), en amont)
  - compte/écriture/exercice inexistant(e)       -> 404
  - numéro déjà utilisé / classe-numéro incohérente / parent invalide -> 422, message humain
  - garde-fou (système, mouvementé, enfants actifs) -> 422, message humain (service.py)
  - fichier CSV invalide / anomalies de validation -> 422, message humain (plan.py)
  - fichier changé entre l'aperçu et la confirmation -> 422, empreintes différentes
  - écriture : exercice fermé, pièce incomplète/déséquilibrée, déjà validée/contre-passée,
    compte de saisie invalide -> 422, message humain (ecritures.py / ecritures_od.py)
  - clôture : exercice déjà clos, brouillons en attente, rien à clôturer, compte 591
    introuvable -> 422, message humain (cloture_exercice.py)

Lecture (+ export) -> compta.plan.read. Écriture (créer, modifier, sens, désactiver, import
en 2 temps) -> compta.plan.manage. Rapports -> compta.rapport.read. Saisie manuelle OD :
lecture -> compta.ecriture.read ; brouillon/validation/suppression -> compta.ecriture.post ;
contre-passation -> compta.ecriture.reverse (permissions existantes, déjà attribuées à
COMPTABLE — seed_security.py). Exercices (liste, aperçu de clôture, clôture) ->
compta.exercice.manage, lecture et écriture confondues : ouvrir/clôturer un exercice est un
acte de gestion, pas une simple consultation (permission déjà définie, sans route avant ce lot).
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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.audit.service import ecrire_audit
from app.modules.comptabilite import (
    cloture_exercice,
    comptes,
    ecritures,
    ecritures_od,
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
from app.modules.comptabilite.models import Account, Exercice, JournalEntry
from app.modules.comptabilite.rapports import TAILLE_PAGE_GRAND_LIVRE, CompteNonSaisieError
from app.modules.comptabilite.schemas import (
    ApercuCloture,
    ApercuImportComptes,
    Balance,
    BrouillonBloquantSchema,
    ChangementSens,
    ClotureExerciceResultat,
    CompteApercuSchema,
    CompteDetail,
    CompteRapport,
    CompteResume,
    CompteSelecteur,
    CompteSelecteurRapport,
    ConfirmationImportComptes,
    CreationCompte,
    CreationEcritureOD,
    DesactivationCompte,
    DiffChampSchema,
    EcritureODDetail,
    EcritureODResume,
    ExerciceResume,
    LigneBalance,
    LigneEcritureODDetail,
    LigneGrandLivre,
    LigneResultatCloture,
    ModificationCompte,
    PageComptes,
    PageEcrituresOD,
    PageGrandLivre,
    VerrouillageSaisie,
)
from app.modules.comptabilite.service import ModificationInterditeError
from app.modules.security.autorisation import UtilisateurCourant, exige
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

    anomalies = plan.valider(lignes)
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
