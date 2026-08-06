"""Pont Épargne -> comptabilité : traduire une opération d'un compte d'épargne en pièce.

Résout les rôles du modèle d'écriture depuis le compte d'épargne concerné :
  - EPARGNE -> le compte de dette du PRODUIT (product.compte_epargne_id, le 3111 collectif) ;
  - CAISSE  -> le compte de caisse de l'AGENCE (agency.compte_caisse_id, le 5721).
Si l'un de ces rattachements manque (provisoire non renseigné), on REFUSE proprement
(RattachementManquantError) : rien n'est écrit, message clair.

Ce module ne bouge NI le solde du membre NI aucun mouvement : il pose seulement la pièce
comptable. C'est E3 (dépôt/retrait) qui l'appellera, avec le verrou et le mouvement, dans une
seule transaction.

CRÉDIT EXTERNE (décaissement de crédit direct sur un compte du tiers, voir
credit/decaissement.py) : `resoudre_compte_collectif`, `charger_compte_pour_credit_externe` et
`enregistrer_credit_externe` sont le point d'entrée POUR UN AUTRE MODULE — la pièce comptable
reste posée par L'APPELANT (qui connaît son propre rôle CREDIT en plus du rôle EPARGNE), ce
module ne fait que le verrou + la comptabilisation auxiliaire (mouvement + solde), EN FLUSH
SEULEMENT — jamais de commit, contrairement à guichet._operer() : l'appelant externe reste
maître de SA transaction.
"""

import uuid

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete
from app.modules.comptabilite.models import JournalEntry
from app.modules.comptabilite.schemas_ecriture import ResolveurRole, poser_depuis_schema
from app.modules.epargne.models import Product, SavingsAccount, SavingsMovement
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant

# Codes des modèles d'écriture des opérations d'épargne (seed comptabilite).
TYPE_DEPOT = "epargne.depot"
TYPE_RETRAIT = "epargne.retrait"
TYPE_CLOTURE = "epargne.cloture"
TYPE_INTERET = "epargne.interet"


class RattachementManquantError(Exception):
    """Un rôle ne se résout pas : compte non rattaché (produit ou agence). Refus propre."""


class CompteInvalideError(Exception):
    """Le compte choisi pour un crédit externe n'existe pas, n'appartient pas au tiers
    attendu, est hors périmètre, ou n'est pas actif. Refus propre, avant tout écriture."""


def resoudre_compte_collectif(db: Session, compte: SavingsAccount) -> uuid.UUID | None:
    """Le compte comptable COLLECTIF qui reçoit les opérations de CE compte — l'ancrage PS3
    (compte.compte_collectif_id) prime, repli sur le compte membre du produit pour un compte
    legacy (NULL). Exposé pour qu'un module EXTERNE (ex. décaissement de crédit direct sur un
    compte du tiers) réutilise cette règle plutôt que de la redupliquer."""
    compte_epargne = db.execute(
        select(Product.compte_epargne_id).where(Product.id == compte.product_id)
    ).scalar_one()
    return compte.compte_collectif_id or compte_epargne


def charger_compte_pour_credit_externe(
    db: Session,
    courant: UtilisateurCourant,
    compte_id: uuid.UUID,
    tier_id: uuid.UUID,
) -> SavingsAccount:
    """Charge SOUS VERROU (FOR UPDATE) le compte choisi pour recevoir un crédit EXTERNE au
    module — vérifie qu'il appartient bien à CE tiers, qu'il est dans le périmètre de
    l'acteur, et qu'il est ACTIF. Même verrou que le guichet (populate_existing) : on décide
    sur l'état réel du compte, pas une copie périmée en session."""
    compte = db.execute(
        select(SavingsAccount)
        .where(
            SavingsAccount.id == compte_id,
            SavingsAccount.tier_id == tier_id,
            courant.condition_perimetre(SavingsAccount.agency_id),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if compte is None:
        raise CompteInvalideError(
            "Ce compte n'existe pas, n'appartient pas à ce tiers, ou est hors de votre périmètre."
        )
    if compte.status != "actif":
        raise CompteInvalideError("Ce compte est fermé : impossible d'y créditer un décaissement.")
    # DETTE TEMPORAIRE (voir docs/conformite-comptable.md, chantier « blocage des DAT ») : un
    # DAT ('terme') n'a AUCUN mécanisme de blocage jusqu'à échéance dans ce module (pas de
    # maturity_date, retirer() ne distingue pas les types) — le créditer d'un décaissement n'a
    # pas de sens métier (le client ne pourrait pas en disposer librement, ce qui contredit
    # l'objet même d'un décaissement). Exclusion PAR TYPE, pas un vrai contrôle de disponibilité
    # (qui n'existe pas encore) : à remplacer par un prédicat « compte disponible » le jour où
    # le blocage DAT sera réellement implémenté.
    type_produit = db.execute(
        select(Product.type).where(Product.id == compte.product_id)
    ).scalar_one()
    if type_produit == "terme":
        raise CompteInvalideError(
            "Ce compte est un dépôt à terme : un décaissement de crédit ne peut pas y être "
            "crédité (le client ne pourrait pas en disposer librement)."
        )
    return compte


def enregistrer_credit_externe(
    db: Session,
    compte: SavingsAccount,
    montant: int,
    *,
    operation_type: str,
    label: str,
    journal_entry_id: uuid.UUID,
    par: uuid.UUID | None,
) -> SavingsMovement:
    """Crédite CE compte pour une opération EXTERNE dont l'écriture comptable a DÉJÀ ÉTÉ
    POSÉE PAR L'APPELANT (ce module ne connaît pas le vocabulaire des opérations externes qui
    peuvent créditer un compte — `operation_type`/`label` sont fournis tels quels, pas
    dérivés). Mouvement + solde, FLUSH SEULEMENT — jamais de commit, pour composer DANS la
    transaction unique de l'appelant."""
    nouveau_solde = compte.balance + montant
    mouvement = SavingsMovement(
        account_id=compte.id,
        sens="credit",
        amount=montant,
        balance_after=nouveau_solde,
        operation_type=operation_type,
        label=label,
        journal_entry_id=journal_entry_id,
        created_by=par,
    )
    db.add(mouvement)
    compte.balance = nouveau_solde
    compte.updated_by = par
    db.flush()
    return mouvement


def enregistrer_debit_externe(
    db: Session,
    compte: SavingsAccount,
    montant: int,
    *,
    operation_type: str,
    label: str,
    journal_entry_id: uuid.UUID,
    par: uuid.UUID | None,
) -> SavingsMovement:
    """Débite CE compte pour une opération EXTERNE dont l'écriture comptable a DÉJÀ ÉTÉ POSÉE
    PAR L'APPELANT — miroir exact de `enregistrer_credit_externe`, sens inverse (prélèvement
    automatique de crédit CR5d, voir credit/prelevement.py). Le plancher
    (min_balance/decouvert_autorise) est déjà respecté par L'APPELANT avant ce point (montant
    plafonné à ce qui est disponible) : ici on ne fait qu'enregistrer, aucun nouveau contrôle.
    Mouvement + solde, FLUSH SEULEMENT — jamais de commit, pour composer DANS la transaction
    unique de l'appelant."""
    nouveau_solde = compte.balance - montant
    mouvement = SavingsMovement(
        account_id=compte.id,
        sens="debit",
        amount=montant,
        balance_after=nouveau_solde,
        operation_type=operation_type,
        label=label,
        journal_entry_id=journal_entry_id,
        created_by=par,
    )
    db.add(mouvement)
    compte.balance = nouveau_solde
    compte.updated_by = par
    db.flush()
    return mouvement


def _resolveur(db: Session, compte: SavingsAccount) -> ResolveurRole:
    compte_epargne, compte_charge_interet = db.execute(
        select(Product.compte_epargne_id, Product.compte_charge_interet_id).where(
            Product.id == compte.product_id
        )
    ).one()
    # ROUTAGE ANCRÉ (PS3) : le collectif FIGÉ à l'ouverture du compte prime (membre -> 3111,
    # client -> 3112). Repli sur le compte membre du produit pour un compte legacy (NULL) —
    # comportement historique intact. Toutes les opérations du compte (dépôt, retrait, clôture,
    # intérêts) suivent le MÊME collectif : un compte ne s'éclate jamais entre 3111 et 3112.
    collectif = compte.compte_collectif_id or compte_epargne
    compte_caisse = db.execute(
        select(Agency.compte_caisse_id).where(Agency.id == compte.agency_id)
    ).scalar_one()

    def resoudre(role: str) -> uuid.UUID:
        if role == "EPARGNE":
            if collectif is None:
                raise RattachementManquantError(
                    "le produit de ce compte n'a pas de compte d'épargne rattaché (plan comptable)"
                )
            return collectif
        if role == "CAISSE":
            if compte_caisse is None:
                raise RattachementManquantError(
                    "l'agence de ce compte n'a pas de compte de caisse rattaché"
                )
            return compte_caisse
        if role == "INTERETS":
            if compte_charge_interet is None:
                raise RattachementManquantError(
                    "le produit n'a pas de compte de charge d'intérêts rattaché"
                )
            return compte_charge_interet
        raise RattachementManquantError(f"rôle « {role} » inconnu dans le modèle d'écriture")

    return resoudre


def poser_ecriture_operation(
    db: Session,
    compte: SavingsAccount,
    code_operation: str,
    montant: int,
    par: uuid.UUID | None,
    *,
    entry_date: object | None = None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> JournalEntry:
    """Pose la pièce comptable équilibrée d'une opération sur `compte`.

    entry_date : date de la pièce (par défaut aujourd'hui). Le versement d'intérêts la date en FIN
    de période. Ne touche pas au solde du membre : l'appelant s'en charge, dans la même transaction.
    """
    jour = entry_date
    if jour is None:
        jour = db.execute(text("SELECT CURRENT_DATE")).scalar_one()
    return poser_depuis_schema(
        db,
        code=code_operation,
        montant=montant,
        resoudre_role=_resolveur(db, compte),
        entry_date=jour,
        par=par,
        description=f"{code_operation} {compte.account_number}",
        contexte=contexte,
    )
