"""Endpoints HTTP — Plan de comptes : Bloc 1 (consultation + gestion unitaire) + Bloc 2
(import/export CSV en masse).

TABLE DES ERREURS (un seul endroit) :
  - permission absente                          -> 403 (exige(), en amont)
  - compte inexistant                           -> 404
  - numéro déjà utilisé / classe-numéro incohérente / parent invalide -> 422, message humain
  - garde-fou (système, mouvementé, enfants actifs) -> 422, message humain (service.py)
  - fichier CSV invalide / anomalies de validation -> 422, message humain (plan.py)
  - fichier changé entre l'aperçu et la confirmation -> 422, empreintes différentes

Lecture (+ export) -> compta.plan.read. Écriture (créer, modifier, sens, désactiver, import
en 2 temps) -> compta.plan.manage.
"""

import uuid
from datetime import date
from typing import Annotated

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
from app.modules.comptabilite import comptes, plan, rapports
from app.modules.comptabilite.comptes import (
    TAILLE_PAGE_DEFAUT,
    TAILLE_PAGE_MAX,
    ChampInvalideError,
    FiltresComptes,
    NumeroDejaUtiliseError,
    ParentIntrouvableError,
)
from app.modules.comptabilite.models import Account
from app.modules.comptabilite.rapports import TAILLE_PAGE_GRAND_LIVRE, CompteNonSaisieError
from app.modules.comptabilite.schemas import (
    ApercuImportComptes,
    Balance,
    ChangementSens,
    CompteApercuSchema,
    CompteDetail,
    CompteRapport,
    CompteResume,
    CompteSelecteur,
    ConfirmationImportComptes,
    CreationCompte,
    DesactivationCompte,
    DiffChampSchema,
    LigneBalance,
    LigneGrandLivre,
    ModificationCompte,
    PageComptes,
    PageGrandLivre,
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


@router.get("/comptes/selecteur-rapport", response_model=list[CompteSelecteur])
def selecteur_rapport_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.rapport.read"))],
    db: Annotated[Session, Depends(get_db)],
    q: Annotated[str | None, Query(description="Recherche — numéro ou libellé.")] = None,
) -> list[CompteSelecteur]:
    """Comptes proposables pour le grand livre — TOUJOURS de saisie, actifs OU désactivés
    (comptes.lister_pour_rapport) : un compte désactivé garde son historique consultable,
    à la différence du sélecteur de rattachement (/comptes/selecteur)."""
    return [
        CompteSelecteur(id=c.id, account_number=c.account_number, name=c.name)
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
        compte=CompteRapport(account_number=compte.account_number, name=compte.name),
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

