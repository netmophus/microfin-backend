"""Pont Épargne -> comptabilité : traduire une opération d'un compte d'épargne en pièce.

Résout les rôles du modèle d'écriture depuis le compte d'épargne concerné :
  - EPARGNE -> le compte de dette du PRODUIT (product.compte_epargne_id, le 3111 collectif) ;
  - CAISSE  -> le compte de caisse de l'AGENCE (agency.compte_caisse_id, le 5721).
Si l'un de ces rattachements manque (provisoire non renseigné), on REFUSE proprement
(RattachementManquantError) : rien n'est écrit, message clair.

Ce module ne bouge NI le solde du membre NI aucun mouvement : il pose seulement la pièce
comptable. C'est E3 (dépôt/retrait) qui l'appellera, avec le verrou et le mouvement, dans une
seule transaction.
"""

import uuid

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete
from app.modules.comptabilite.models import JournalEntry
from app.modules.comptabilite.schemas_ecriture import ResolveurRole, poser_depuis_schema
from app.modules.epargne.models import Product, SavingsAccount
from app.modules.parameters.models import Agency

# Codes des modèles d'écriture des opérations d'épargne (seed comptabilite).
TYPE_DEPOT = "epargne.depot"
TYPE_RETRAIT = "epargne.retrait"
TYPE_CLOTURE = "epargne.cloture"
TYPE_INTERET = "epargne.interet"


class RattachementManquantError(Exception):
    """Un rôle ne se résout pas : compte non rattaché (produit ou agence). Refus propre."""


def _resolveur(db: Session, compte: SavingsAccount) -> ResolveurRole:
    compte_epargne, compte_charge_interet = db.execute(
        select(Product.compte_epargne_id, Product.compte_charge_interet_id).where(
            Product.id == compte.product_id
        )
    ).one()
    compte_caisse = db.execute(
        select(Agency.compte_caisse_id).where(Agency.id == compte.agency_id)
    ).scalar_one()

    def resoudre(role: str) -> uuid.UUID:
        if role == "EPARGNE":
            if compte_epargne is None:
                raise RattachementManquantError(
                    "le produit de ce compte n'a pas de compte d'épargne rattaché (plan comptable)"
                )
            return compte_epargne
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
