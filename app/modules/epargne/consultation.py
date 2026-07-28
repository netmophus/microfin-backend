"""Lectures du module Épargne (F1) — cloisonnées par agence.

Le caissier / chargé ne voit que les comptes de SON agence (condition_perimetre sur agency_id).
Un compte hors périmètre est INTROUVABLE (le router rend 404), jamais 403.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.comptabilite.models import JournalEntry
from app.modules.epargne.models import Product, SavingsAccount, SavingsMovement
from app.modules.security.autorisation import UtilisateurCourant


def lister_produits(db: Session) -> Sequence[Product]:
    """Les produits d'épargne actifs (pour le choix à l'ouverture)."""
    return db.execute(
        select(Product).where(Product.is_active).order_by(Product.code)
    ).scalars().all()


def lister_comptes_du_membre(
    db: Session, courant: UtilisateurCourant, tier_id: uuid.UUID
) -> Sequence[Any]:
    """Les comptes d'épargne d'un membre, dans le périmètre de l'acteur. (compte, produit)."""
    return db.execute(
        select(SavingsAccount, Product)
        .join(Product, Product.id == SavingsAccount.product_id)
        .where(
            SavingsAccount.tier_id == tier_id,
            courant.condition_perimetre(SavingsAccount.agency_id),
        )
        .order_by(SavingsAccount.opened_at)
    ).all()


def lire_compte(
    db: Session, courant: UtilisateurCourant, compte_id: uuid.UUID
) -> Any | None:
    """Le compte + son produit, dans le périmètre, ou None (-> 404)."""
    return db.execute(
        select(SavingsAccount, Product)
        .join(Product, Product.id == SavingsAccount.product_id)
        .where(
            SavingsAccount.id == compte_id,
            courant.condition_perimetre(SavingsAccount.agency_id),
        )
    ).first()


def lister_mouvements(db: Session, compte_id: uuid.UUID) -> Sequence[Any]:
    """Le relevé d'un compte : mouvements + n° de la pièce comptable liée."""
    return db.execute(
        select(SavingsMovement, JournalEntry.entry_number)
        .outerjoin(JournalEntry, JournalEntry.id == SavingsMovement.journal_entry_id)
        .where(SavingsMovement.account_id == compte_id)
        .order_by(SavingsMovement.created_at, SavingsMovement.id)
    ).all()
