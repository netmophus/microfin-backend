"""Lectures du module Épargne (F1) — cloisonnées par agence.

Le caissier / chargé ne voit que les comptes de SON agence (condition_perimetre sur agency_id).
Un compte hors périmètre est INTROUVABLE (le router rend 404), jamais 403.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.comptabilite.models import JournalEntry
from app.modules.epargne.models import Product, SavingsAccount, SavingsMovement
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.tiers.models import GroupProfile, IndividualProfile, LegalEntityProfile, Tier

# Tables cœur des sous-types (héritage joint) : on les joint par tier_id sans re-polymorphisme.
_T = Tier.__table__
_IND = IndividualProfile.__table__
_LE = LegalEntityProfile.__table__
_GP = GroupProfile.__table__


def _nom_membre() -> Any:
    """Nom lisible du membre selon son type (physique / morale / groupement), tier_number en
    dernier recours — miroir de _nom_affichage des tiers."""
    return func.coalesce(
        func.nullif(func.trim(func.concat_ws(" ", _IND.c.last_name, _IND.c.first_name)), ""),
        _LE.c.legal_name,
        _GP.c.group_name,
        _T.c.tier_number,
    )


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


def rechercher_par_numero(
    db: Session, courant: UtilisateurCourant, numero: str
) -> Any | None:
    """Cherche un compte par son NUMÉRO (chemin du guichet), dans le périmètre. Rend
    (compte, produit, tier_id, nom_membre) ou None (-> 404). Le nom sert de vérification humaine."""
    return db.execute(
        select(SavingsAccount, Product, _T.c.id.label("tier_id"), _nom_membre().label("nom"))
        .join(Product, Product.id == SavingsAccount.product_id)
        .join(_T, _T.c.id == SavingsAccount.tier_id)
        .outerjoin(_IND, _IND.c.tier_id == _T.c.id)
        .outerjoin(_LE, _LE.c.tier_id == _T.c.id)
        .outerjoin(_GP, _GP.c.tier_id == _T.c.id)
        .where(
            SavingsAccount.account_number == numero,
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
