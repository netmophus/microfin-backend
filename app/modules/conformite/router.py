"""Endpoints HTTP du module Conformité — ratios prudentiels RCSFD (lot P2.1.a, couche API
au-dessus du moteur `app/modules/conformite/moteur.py` et du paramétrage `service.py`).

Deux volets, deux permissions, deux préfixes :
  - Volet 1 (tableau de bord, LECTURE SEULE, `conformite.ratio.read`) :
    `GET /conformite/ratios` et `GET /conformite/ratios/{code}` — adressés PAR CODE (le
    tableau de bord navigue par identifiant métier, jamais par UUID).
  - Volet 2 (paramétrage, CRUD, `conformite.ratio.manage`) : sous `/conformite/admin/...`,
    préfixe délibéré pour ne jamais partager un gabarit d'URL avec le volet 1 — les routes de
    paramétrage adressent la ressource PAR id (UUID), même convention que les paliers de
    souffrance (`credit.delinquency_tier`).

Date d'arrêté (`a_la_date`) : même défaut que les états financiers (`etats_financiers.bilan`)
— `CURRENT_DATE` côté base si omis, jamais la date civile du poste client.

TABLE DES ERREURS (un seul endroit) :
  - permission absente                                         -> 403 (exige(), en amont)
  - agrégat / ratio / seuil / palier hors périmètre ou inexistant -> 404
  - paramètre de l'institution non initialisé (seed jamais exécuté) -> 404
  - code d'agrégat ou de ratio déjà utilisé, ordre déjà utilisé, seuil déjà défini pour cette
    catégorie, référence à un agrégat de numérateur/dénominateur inexistant, type SPECIAL sans
    calcul_special (ou l'inverse), seuil négatif, categorie_sfd hors énum -> 422, message humain
  - suppression d'un agrégat encore référencé par un ratio                -> 422, message humain
  - agrégat SPECIAL dont le calcul n'est pas encore implémenté (demandé en détail, volet 1) ->
    422, message humain (jamais un 500 — voir moteur.CalculSpecialInconnuError)

Dans la LISTE du tableau de bord (volet 1), un ratio INACTIF n'est JAMAIS soumis au moteur —
l'écran le montre « en attente » sans chiffre, et ceci quel que soit l'état de son
paramétrage (même un ratio actif dont l'agrégat SPECIAL n'est pas implémenté dégrade sur la
même présentation plutôt que de faire échouer tout le tableau de bord — état « partiel »,
§6 CLAUDE.md). Le DÉTAIL d'un ratio (`GET .../{code}`), lui, évalue TOUJOURS le ratio demandé
et renvoie un 422 explicite si le calcul échoue : l'administrateur qui consulte UN ratio en
particulier doit savoir précisément pourquoi, pas recevoir un silence poli.
"""

import uuid
from datetime import date
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.conformite import service
from app.modules.conformite.models import (
    AgregatPrudentiel,
    RatioPrudentiel,
    RatioSeuil,
)
from app.modules.conformite.moteur import (
    STATUT_NON_CALCULABLE,
    AgregatIntrouvableError,
    CalculSpecialInconnuError,
    EvaluationRatio,
    RatioIntrouvableError,
    detail_agregat,
    evaluer_ratio,
)
from app.modules.conformite.schemas import (
    AgregatAdmin,
    ComposantAgregatSchema,
    CreationAgregat,
    CreationRatio,
    CreationSeuil,
    DetailAgregatSchema,
    LigneCompositionAgregat,
    ModificationAgregat,
    ModificationParametreInstitution,
    ModificationRatio,
    ModificationSeuil,
    ParametreInstitutionSchema,
    RatioAdmin,
    RatioDetail,
    RatioEvalue,
    SeuilAdmin,
    SuppressionAgregat,
    SuppressionRatio,
    SuppressionSeuil,
)
from app.modules.conformite.service import (
    AgregatReferenceError,
    CodeAgregatDejaUtiliseError,
    CodeRatioDejaUtiliseError,
    OrdreRatioDejaUtiliseError,
    SeuilDejaDefiniError,
)
from app.modules.security.autorisation import UtilisateurCourant, exige
from app.modules.security.router import _contexte

router = APIRouter(tags=["conformite"])

MESSAGE_AGREGAT_INTROUVABLE = "Agrégat prudentiel introuvable."
MESSAGE_RATIO_INTROUVABLE = "Ratio prudentiel introuvable."
MESSAGE_SEUIL_INTROUVABLE = "Seuil introuvable."
MESSAGE_PARAMETRE_INTROUVABLE = (
    "Les paramètres de l'institution n'ont pas encore été initialisés "
    "(exécuter le seed de conformité)."
)

_ERREURS_422_PARAMETRAGE = (
    CodeAgregatDejaUtiliseError,
    CodeRatioDejaUtiliseError,
    OrdreRatioDejaUtiliseError,
    SeuilDejaDefiniError,
    AgregatReferenceError,
    AgregatIntrouvableError,
)


def _date_arretee(db: Session, a_la_date: date | None) -> date:
    if a_la_date is not None:
        return a_la_date
    return cast(date, db.execute(text("SELECT CURRENT_DATE")).scalar_one())


# --- Volet 1 — Lecture (tableau de bord) ---------------------------------------------------


def _ratio_en_attente(ratio: RatioPrudentiel) -> RatioEvalue:
    return RatioEvalue(
        code=ratio.code,
        libelle=ratio.libelle,
        reference_reglementaire=ratio.reference_reglementaire,
        operateur=ratio.operateur,  # type: ignore[arg-type]
        seuil_applicable=None,
        valeur_numerateur=None,
        valeur_denominateur=None,
        valeur_ratio_pct=None,
        conforme=None,
        marge=None,
        statut=STATUT_NON_CALCULABLE,  # type: ignore[arg-type]
        actif=ratio.actif,
    )


def _vers_ratio_evalue(ratio: RatioPrudentiel, evaluation: EvaluationRatio) -> RatioEvalue:
    return RatioEvalue(
        code=evaluation.code,
        libelle=evaluation.libelle,
        reference_reglementaire=ratio.reference_reglementaire,
        operateur=evaluation.operateur,  # type: ignore[arg-type]
        seuil_applicable=evaluation.seuil_applicable,
        valeur_numerateur=evaluation.valeur_numerateur,
        valeur_denominateur=evaluation.valeur_denominateur,
        valeur_ratio_pct=evaluation.valeur_ratio_pct,
        conforme=evaluation.conforme,
        marge=evaluation.marge,
        statut=evaluation.statut,  # type: ignore[arg-type]
        actif=ratio.actif,
    )


@router.get("/conformite/ratios", response_model=list[RatioEvalue])
def lister_ratios_evalues_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("conformite.ratio.read"))],
    db: Annotated[Session, Depends(get_db)],
    a_la_date: Annotated[date | None, Query(description="Par défaut : aujourd'hui.")] = None,
) -> list[RatioEvalue]:
    """Les 10 ratios paramétrés — les ACTIFS évalués, les INACTIFS renvoyés « en attente »
    sans jamais appeler le moteur sur eux (voir docstring de module)."""
    date_arret = _date_arretee(db, a_la_date)
    resultats: list[RatioEvalue] = []
    for ratio in service.lister_ratios(db):
        if not ratio.actif:
            resultats.append(_ratio_en_attente(ratio))
            continue
        try:
            evaluation = evaluer_ratio(db, ratio.code, date_arret)
        except CalculSpecialInconnuError:
            # Garde-fou : un ratio actif dont l'agrégat SPECIAL n'est pas encore implémenté
            # dégrade sur la présentation « en attente » plutôt que de faire échouer tout le
            # tableau de bord — un seul paramétrage incomplet ne doit jamais tout casser.
            resultats.append(_ratio_en_attente(ratio))
            continue
        resultats.append(_vers_ratio_evalue(ratio, evaluation))
    return resultats


@router.get("/conformite/ratios/{code}", response_model=RatioDetail)
def detail_ratio_endpoint(
    code: str,
    courant: Annotated[UtilisateurCourant, Depends(exige("conformite.ratio.read"))],
    db: Annotated[Session, Depends(get_db)],
    a_la_date: Annotated[date | None, Query(description="Par défaut : aujourd'hui.")] = None,
) -> RatioDetail:
    """Le détail d'UN ratio — évalué quel que soit son `actif` (l'administrateur qui
    paramètre doit voir le calcul réel), PLUS la décomposition de ses deux agrégats."""
    date_arret = _date_arretee(db, a_la_date)
    ratio = service.obtenir_ratio_par_code(db, code)
    if ratio is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_RATIO_INTROUVABLE)

    agregat_num = db.get(AgregatPrudentiel, ratio.agregat_numerateur_id)
    agregat_denom = db.get(AgregatPrudentiel, ratio.agregat_denominateur_id)
    assert agregat_num is not None and agregat_denom is not None  # FK NOT NULL

    try:
        evaluation = evaluer_ratio(db, code, date_arret)
        detail_num = detail_agregat(db, agregat_num.code, date_arret)
        detail_denom = detail_agregat(db, agregat_denom.code, date_arret)
    except (CalculSpecialInconnuError, RatioIntrouvableError, AgregatIntrouvableError) as erreur:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None

    base = _vers_ratio_evalue(ratio, evaluation)
    return RatioDetail(
        **base.model_dump(),
        agregat_numerateur=DetailAgregatSchema(
            code=detail_num.code,
            libelle=detail_num.libelle,
            type=detail_num.type,  # type: ignore[arg-type]
            valeur=detail_num.valeur,
            composants=[
                ComposantAgregatSchema(
                    prefixe_compte=c.prefixe_compte,
                    sens=c.sens,  # type: ignore[arg-type]
                    solde=c.solde,
                    contribution=c.contribution,
                )
                for c in detail_num.composants
            ],
            complement_provisions_tutelle_applique=detail_num.complement_provisions_tutelle_applique,
        ),
        agregat_denominateur=DetailAgregatSchema(
            code=detail_denom.code,
            libelle=detail_denom.libelle,
            type=detail_denom.type,  # type: ignore[arg-type]
            valeur=detail_denom.valeur,
            composants=[
                ComposantAgregatSchema(
                    prefixe_compte=c.prefixe_compte,
                    sens=c.sens,  # type: ignore[arg-type]
                    solde=c.solde,
                    contribution=c.contribution,
                )
                for c in detail_denom.composants
            ],
            complement_provisions_tutelle_applique=detail_denom.complement_provisions_tutelle_applique,
        ),
    )


# --- Volet 2 — Paramétrage (CRUD) -----------------------------------------------------------


def _vers_agregat_admin(db: Session, agregat: AgregatPrudentiel) -> AgregatAdmin:
    return AgregatAdmin(
        id=agregat.id,
        code=agregat.code,
        libelle=agregat.libelle,
        reference=agregat.reference,
        type=agregat.type,  # type: ignore[arg-type]
        calcul_special=agregat.calcul_special,
        nets_de_provisions=agregat.nets_de_provisions,
        applique_complement_provisions_tutelle=agregat.applique_complement_provisions_tutelle,
        is_system=agregat.is_system,
        composition=[
            LigneCompositionAgregat(prefixe_compte=c.prefixe_compte, sens=c.sens)  # type: ignore[arg-type]
            for c in service.composition_agregat(db, agregat.id)
        ],
    )


@router.get("/conformite/admin/agregats", response_model=list[AgregatAdmin])
def lister_agregats_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("conformite.ratio.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[AgregatAdmin]:
    return [_vers_agregat_admin(db, a) for a in service.lister_agregats(db)]


@router.get("/conformite/admin/agregats/{agregat_id}", response_model=AgregatAdmin)
def obtenir_agregat_endpoint(
    agregat_id: uuid.UUID,
    courant: Annotated[UtilisateurCourant, Depends(exige("conformite.ratio.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> AgregatAdmin:
    agregat = service.obtenir_agregat(db, agregat_id)
    if agregat is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_AGREGAT_INTROUVABLE
        )
    return _vers_agregat_admin(db, agregat)


@router.post(
    "/conformite/admin/agregats", response_model=AgregatAdmin, status_code=status.HTTP_201_CREATED
)
def creer_agregat_endpoint(
    corps: CreationAgregat,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("conformite.ratio.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> AgregatAdmin:
    try:
        agregat = service.creer_agregat(
            db,
            code=corps.code,
            libelle=corps.libelle,
            reference=corps.reference,
            type=corps.type,
            calcul_special=corps.calcul_special,
            nets_de_provisions=corps.nets_de_provisions,
            applique_complement_provisions_tutelle=corps.applique_complement_provisions_tutelle,
            composition=[(ligne.prefixe_compte, ligne.sens) for ligne in corps.composition],
            motif=corps.motif,
            par=courant.user_id,
            contexte=_contexte(request),
        )
        db.commit()
    except CodeAgregatDejaUtiliseError as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    return _vers_agregat_admin(db, agregat)


@router.patch("/conformite/admin/agregats/{agregat_id}", response_model=AgregatAdmin)
def modifier_agregat_endpoint(
    agregat_id: uuid.UUID,
    corps: ModificationAgregat,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("conformite.ratio.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> AgregatAdmin:
    """Remplace l'état complet de l'agrégat, composition comprise — `is_system` n'empêche
    rien (voir service.py)."""
    agregat = service.obtenir_agregat(db, agregat_id)
    if agregat is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_AGREGAT_INTROUVABLE
        )
    try:
        service.modifier_agregat(
            db,
            agregat,
            code=corps.code,
            libelle=corps.libelle,
            reference=corps.reference,
            type=corps.type,
            calcul_special=corps.calcul_special,
            nets_de_provisions=corps.nets_de_provisions,
            applique_complement_provisions_tutelle=corps.applique_complement_provisions_tutelle,
            composition=[(ligne.prefixe_compte, ligne.sens) for ligne in corps.composition],
            motif=corps.motif,
            par=courant.user_id,
            contexte=_contexte(request),
        )
        db.commit()
    except CodeAgregatDejaUtiliseError as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    return _vers_agregat_admin(db, agregat)


@router.post(
    "/conformite/admin/agregats/{agregat_id}/retirer",
    response_model=None,
    status_code=status.HTTP_204_NO_CONTENT,
)
def supprimer_agregat_endpoint(
    agregat_id: uuid.UUID,
    corps: SuppressionAgregat,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("conformite.ratio.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    agregat = service.obtenir_agregat(db, agregat_id)
    if agregat is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_AGREGAT_INTROUVABLE
        )
    try:
        service.supprimer_agregat(
            db, agregat, motif=corps.motif, par=courant.user_id, contexte=_contexte(request)
        )
        db.commit()
    except AgregatReferenceError as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None


def _vers_ratio_admin(db: Session, ratio: RatioPrudentiel) -> RatioAdmin:
    agregat_num = db.get(AgregatPrudentiel, ratio.agregat_numerateur_id)
    agregat_denom = db.get(AgregatPrudentiel, ratio.agregat_denominateur_id)
    assert agregat_num is not None and agregat_denom is not None
    return RatioAdmin(
        id=ratio.id,
        code=ratio.code,
        libelle=ratio.libelle,
        reference_reglementaire=ratio.reference_reglementaire,
        agregat_numerateur_code=agregat_num.code,
        agregat_denominateur_code=agregat_denom.code,
        operateur=ratio.operateur,  # type: ignore[arg-type]
        actif=ratio.actif,
        ordre=ratio.ordre,
        is_system=ratio.is_system,
    )


@router.get("/conformite/admin/ratios", response_model=list[RatioAdmin])
def lister_ratios_admin_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("conformite.ratio.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[RatioAdmin]:
    return [_vers_ratio_admin(db, r) for r in service.lister_ratios(db)]


@router.get("/conformite/admin/ratios/{ratio_id}", response_model=RatioAdmin)
def obtenir_ratio_admin_endpoint(
    ratio_id: uuid.UUID,
    courant: Annotated[UtilisateurCourant, Depends(exige("conformite.ratio.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> RatioAdmin:
    ratio = service.obtenir_ratio(db, ratio_id)
    if ratio is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_RATIO_INTROUVABLE)
    return _vers_ratio_admin(db, ratio)


@router.post(
    "/conformite/admin/ratios", response_model=RatioAdmin, status_code=status.HTTP_201_CREATED
)
def creer_ratio_endpoint(
    corps: CreationRatio,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("conformite.ratio.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> RatioAdmin:
    try:
        ratio = service.creer_ratio(
            db,
            code=corps.code,
            libelle=corps.libelle,
            reference_reglementaire=corps.reference_reglementaire,
            agregat_numerateur_code=corps.agregat_numerateur_code,
            agregat_denominateur_code=corps.agregat_denominateur_code,
            operateur=corps.operateur,
            actif=corps.actif,
            ordre=corps.ordre,
            motif=corps.motif,
            par=courant.user_id,
            contexte=_contexte(request),
        )
        db.commit()
    except _ERREURS_422_PARAMETRAGE as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    return _vers_ratio_admin(db, ratio)


@router.patch("/conformite/admin/ratios/{ratio_id}", response_model=RatioAdmin)
def modifier_ratio_endpoint(
    ratio_id: uuid.UUID,
    corps: ModificationRatio,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("conformite.ratio.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> RatioAdmin:
    """Remplace l'état complet du ratio — activation/désactivation, opérateur et ordre
    inclus, pas de routes séparées pour ces champs."""
    ratio = service.obtenir_ratio(db, ratio_id)
    if ratio is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_RATIO_INTROUVABLE)
    try:
        service.modifier_ratio(
            db,
            ratio,
            code=corps.code,
            libelle=corps.libelle,
            reference_reglementaire=corps.reference_reglementaire,
            agregat_numerateur_code=corps.agregat_numerateur_code,
            agregat_denominateur_code=corps.agregat_denominateur_code,
            operateur=corps.operateur,
            actif=corps.actif,
            ordre=corps.ordre,
            motif=corps.motif,
            par=courant.user_id,
            contexte=_contexte(request),
        )
        db.commit()
    except _ERREURS_422_PARAMETRAGE as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    return _vers_ratio_admin(db, ratio)


@router.post(
    "/conformite/admin/ratios/{ratio_id}/retirer",
    response_model=None,
    status_code=status.HTTP_204_NO_CONTENT,
)
def supprimer_ratio_endpoint(
    ratio_id: uuid.UUID,
    corps: SuppressionRatio,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("conformite.ratio.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    ratio = service.obtenir_ratio(db, ratio_id)
    if ratio is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_RATIO_INTROUVABLE)
    service.supprimer_ratio(
        db, ratio, motif=corps.motif, par=courant.user_id, contexte=_contexte(request)
    )
    db.commit()


def _vers_seuil_admin(seuil: RatioSeuil) -> SeuilAdmin:
    return SeuilAdmin(
        id=seuil.id,
        ratio_id=seuil.ratio_id,
        categorie_sfd=seuil.categorie_sfd,  # type: ignore[arg-type]
        valeur_seuil=seuil.valeur_seuil,
        is_system=seuil.is_system,
    )


@router.get("/conformite/admin/ratios/{ratio_id}/seuils", response_model=list[SeuilAdmin])
def lister_seuils_endpoint(
    ratio_id: uuid.UUID,
    courant: Annotated[UtilisateurCourant, Depends(exige("conformite.ratio.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[SeuilAdmin]:
    ratio = service.obtenir_ratio(db, ratio_id)
    if ratio is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_RATIO_INTROUVABLE)
    return [_vers_seuil_admin(s) for s in service.lister_seuils(db, ratio_id)]


@router.post(
    "/conformite/admin/ratios/{ratio_id}/seuils",
    response_model=SeuilAdmin,
    status_code=status.HTTP_201_CREATED,
)
def creer_seuil_endpoint(
    ratio_id: uuid.UUID,
    corps: CreationSeuil,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("conformite.ratio.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> SeuilAdmin:
    ratio = service.obtenir_ratio(db, ratio_id)
    if ratio is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_RATIO_INTROUVABLE)
    try:
        seuil = service.creer_seuil(
            db,
            ratio,
            categorie_sfd=corps.categorie_sfd,
            valeur_seuil=corps.valeur_seuil,
            motif=corps.motif,
            par=courant.user_id,
            contexte=_contexte(request),
        )
        db.commit()
    except SeuilDejaDefiniError as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    return _vers_seuil_admin(seuil)


@router.patch(
    "/conformite/admin/ratios/{ratio_id}/seuils/{seuil_id}", response_model=SeuilAdmin
)
def modifier_seuil_endpoint(
    ratio_id: uuid.UUID,
    seuil_id: uuid.UUID,
    corps: ModificationSeuil,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("conformite.ratio.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> SeuilAdmin:
    seuil = service.obtenir_seuil(db, seuil_id)
    if seuil is None or seuil.ratio_id != ratio_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_SEUIL_INTROUVABLE
        )
    try:
        service.modifier_seuil(
            db,
            seuil,
            categorie_sfd=corps.categorie_sfd,
            valeur_seuil=corps.valeur_seuil,
            motif=corps.motif,
            par=courant.user_id,
            contexte=_contexte(request),
        )
        db.commit()
    except SeuilDejaDefiniError as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    return _vers_seuil_admin(seuil)


@router.post(
    "/conformite/admin/ratios/{ratio_id}/seuils/{seuil_id}/retirer",
    response_model=None,
    status_code=status.HTTP_204_NO_CONTENT,
)
def supprimer_seuil_endpoint(
    ratio_id: uuid.UUID,
    seuil_id: uuid.UUID,
    corps: SuppressionSeuil,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("conformite.ratio.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    seuil = service.obtenir_seuil(db, seuil_id)
    if seuil is None or seuil.ratio_id != ratio_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_SEUIL_INTROUVABLE
        )
    service.supprimer_seuil(
        db, seuil, motif=corps.motif, par=courant.user_id, contexte=_contexte(request)
    )
    db.commit()


@router.get("/conformite/admin/parametre-institution", response_model=ParametreInstitutionSchema)
def obtenir_parametre_institution_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("conformite.ratio.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> ParametreInstitutionSchema:
    parametre = service.obtenir_parametre_institution(db)
    if parametre is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_PARAMETRE_INTROUVABLE
        )
    return ParametreInstitutionSchema(
        id=parametre.id,
        categorie_sfd=parametre.categorie_sfd,  # type: ignore[arg-type]
        complement_provisions_tutelle=parametre.complement_provisions_tutelle,
    )


@router.patch("/conformite/admin/parametre-institution", response_model=ParametreInstitutionSchema)
def modifier_parametre_institution_endpoint(
    corps: ModificationParametreInstitution,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("conformite.ratio.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> ParametreInstitutionSchema:
    """Pas de création ni de suppression via l'API (singleton bootstrapé par
    `seed-conformite`, voir service.py)."""
    parametre = service.obtenir_parametre_institution(db)
    if parametre is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_PARAMETRE_INTROUVABLE
        )
    service.modifier_parametre_institution(
        db,
        parametre,
        categorie_sfd=corps.categorie_sfd,
        complement_provisions_tutelle=corps.complement_provisions_tutelle,
        motif=corps.motif,
        par=courant.user_id,
        contexte=_contexte(request),
    )
    db.commit()
    return ParametreInstitutionSchema(
        id=parametre.id,
        categorie_sfd=parametre.categorie_sfd,  # type: ignore[arg-type]
        complement_provisions_tutelle=parametre.complement_provisions_tutelle,
    )
