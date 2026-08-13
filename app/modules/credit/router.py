"""Endpoints HTTP du module Crédit — CR1 : produits (lecture), demande, décision.
CR3 : décaissement (pièce comptable + échéancier persisté) et lecture de l'échéancier.
CR4 : remboursement (une échéance à la fois, montant exact).
CR6b : aperçu PUR de l'échéancier d'une demande approuvée — même moteur que le décaissement,
rien n'est écrit en base (à présenter au client avant signature/décaissement).
CR6d : recherche du guichet (numéro de dossier, numéro de tiers ou nom) — voir
credit.remboursement.create pour l'encaissement lui-même, déjà présent.
CR5a : paramétrage des paliers de souffrance (Bloc 5) — lecture/écriture de la CONFIGURATION
seule. CR5c : reclassification automatique — un seul endpoint d'exécution, réservé DIRECTION,
voir reclassification.py pour le détail (encours + provisionnement, comptes dynamiques par
palier).

Permissions (exige) : lecture produits -> credit.product.read ; créer une demande ->
credit.demande.create ; lire -> credit.demande.read ; décider -> credit.demande.decide ;
décaisser -> credit.decaissement.create (séparée de decide) ; rembourser ->
credit.remboursement.create (voir seed_security) ; paliers de souffrance en LECTURE ->
compta.plan.read OU credit.delinquency.read (exige_une_de — le comptable via le Bloc 5 entier,
la direction en lecture seule avant de lancer le job, moindre privilège) ; en ÉCRITURE ->
compta.plan.manage seul (inchangé) ; aperçu + exécution de la reclassification ->
credit.delinquency.executer (DIRECTION seule, acte d'institution).

TABLE DES ERREURS (un seul endroit) :
  - permission absente                    -> 403 (exige(), en amont)
  - tiers / demande / palier hors périmètre ou inexistant -> 404
  - tiers non actif (gate KYC, création, approbation ou décaissement) -> 422
  - produit inexistant/inactif             -> 422
  - demande déjà décidée / montant décidé invalide -> 422
  - demande non approuvée, rattachement comptable manquant, échéancier impossible -> 422
  - compte choisi invalide (mode 'epargne' : hors tiers, hors périmètre, fermé) -> 422
  - aucune session de caisse ouverte (décaissement mode 'caisse', remboursement au guichet) -> 422
  - aucune échéance à régler (non décaissé ou déjà soldé), montant incorrect -> 422
  - code/seuil de palier déjà utilisé par un autre palier, compte de rattachement invalide -> 422
  - palier encore classé sur un dossier (suppression refusée) -> 422
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.caisse.service import AucuneSessionOuverteError
from app.modules.comptabilite.comptes import CompteInvalideRattachementError
from app.modules.comptabilite.models import Account
from app.modules.credit import consultation, delinquency_parametres
from app.modules.credit.decaissement import (
    DemandeNonApprouveeError,
    RattachementManquantError,
    decaisser,
    generer_apercu,
)
from app.modules.credit.delinquency_parametres import (
    CodeDejaUtiliseError,
    PalierEnUsageError,
    SeuilDejaUtiliseError,
)
from app.modules.credit.demandes import (
    DemandeDejaDecideeError,
    MontantDecideInvalideError,
    ProduitIntrouvableError,
    TierNonActifError,
    creer_demande,
    decider,
)
from app.modules.credit.echeancier import EcheancierImpossibleError
from app.modules.credit.models import Application, DelinquencyTier, Installment
from app.modules.credit.reclassification import (
    executer_reclassification,
    previsualiser_reclassement,
)
from app.modules.credit.remboursement import (
    AucuneEcheanceAReglerError,
    MontantIncorrectError,
    prochaine_echeance,
    rembourser,
)
from app.modules.credit.schemas import (
    ApercuReclassement,
    CompteRattachementPalier,
    CreationDemande,
    CreationPalier,
    DecaissementCorps,
    Decision,
    DemandeDecaissee,
    DemandeDetail,
    DemandeResume,
    DossierRemboursable,
    EcheanceApercuLigne,
    EcheanceDue,
    EcheanceLigne,
    LigneApercuReclassement,
    LigneReclassement,
    ModificationPalier,
    PalierSouffrance,
    RapportReclassement,
    Remboursement,
    RemboursementRecu,
    SuppressionPalier,
)
from app.modules.epargne.models import SavingsAccount
from app.modules.epargne.operations import CompteInvalideError
from app.modules.security.autorisation import UtilisateurCourant, exige, exige_une_de
from app.modules.security.router import _contexte
from app.modules.tiers.models import Tier

router = APIRouter(tags=["credit"])

MESSAGE_TIER_INTROUVABLE = "Tiers introuvable."
MESSAGE_DEMANDE_INTROUVABLE = "Demande de crédit introuvable."
MESSAGE_PALIER_INTROUVABLE = "Palier de souffrance introuvable."
# Défaut du corps de décaissement (mode 'caisse', comportement historique si aucun corps
# n'est envoyé) — singleton module, pas un appel dans la signature (immutable, jamais modifié).
_DECAISSEMENT_CAISSE_PAR_DEFAUT = DecaissementCorps()


def _resume(ligne: tuple) -> DemandeResume:
    demande, produit, tier_number, tier_nom, is_member = ligne
    return DemandeResume(
        id=demande.id,
        application_number=demande.application_number,
        tier_id=demande.tier_id,
        tier_number=tier_number,
        tier_nom=tier_nom,
        is_member=is_member,
        product_code=produit.code,
        product_name=produit.name,
        montant_demande=demande.montant_demande,
        duree_echeances=demande.duree_echeances,
        status=demande.status,
        created_at=demande.created_at,
    )


@router.get("/credit/produits")
def lister_produits_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("credit.product.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[dict[str, object]]:
    """Produits de crédit actifs — pour le choix à la demande."""
    return [
        {"id": p.id, "code": p.code, "name": p.name, "is_provisional": p.is_provisional}
        for p in consultation.lister_produits(db)
    ]


@router.get("/credit/demandes", response_model=list[DemandeResume])
def lister_demandes_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("credit.demande.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[DemandeResume]:
    """Les demandes de crédit dans le périmètre de l'acteur."""
    return [_resume(ligne) for ligne in consultation.lister_demandes(db, courant)]


@router.get("/credit/demandes/{application_id}", response_model=DemandeDetail)
def lire_demande_endpoint(
    application_id: uuid.UUID,
    courant: Annotated[UtilisateurCourant, Depends(exige("credit.demande.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> DemandeDetail:
    ligne = consultation.lire_demande(db, courant, application_id)
    if ligne is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_DEMANDE_INTROUVABLE
        )
    demande, *_reste = ligne
    base = _resume(ligne)
    return DemandeDetail(
        **base.model_dump(),
        objet=demande.objet,
        montant_decide=demande.montant_decide,
        decided_at=demande.decided_at,
        motif_decision=demande.motif_decision,
    )


@router.get("/tiers/{tier_id}/demandes-credit", response_model=list[DemandeResume])
def lister_demandes_tier_endpoint(
    tier_id: uuid.UUID,
    courant: Annotated[UtilisateurCourant, Depends(exige("credit.demande.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[DemandeResume]:
    """Les demandes de crédit d'UN tiers, dans le périmètre de l'acteur — pour l'onglet Crédit
    de sa fiche. Un tiers hors périmètre est INTROUVABLE (404), jamais 403."""
    agency_id = db.execute(
        select(Tier.primary_agency_id).where(
            Tier.id == tier_id,
            Tier.deleted_at.is_(None),
            courant.condition_perimetre(Tier.primary_agency_id),
        )
    ).scalar_one_or_none()
    if agency_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_TIER_INTROUVABLE
        )

    return [_resume(ligne) for ligne in consultation.lister_demandes_tier(db, courant, tier_id)]


@router.post(
    "/tiers/{tier_id}/demandes-credit",
    response_model=DemandeResume,
    status_code=status.HTTP_201_CREATED,
)
def creer_demande_endpoint(
    tier_id: uuid.UUID,
    corps: CreationDemande,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("credit.demande.create"))],
    db: Annotated[Session, Depends(get_db)],
) -> DemandeResume:
    """Crée une demande de crédit pour un tiers ACTIF (gate KYC), dans le périmètre de l'acteur."""
    agency_id = db.execute(
        select(Tier.primary_agency_id).where(
            Tier.id == tier_id,
            Tier.deleted_at.is_(None),
            courant.condition_perimetre(Tier.primary_agency_id),
        )
    ).scalar_one_or_none()
    if agency_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_TIER_INTROUVABLE
        )

    try:
        demande = creer_demande(
            db,
            tier_id=tier_id,
            agency_id=agency_id,
            product_id=corps.product_id,
            montant_demande=corps.montant_demande,
            duree_echeances=corps.duree_echeances,
            objet=corps.objet,
            par=courant.user_id,
            contexte=_contexte(request),
        )
        db.commit()
    except (TierNonActifError, ProduitIntrouvableError) as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None

    ligne = consultation.lire_demande(db, courant, demande.id)
    assert ligne is not None
    return _resume(ligne)


@router.post("/credit/demandes/{application_id}/decision", response_model=DemandeDetail)
def decider_endpoint(
    application_id: uuid.UUID,
    corps: Decision,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("credit.demande.decide"))],
    db: Annotated[Session, Depends(get_db)],
) -> DemandeDetail:
    """Approuve ou refuse une demande — motif obligatoire dans les deux sens, décision définitive."""
    ligne = consultation.lire_demande(db, courant, application_id)
    if ligne is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_DEMANDE_INTROUVABLE
        )
    demande = db.get(Application, application_id)
    assert demande is not None

    try:
        decider(
            db,
            demande,
            decision=corps.decision,
            montant_decide=corps.montant_decide,
            motif=corps.motif,
            par=courant.user_id,
            contexte=_contexte(request),
        )
        db.commit()
    except (
        DemandeDejaDecideeError,
        TierNonActifError,
        MontantDecideInvalideError,
    ) as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None

    ligne = consultation.lire_demande(db, courant, application_id)
    assert ligne is not None
    base = _resume(ligne)
    return DemandeDetail(
        **base.model_dump(),
        objet=demande.objet,
        montant_decide=demande.montant_decide,
        decided_at=demande.decided_at,
        motif_decision=demande.motif_decision,
    )


@router.get(
    "/credit/demandes/{application_id}/echeancier-apercu",
    response_model=list[EcheanceApercuLigne],
)
def apercu_echeancier_endpoint(
    application_id: uuid.UUID,
    courant: Annotated[UtilisateurCourant, Depends(exige("credit.demande.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[EcheanceApercuLigne]:
    """Aperçu PUR de l'échéancier d'une demande approuvée — calcul, RIEN n'est écrit en base.
    À présenter au client avant signature/décaissement : montants définitifs, dates
    indicatives (voir decaissement.generer_apercu)."""
    ligne = consultation.lire_demande(db, courant, application_id)
    if ligne is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_DEMANDE_INTROUVABLE
        )
    demande = db.get(Application, application_id)
    assert demande is not None

    try:
        echeances = generer_apercu(db, demande)
    except (
        DemandeNonApprouveeError,
        ProduitIntrouvableError,
        EcheancierImpossibleError,
    ) as erreur:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None

    return [
        EcheanceApercuLigne(
            numero=e.numero,
            due_date=e.due_date,
            capital=e.capital,
            interets=e.interets,
            total=e.total,
            capital_restant_du=e.capital_restant_du,
        )
        for e in echeances
    ]


@router.post(
    "/credit/demandes/{application_id}/decaissement", response_model=DemandeDecaissee
)
def decaisser_endpoint(
    application_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("credit.decaissement.create"))],
    db: Annotated[Session, Depends(get_db)],
    corps: DecaissementCorps = _DECAISSEMENT_CAISSE_PAR_DEFAUT,
) -> DemandeDecaissee:
    """Décaisse une demande APPROUVÉE : pièce comptable + échéancier persisté, en une
    transaction unique. Réservé au responsable d'agence (séparation des tâches avec le
    chargé de prêt qui a monté le dossier).

    `corps.mode` : 'caisse' (espèces, défaut — exige une session de caisse OUVERTE pour
    l'acteur, Bloc C5) ou 'epargne' (crédit direct sur un compte du tiers choisi — n'importe
    quel produit epargne.accounts, `corps.compte_epargne_id`, aucune session requise)."""
    ligne = consultation.lire_demande(db, courant, application_id)
    if ligne is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_DEMANDE_INTROUVABLE
        )
    demande = db.get(Application, application_id)
    assert demande is not None

    try:
        decaisser(
            db,
            demande,
            par=courant.user_id,
            mode=corps.mode,
            compte_epargne_id=corps.compte_epargne_id,
            courant=courant,
            contexte=_contexte(request),
        )
        db.commit()
    except (
        DemandeNonApprouveeError,
        TierNonActifError,
        ProduitIntrouvableError,
        RattachementManquantError,
        EcheancierImpossibleError,
        CompteInvalideError,
        AucuneSessionOuverteError,
    ) as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None

    ligne = consultation.lire_demande(db, courant, application_id)
    assert ligne is not None
    base = _resume(ligne)
    echeances = consultation.lire_echeancier(db, courant, application_id)
    compte_credit_number = db.execute(
        select(Account.account_number).where(Account.id == demande.compte_credit_id)
    ).scalar_one_or_none()
    # Mode 'epargne' : le numéro UTILE est celui du compte du TIERS (ex. EP-2026-000006), pas
    # celui du collectif comptable (251121) — plusieurs tiers partagent le même collectif, il
    # ne distinguerait rien. Mode 'caisse' : le compte de caisse (comptabilité) suffit.
    if demande.mode_decaissement == "epargne" and corps.compte_epargne_id is not None:
        compte_destination_number = db.execute(
            select(SavingsAccount.account_number).where(
                SavingsAccount.id == corps.compte_epargne_id
            )
        ).scalar_one_or_none()
    else:
        compte_destination_number = db.execute(
            select(Account.account_number).where(Account.id == demande.compte_destination_id)
        ).scalar_one_or_none()
    return DemandeDecaissee(
        **base.model_dump(),
        objet=demande.objet,
        montant_decide=demande.montant_decide,
        decided_at=demande.decided_at,
        motif_decision=demande.motif_decision,
        disbursed_at=demande.disbursed_at,
        compte_credit_number=compte_credit_number,
        mode_decaissement=demande.mode_decaissement,
        compte_destination_number=compte_destination_number,
        nb_echeances=len(echeances),
        premiere_echeance_le=echeances[0].due_date if echeances else None,
        derniere_echeance_le=echeances[-1].due_date if echeances else None,
    )


@router.get("/credit/demandes/{application_id}/echeancier", response_model=list[EcheanceLigne])
def lire_echeancier_endpoint(
    application_id: uuid.UUID,
    courant: Annotated[UtilisateurCourant, Depends(exige("credit.demande.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[EcheanceLigne]:
    """L'échéancier persisté d'une demande décaissée (vide si pas encore décaissée)."""
    ligne = consultation.lire_demande(db, courant, application_id)
    if ligne is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_DEMANDE_INTROUVABLE
        )
    return [
        EcheanceLigne(
            numero=e.numero,
            due_date=e.due_date,
            capital=e.capital,
            interets=e.interets,
            total=e.total,
            capital_restant_du=e.capital_restant_du,
            status=e.status,
            montant_paye=e.montant_paye,
            solde_du=e.total - e.montant_paye,
        )
        for e in consultation.lire_echeancier(db, courant, application_id)
    ]


@router.get("/credit/recherche-remboursement", response_model=list[DossierRemboursable])
def rechercher_remboursement_endpoint(
    q: str,
    courant: Annotated[UtilisateurCourant, Depends(exige("credit.remboursement.create"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[DossierRemboursable]:
    """Trouve les crédits DÉCAISSÉS du périmètre, par numéro de dossier, numéro de tiers ou nom
    (chemin de recherche du guichet CR6d). Un résultat sans `prochaine_echeance` est déjà
    entièrement soldé — le frontend l'affiche tel quel, jamais un clic qui échouerait.

    Gardée sur `credit.remboursement.create` (pas `credit.demande.read`) : le CAISSIER, seul
    acteur du guichet de remboursement, ne détient QUE ce premier droit — il n'a pas de visibilité
    générale sur les dossiers de crédit, seulement sur ceux qu'il peut encaisser."""
    resultats: list[DossierRemboursable] = []
    lignes = consultation.rechercher_remboursables(db, courant, q.strip())
    for demande, produit, tier_number, tier_nom, _is_member in lignes:
        echeance = prochaine_echeance(db, demande.id)
        resultats.append(
            DossierRemboursable(
                id=demande.id,
                application_number=demande.application_number,
                tier_number=tier_number,
                tier_nom=tier_nom,
                product_name=produit.name,
                prochaine_echeance=(
                    EcheanceDue(
                        numero=echeance.numero,
                        due_date=echeance.due_date,
                        capital=echeance.capital,
                        interets=echeance.interets,
                        total=echeance.total,
                        montant_paye=echeance.montant_paye,
                        solde_du=echeance.total - echeance.montant_paye,
                    )
                    if echeance is not None
                    else None
                ),
            )
        )
    return resultats


@router.post(
    "/credit/demandes/{application_id}/remboursement", response_model=RemboursementRecu
)
def rembourser_endpoint(
    application_id: uuid.UUID,
    corps: Remboursement,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("credit.remboursement.create"))],
    db: Annotated[Session, Depends(get_db)],
) -> RemboursementRecu:
    """Règle la prochaine échéance impayée, pour son montant EXACT (ou un versement partiel,
    CR5b). Aucun gate KYC (encaisser de l'argent qui rentre ne présente aucun risque). Exige une
    session de caisse OUVERTE pour l'acteur (Bloc C6, guichet volontaire seulement) : la CAISSE
    débitée est celle de SA session, pas celle de l'agence."""
    ligne = consultation.lire_demande(db, courant, application_id)
    if ligne is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_DEMANDE_INTROUVABLE
        )
    demande = db.get(Application, application_id)
    assert demande is not None

    try:
        resultat = rembourser(
            db, demande, montant=corps.montant, par=courant.user_id, contexte=_contexte(request)
        )
        db.commit()
    except (
        AucuneEcheanceAReglerError,
        MontantIncorrectError,
        RattachementManquantError,
        AucuneSessionOuverteError,
    ) as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None

    # != 'paye', pas == 'a_echoir' (CR5b) : une échéance partiellement payée reste « restante ».
    restantes = db.execute(
        select(func.count())
        .select_from(Installment)
        .where(Installment.application_id == application_id, Installment.status != "paye")
    ).scalar_one()
    echeance = resultat.echeance
    return RemboursementRecu(
        numero=echeance.numero,
        due_date=echeance.due_date,
        capital=resultat.montant_capital,
        interets=resultat.montant_interets,
        montant_total=resultat.montant,
        paid_at=resultat.paid_at,
        solde_du=resultat.solde_du,
        echeance_soldee=resultat.echeance_soldee,
        echeances_restantes=restantes,
    )


# --- Paliers de souffrance (CR5a, Bloc 5) -----------------------------------------------------


def _compte_rattachement_palier(
    db: Session, account_id: uuid.UUID | None
) -> CompteRattachementPalier | None:
    if account_id is None:
        return None
    compte = db.get(Account, account_id)
    if compte is None:
        return None
    return CompteRattachementPalier(account_number=compte.account_number, name=compte.name)


def _vers_palier(db: Session, palier: DelinquencyTier) -> PalierSouffrance:
    return PalierSouffrance(
        id=palier.id,
        code=palier.code,
        libelle=palier.libelle,
        seuil_jours=palier.seuil_jours,
        taux_provision_bp=palier.taux_provision_bp,
        compte_encours=_compte_rattachement_palier(db, palier.compte_encours_id),
        compte_dotation=_compte_rattachement_palier(db, palier.compte_dotation_id),
        compte_provision=_compte_rattachement_palier(db, palier.compte_provision_id),
        compte_reprise=_compte_rattachement_palier(db, palier.compte_reprise_id),
        is_terminal=palier.is_terminal,
        is_provisional=palier.is_provisional,
    )


@router.get("/credit/paliers-souffrance", response_model=list[PalierSouffrance])
def lister_paliers_souffrance_endpoint(
    courant: Annotated[
        UtilisateurCourant,
        Depends(exige_une_de("compta.plan.read", "credit.delinquency.read")),
    ],
    db: Annotated[Session, Depends(get_db)],
) -> list[PalierSouffrance]:
    """Les paliers de souffrance, triés par ancienneté (CR5a, paramétrage). Lecture ouverte au
    comptable (compta.plan.read, Bloc 5 entier) ET à la direction (credit.delinquency.read,
    lecture seule — avant de lancer la reclassification). L'écriture (create/modifier/retirer)
    reste réservée à compta.plan.manage, inchangée sur les 3 autres routes ci-dessous."""
    return [_vers_palier(db, p) for p in delinquency_parametres.lister(db)]


@router.post(
    "/credit/paliers-souffrance",
    response_model=PalierSouffrance,
    status_code=status.HTTP_201_CREATED,
)
def creer_palier_souffrance_endpoint(
    corps: CreationPalier,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.plan.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> PalierSouffrance:
    """Ajoute un palier — le nombre de paliers est une donnée, aucune migration requise."""
    try:
        palier = delinquency_parametres.creer(
            db,
            code=corps.code,
            libelle=corps.libelle,
            seuil_jours=corps.seuil_jours,
            taux_provision_bp=corps.taux_provision_bp,
            compte_encours_number=corps.compte_encours,
            compte_dotation_number=corps.compte_dotation,
            compte_provision_number=corps.compte_provision,
            compte_reprise_number=corps.compte_reprise,
            is_terminal=corps.is_terminal,
            motif=corps.motif,
            par=courant.user_id,
            contexte=_contexte(request),
        )
        db.commit()
    except (
        CodeDejaUtiliseError,
        SeuilDejaUtiliseError,
        CompteInvalideRattachementError,
    ) as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    return _vers_palier(db, palier)


@router.patch("/credit/paliers-souffrance/{palier_id}", response_model=PalierSouffrance)
def modifier_palier_souffrance_endpoint(
    palier_id: uuid.UUID,
    corps: ModificationPalier,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.plan.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> PalierSouffrance:
    """Remplace l'état complet d'un palier (pas un PATCH partiel — même discipline que les
    rattachements produit d'épargne)."""
    palier = db.get(DelinquencyTier, palier_id)
    if palier is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_PALIER_INTROUVABLE
        )
    try:
        delinquency_parametres.modifier(
            db,
            palier,
            code=corps.code,
            libelle=corps.libelle,
            seuil_jours=corps.seuil_jours,
            taux_provision_bp=corps.taux_provision_bp,
            compte_encours_number=corps.compte_encours,
            compte_dotation_number=corps.compte_dotation,
            compte_provision_number=corps.compte_provision,
            compte_reprise_number=corps.compte_reprise,
            is_terminal=corps.is_terminal,
            motif=corps.motif,
            par=courant.user_id,
            contexte=_contexte(request),
        )
        db.commit()
    except (
        CodeDejaUtiliseError,
        SeuilDejaUtiliseError,
        CompteInvalideRattachementError,
    ) as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    return _vers_palier(db, palier)


@router.post(
    "/credit/paliers-souffrance/{palier_id}/retirer",
    response_model=None,
    status_code=status.HTTP_204_NO_CONTENT,
)
def supprimer_palier_souffrance_endpoint(
    palier_id: uuid.UUID,
    corps: SuppressionPalier,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.plan.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    """Retire un palier. Refuse si un dossier de crédit y est actuellement classé (CR5c)."""
    palier = db.get(DelinquencyTier, palier_id)
    if palier is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_PALIER_INTROUVABLE
        )
    try:
        delinquency_parametres.supprimer(
            db, palier, motif=corps.motif, par=courant.user_id, contexte=_contexte(request)
        )
        db.commit()
    except PalierEnUsageError as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None


@router.post("/credit/delinquency/apercu", response_model=ApercuReclassement)
def previsualiser_reclassement_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("credit.delinquency.executer"))],
    db: Annotated[Session, Depends(get_db)],
) -> ApercuReclassement:
    """Prévisualisation OBLIGATOIRE avant exécution (dry-run) : CALCULE sans rien écrire — même
    permission que l'exécution (voir reclassification.py, previsualiser_reclassement).
    Ne liste que les dossiers dont le palier changerait réellement."""
    apercu = previsualiser_reclassement(db)
    return ApercuReclassement(
        dossiers_evalues=apercu.dossiers_evalues,
        a_reclasser=apercu.a_reclasser,
        rattachements_manquants=apercu.rattachements_manquants,
        lignes=[
            LigneApercuReclassement(
                application_number=ligne.application_number,
                tier_avant_code=ligne.tier_avant_code,
                tier_avant_libelle=ligne.tier_avant_libelle,
                tier_apres_code=ligne.tier_apres_code,
                tier_apres_libelle=ligne.tier_apres_libelle,
                jours_retard=ligne.jours_retard,
                encours_actuel=ligne.encours_actuel,
                provision_avant=ligne.provision_avant,
                provision_apres=ligne.provision_apres,
                rattachement_manquant=ligne.rattachement_manquant,
            )
            for ligne in apercu.lignes
        ],
    )


@router.post("/credit/delinquency/executer", response_model=RapportReclassement)
def executer_reclassification_endpoint(
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("credit.delinquency.executer"))],
    db: Annotated[Session, Depends(get_db)],
) -> RapportReclassement:
    """Reclasse tous les crédits décaissés (CR5c) — acte D'INSTITUTION réservé DIRECTION,
    même patron que epargne.interet.executer. Chaque dossier est committé séparément : un
    paramétrage incomplet sur l'un ne bloque pas les autres (voir reclassification.py)."""
    rapport = executer_reclassification(
        db, par=courant.user_id, contexte=_contexte(request)
    )
    return RapportReclassement(
        dossiers_evalues=rapport.dossiers_evalues,
        reclasses=rapport.reclasses,
        ignores_rattachement_manquant=rapport.ignores_rattachement_manquant,
        lignes=[
            LigneReclassement(
                application_number=ligne.application_number,
                tier_avant_code=ligne.tier_avant_code,
                tier_avant_libelle=ligne.tier_avant_libelle,
                tier_apres_code=ligne.tier_apres_code,
                tier_apres_libelle=ligne.tier_apres_libelle,
                jours_retard=ligne.jours_retard,
                encours_actuel=ligne.encours_actuel,
                provision_avant=ligne.provision_avant,
                provision_apres=ligne.provision_apres,
            )
            for ligne in rapport.lignes
        ],
    )
