"""Endpoints HTTP du module Épargne — F1 : consultation + ouverture.

Permissions (exige) : lecture -> epargne.account.read / epargne.product.read ; ouverture ->
epargne.account.open. Le cloisonnement fin est une règle du service/consultation, pas un code
d'erreur : hors périmètre -> 404 (n'existe pas de mon point de vue), jamais 403.

TABLE DES ERREURS (un seul endroit, _traduire) :
  - permission absente        -> 403 (exige(), en amont)
  - membre / compte hors périmètre ou inexistant -> 404
  - membre non actif (gate KYC) -> 422 avec message : un prospect n'a pas droit à un compte
  - produit inexistant/inactif  -> 422
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.caisse.service import AucuneSessionOuverteError
from app.modules.comptabilite.comptes import CompteInvalideRattachementError
from app.modules.comptabilite.journee import AucuneJourneeOuverteError
from app.modules.comptabilite.models import Account
from app.modules.epargne import consultation, gestion_produits, rattachements
from app.modules.epargne.gestion_produits import CodeDejaUtiliseError, ComptesNonRattachesError
from app.modules.epargne.guichet import (
    CompteClotureError,
    CompteIntrouvableError,
    MontantInvalideError,
    OperationGeleeError,
    SoldeInsuffisantError,
    deposer,
    fermer_compte,
    retirer,
)
from app.modules.epargne.interets import previsualiser_interets, verser_interets
from app.modules.epargne.models import Product
from app.modules.epargne.rapprochement import rapprocher_tout
from app.modules.epargne.schemas import (
    ActivationProduit,
    ApercuInterets,
    ApercuLigneInterets,
    CompteEpargneDetail,
    CompteEpargneResume,
    CompteGuichet,
    CompteRattachement,
    CreationProduitEpargne,
    DemandeInterets,
    LigneRapprochement,
    ModificationProduitEpargne,
    ModificationRattachementsProduit,
    MouvementResume,
    OperationGuichet,
    OuvertureCompte,
    ProduitEpargne,
    ProduitEpargneDetail,
    RapportInterets,
    RattachementsProduit,
    ResultatOperation,
    ValidationProduitResultat,
)
from app.modules.epargne.service import (
    CompteDebiteurError,
    CompteDejaClotureError,
    MembreNonActifError,
    ProduitIntrouvableError,
    ouvrir_compte,
)
from app.modules.security.autorisation import UtilisateurCourant, exige
from app.modules.security.router import _contexte
from app.modules.tiers.models import Tier

router = APIRouter(tags=["epargne"])

MESSAGE_COMPTE_INTROUVABLE = "Compte d'épargne introuvable."
MESSAGE_MEMBRE_INTROUVABLE = "Membre introuvable."
MESSAGE_PRODUIT_INTROUVABLE = "Produit d'épargne introuvable."


def _compte_rattachement(db: Session, account_id: uuid.UUID | None) -> CompteRattachement | None:
    if account_id is None:
        return None
    compte = db.get(Account, account_id)
    if compte is None:
        return None
    return CompteRattachement(account_number=compte.account_number, name=compte.name)


def _vers_rattachements(db: Session, produit: Product) -> RattachementsProduit:
    return RattachementsProduit(
        id=produit.id,
        code=produit.code,
        name=produit.name,
        compte_epargne=_compte_rattachement(db, produit.compte_epargne_id),
        compte_epargne_client=_compte_rattachement(db, produit.compte_epargne_client_id),
        compte_charge_interet=_compte_rattachement(db, produit.compte_charge_interet_id),
    )


def _vers_detail(produit: Product) -> ProduitEpargneDetail:
    return ProduitEpargneDetail(
        id=produit.id,
        code=produit.code,
        name=produit.name,
        type=produit.type,
        currency=produit.currency,
        is_active=produit.is_active,
        is_provisional=produit.is_provisional,
        taux_bp=produit.taux_bp,
        periodicite=produit.periodicite,
        methode_calcul_solde=produit.methode_calcul_solde,
        base_jours=produit.base_jours,
        regle_arrondi=produit.regle_arrondi,
        solde_minimum_remunere=produit.solde_minimum_remunere,
    )


def _resume(compte: object, produit: object) -> CompteEpargneResume:
    return CompteEpargneResume(
        id=compte.id,
        account_number=compte.account_number,
        product_code=produit.code,
        product_name=produit.name,
        product_type=produit.type,
        currency=compte.currency,
        balance=compte.balance,
        status=compte.status,
        is_provisional=produit.is_provisional,
    )


@router.get("/epargne/produits", response_model=list[ProduitEpargne])
def lister_produits_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("epargne.product.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[ProduitEpargne]:
    """Les produits d'épargne actifs (pour le choix à l'ouverture)."""
    return [
        ProduitEpargne(
            id=p.id, code=p.code, name=p.name, type=p.type, is_provisional=p.is_provisional
        )
        for p in consultation.lister_produits(db)
    ]


@router.get("/epargne/produits/rattachements", response_model=list[RattachementsProduit])
def lister_rattachements_produits_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.plan.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[RattachementsProduit]:
    """Rattachements comptables des produits ACTIFS (Bloc 5) — écran du comptable."""
    return [_vers_rattachements(db, p) for p in consultation.lister_produits(db)]


@router.patch(
    "/epargne/produits/{produit_id}/rattachements", response_model=RattachementsProduit
)
def modifier_rattachements_produit_endpoint(
    produit_id: uuid.UUID,
    corps: ModificationRattachementsProduit,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.plan.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> RattachementsProduit:
    """Ce changement s'applique aux PROCHAINES opérations seulement — les écritures déjà
    posées référencent directement un compte, jamais ce paramètre."""
    produit = db.get(Product, produit_id)
    if produit is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_PRODUIT_INTROUVABLE
        )
    try:
        rattachements.modifier_rattachements(
            db,
            produit,
            compte_epargne_number=corps.compte_epargne,
            compte_epargne_client_number=corps.compte_epargne_client,
            compte_charge_interet_number=corps.compte_charge_interet,
            motif=corps.motif,
            par=courant.user_id,
            contexte=_contexte(request),
        )
        db.commit()
    except CompteInvalideRattachementError as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    return _vers_rattachements(db, produit)


# --- Référentiel produit : consultation, création, modification métier, validation,
# activation ---------------------------------------------------------------------------------
# Gardé epargne.product.read (lecture) / epargne.product.manage (écriture) — ADMIN_FONCTIONNEL —
# jamais compta.plan.manage, réservé aux 2 blocs ci-dessus (comptes, taux). Voir
# gestion_produits.py.


@router.get("/epargne/produits/referentiel", response_model=list[ProduitEpargneDetail])
def lister_produits_gestion_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("epargne.product.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[ProduitEpargneDetail]:
    """TOUS les produits (actifs, inactifs, provisoires) — écran de gestion du référentiel.
    Distinct de `GET /epargne/produits` (choix à l'ouverture, actifs seulement, schéma léger)."""
    return [_vers_detail(p) for p in consultation.lister_produits_gestion(db)]


@router.post(
    "/epargne/produits", response_model=ProduitEpargneDetail, status_code=status.HTTP_201_CREATED
)
def creer_produit_endpoint(
    corps: CreationProduitEpargne,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("epargne.product.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> ProduitEpargneDetail:
    """Crée un produit PROVISOIRE, sans compte comptable rattaché (voir l'écran de
    rattachement, permission distincte, après création)."""
    try:
        produit = gestion_produits.creer_produit(
            db,
            code=corps.code,
            name=corps.name,
            type=corps.type,
            currency=corps.currency,
            taux_bp=corps.taux_bp,
            periodicite=corps.periodicite,
            methode_calcul_solde=corps.methode_calcul_solde,
            base_jours=corps.base_jours,
            regle_arrondi=corps.regle_arrondi,
            solde_minimum_remunere=corps.solde_minimum_remunere,
            par=courant.user_id,
            contexte=_contexte(request),
        )
        db.commit()
    except CodeDejaUtiliseError as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    except IntegrityError as erreur:
        # Filet de sécurité : deux créations concurrentes du même code (rare).
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Le code « {corps.code} » est déjà utilisé par un autre produit d'épargne.",
        ) from erreur
    return _vers_detail(produit)


@router.patch("/epargne/produits/{produit_id}", response_model=ProduitEpargneDetail)
def modifier_produit_endpoint(
    produit_id: uuid.UUID,
    corps: ModificationProduitEpargne,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("epargne.product.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> ProduitEpargneDetail:
    """Champs MÉTIER uniquement (nom, type, paramètres d'intérêt) — ne touche jamais aux comptes
    rattachés (endpoint dédié, `compta.plan.manage`)."""
    produit = db.get(Product, produit_id)
    if produit is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_PRODUIT_INTROUVABLE
        )
    gestion_produits.modifier_produit(
        db,
        produit,
        name=corps.name,
        type=corps.type,
        taux_bp=corps.taux_bp,
        periodicite=corps.periodicite,
        methode_calcul_solde=corps.methode_calcul_solde,
        base_jours=corps.base_jours,
        regle_arrondi=corps.regle_arrondi,
        solde_minimum_remunere=corps.solde_minimum_remunere,
        motif=corps.motif,
        par=courant.user_id,
        contexte=_contexte(request),
    )
    db.commit()
    return _vers_detail(produit)


@router.post("/epargne/produits/{produit_id}/valider", response_model=ValidationProduitResultat)
def valider_produit_endpoint(
    produit_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("epargne.product.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> ValidationProduitResultat:
    """Lève le provisoire — REFUSE (422) si le compte membre n'est pas rattaché. Le compte
    client manquant ne bloque pas mais revient en avertissement explicite (bascule vers le
    compte membre, à ne jamais laisser silencieuse)."""
    produit = db.get(Product, produit_id)
    if produit is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_PRODUIT_INTROUVABLE
        )
    try:
        avertissements = gestion_produits.valider_produit(
            db, produit, par=courant.user_id, contexte=_contexte(request)
        )
        db.commit()
    except ComptesNonRattachesError as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    return ValidationProduitResultat(
        **_vers_detail(produit).model_dump(), avertissements=avertissements
    )


@router.patch("/epargne/produits/{produit_id}/activation", response_model=ProduitEpargneDetail)
def changer_activation_produit_endpoint(
    produit_id: uuid.UUID,
    corps: ActivationProduit,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("epargne.product.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> ProduitEpargneDetail:
    """(Dés)active le produit dans le catalogue proposé à l'ouverture (jamais de suppression).
    Motif obligatoire dans les deux sens."""
    produit = db.get(Product, produit_id)
    if produit is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_PRODUIT_INTROUVABLE
        )
    gestion_produits.changer_activation_produit(
        db,
        produit,
        is_active=corps.is_active,
        motif=corps.motif,
        par=courant.user_id,
        contexte=_contexte(request),
    )
    db.commit()
    return _vers_detail(produit)


@router.get("/tiers/{tier_id}/comptes-epargne", response_model=list[CompteEpargneResume])
def lister_comptes_membre_endpoint(
    tier_id: uuid.UUID,
    courant: Annotated[UtilisateurCourant, Depends(exige("epargne.account.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[CompteEpargneResume]:
    """Les comptes d'épargne d'un membre (cloisonné à l'agence de l'acteur)."""
    return [
        _resume(compte, produit)
        for compte, produit in consultation.lister_comptes_du_membre(db, courant, tier_id)
    ]


@router.get("/epargne/comptes/{compte_id}", response_model=CompteEpargneDetail)
def lire_compte_endpoint(
    compte_id: uuid.UUID,
    courant: Annotated[UtilisateurCourant, Depends(exige("epargne.account.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> CompteEpargneDetail:
    """Détail d'un compte + relevé des mouvements. 404 si hors périmètre."""
    ligne = consultation.lire_compte(db, courant, compte_id)
    if ligne is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_COMPTE_INTROUVABLE
        )
    compte, produit = ligne
    mouvements = [
        MouvementResume(
            id=m.id,
            sens=m.sens,
            amount=m.amount,
            balance_after=m.balance_after,
            operation_type=m.operation_type,
            label=m.label,
            created_at=m.created_at,
            entry_number=entry_number,
        )
        for m, entry_number in consultation.lister_mouvements(db, compte_id)
    ]
    base = _resume(compte, produit)
    return CompteEpargneDetail(
        **base.model_dump(),
        opened_at=compte.opened_at,
        closed_at=compte.closed_at,
        mouvements=mouvements,
    )


@router.post(
    "/tiers/{tier_id}/comptes-epargne",
    response_model=CompteEpargneResume,
    status_code=status.HTTP_201_CREATED,
)
def ouvrir_compte_endpoint(
    tier_id: uuid.UUID,
    corps: OuvertureCompte,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("epargne.account.open"))],
    db: Annotated[Session, Depends(get_db)],
) -> CompteEpargneResume:
    """Ouvre un compte d'épargne pour un membre ACTIF (gate KYC). Réservé chargé/responsable."""
    # Le membre doit être dans le périmètre de l'acteur ; on en tire aussi son agence.
    agency_id = db.execute(
        select(Tier.primary_agency_id).where(
            Tier.id == tier_id,
            Tier.deleted_at.is_(None),
            courant.condition_perimetre(Tier.primary_agency_id),
        )
    ).scalar_one_or_none()
    if agency_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_MEMBRE_INTROUVABLE
        )

    try:
        compte = ouvrir_compte(
            db,
            tier_id=tier_id,
            product_id=corps.product_id,
            agency_id=agency_id,
            par=courant.user_id,
            contexte=_contexte(request),
        )
        db.commit()
    except MembreNonActifError as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    except ProduitIntrouvableError as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None

    produit = next(
        p for p in consultation.lister_produits(db) if p.id == corps.product_id
    )
    return _resume(compte, produit)


# --- Guichet : recherche + dépôt/retrait --------------------------------------------


def _422(erreur: Exception) -> HTTPException:
    """Refus d'opération -> 422 avec le message métier (dit POURQUOI). Jamais une erreur brute."""
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur))


def _vers_compte_guichet(ligne: Any) -> CompteGuichet:
    compte, produit, tier_id, nom = ligne
    return CompteGuichet(
        id=compte.id,
        account_number=compte.account_number,
        tier_id=tier_id,
        membre_nom=nom,
        product_name=produit.name,
        product_type=produit.type,
        currency=compte.currency,
        balance=compte.balance,
        status=compte.status,
        is_provisional=produit.is_provisional,
    )


@router.get("/epargne/recherche-comptes", response_model=list[CompteGuichet])
def rechercher_comptes_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("epargne.account.read"))],
    db: Annotated[Session, Depends(get_db)],
    q: Annotated[str, Query(min_length=1, description="Numéro de compte ou nom du titulaire.")],
) -> list[CompteGuichet]:
    """Trouve les comptes du périmètre dont le numéro OU le nom du titulaire correspond à `q`
    (chemin de recherche du guichet, partielle) — même patron que
    `credit.recherche-remboursement`. Le NOM du membre est renvoyé, proéminent à l'écran :
    vérification humaine contre une faute de frappe dans le numéro choisi."""
    lignes = consultation.rechercher_comptes(db, courant, q.strip())
    return [_vers_compte_guichet(ligne) for ligne in lignes]


@router.post("/epargne/comptes/{compte_id}/depot", response_model=ResultatOperation)
def deposer_endpoint(
    compte_id: uuid.UUID,
    corps: OperationGuichet,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("epargne.operation.deposit"))],
    db: Annotated[Session, Depends(get_db)],
) -> ResultatOperation:
    """Dépôt au guichet (caissier, cloisonné). Le service commet la transaction unique. Exige
    une session de caisse OUVERTE pour l'acteur (Bloc C4) : la CAISSE créditée est celle de SA
    session, pas celle de l'agence."""
    try:
        resultat = deposer(db, courant, compte_id, corps.montant, contexte=_contexte(request))
    except CompteIntrouvableError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_COMPTE_INTROUVABLE
        ) from None
    except (
        MontantInvalideError,
        OperationGeleeError,
        CompteClotureError,
        AucuneSessionOuverteError,
        AucuneJourneeOuverteError,
    ) as erreur:
        raise _422(erreur) from None
    return ResultatOperation(
        account_number=resultat.account_number,
        nouveau_solde=resultat.nouveau_solde,
        entry_number=resultat.entry_number,
    )


@router.post("/epargne/comptes/{compte_id}/fermeture", response_model=CompteEpargneResume)
def fermer_compte_endpoint(
    compte_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("epargne.account.close"))],
    db: Annotated[Session, Depends(get_db)],
) -> CompteEpargneResume:
    """Ferme un compte (responsable) : restitution du solde résiduel + clôture définitive."""
    try:
        compte = fermer_compte(db, courant, compte_id, contexte=_contexte(request))
    except CompteIntrouvableError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_COMPTE_INTROUVABLE
        ) from None
    except (CompteDebiteurError, CompteDejaClotureError) as erreur:
        raise _422(erreur) from None
    produit = next(
        p for p in consultation.lister_produits(db) if p.id == compte.product_id
    )
    return _resume(compte, produit)


# --- Intérêts (F4) : prévisualiser, puis verser. Acte d'INSTITUTION, réservé à la direction ----


@router.post("/epargne/interets/apercu", response_model=ApercuInterets)
def previsualiser_interets_endpoint(
    corps: DemandeInterets,
    courant: Annotated[UtilisateurCourant, Depends(exige("epargne.interet.executer"))],
    db: Annotated[Session, Depends(get_db)],
) -> ApercuInterets:
    """Prévisualisation obligatoire : CALCULE sans rien verser (dry-run). Renvoie le total ET un
    échantillon détaillé (taux, méthode, base, montant par compte) pour vérifier « ça a l'air
    juste » avant de créer de l'argent. Dit si la période est déjà (partiellement) versée."""
    apercu = previsualiser_interets(
        db, periode=corps.periode.strip(), debut=corps.debut, fin=corps.fin
    )
    return ApercuInterets(
        periode=apercu.periode,
        debut=apercu.debut,
        fin=apercu.fin,
        jours=apercu.jours,
        comptes_actifs=apercu.comptes_actifs,
        comptes_taux_zero=apercu.comptes_taux_zero,
        comptes_a_crediter=apercu.comptes_a_crediter,
        total=apercu.total,
        deja_traites=apercu.deja_traites,
        deja_verse_le=apercu.deja_verse_le,
        echantillon=[
            ApercuLigneInterets(
                account_number=ligne.account_number,
                produit=ligne.produit,
                taux_bp=ligne.taux_bp,
                methode=ligne.methode,
                base_solde=ligne.base_solde,
                montant=ligne.montant,
            )
            for ligne in apercu.echantillon
        ],
    )


@router.post("/epargne/interets", response_model=RapportInterets)
def verser_interets_endpoint(
    corps: DemandeInterets,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("epargne.interet.executer"))],
    db: Annotated[Session, Depends(get_db)],
) -> RapportInterets:
    """Versement effectif : crédite les comptes, pose les écritures (D 603 / C 3111). Idempotent —
    une période déjà versée ressort en `ignores`, l'écran le dira (« déjà versés »). Le moteur
    committe compte par compte."""
    rapport = verser_interets(
        db,
        periode=corps.periode.strip(),
        debut=corps.debut,
        fin=corps.fin,
        par=courant.user_id,
        contexte=_contexte(request),
    )
    return RapportInterets(
        traites=rapport.traites,
        credites=rapport.credites,
        ignores=rapport.ignores,
        total=rapport.total,
    )


@router.get("/epargne/rapprochement", response_model=list[LigneRapprochement])
def rapprochement_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("epargne.rapprochement.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[LigneRapprochement]:
    """Vue de contrôle (auditeur/direction) : pour chaque compte collectif, Σ des soldes d'épargne
    (auxiliaire) face au solde comptable (général). Concordant, ou écart signalé avec le montant.
    Vue RÉSEAU (l'invariant porte sur tout le collectif), non cloisonnée à une agence."""
    return [
        LigneRapprochement(
            compte_general=r.compte_general,
            auxiliaire=r.auxiliaire,
            general=r.general,
            concordant=r.concordant,
            ecart=r.ecart,
        )
        for r in rapprocher_tout(db)
    ]


@router.post("/epargne/comptes/{compte_id}/retrait", response_model=ResultatOperation)
def retirer_endpoint(
    compte_id: uuid.UUID,
    corps: OperationGuichet,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("epargne.operation.withdraw"))],
    db: Annotated[Session, Depends(get_db)],
) -> ResultatOperation:
    """Retrait au guichet. Refus parlant si solde insuffisant / membre suspendu / compte fermé.
    Exige une session de caisse OUVERTE pour l'acteur (Bloc C4) : la CAISSE débitée est celle de
    SA session, pas celle de l'agence."""
    try:
        resultat = retirer(db, courant, compte_id, corps.montant, contexte=_contexte(request))
    except CompteIntrouvableError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_COMPTE_INTROUVABLE
        ) from None
    except (
        SoldeInsuffisantError,
        MontantInvalideError,
        OperationGeleeError,
        CompteClotureError,
        AucuneSessionOuverteError,
        AucuneJourneeOuverteError,
    ) as erreur:
        raise _422(erreur) from None
    return ResultatOperation(
        account_number=resultat.account_number,
        nouveau_solde=resultat.nouveau_solde,
        entry_number=resultat.entry_number,
    )
