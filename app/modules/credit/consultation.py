"""Lectures du module Crédit (CR1) — cloisonnées par agence.

Le chargé de prêt / comité ne voit que les dossiers de SON agence (condition_perimetre sur
agency_id). Un dossier hors périmètre est INTROUVABLE (le router rend 404), jamais 403.
"""

from collections.abc import Sequence
from typing import Any
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.credit.models import Application, Installment, Product
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.tiers.models import GroupProfile, IndividualProfile, LegalEntityProfile, Tier

# Tables cœur des sous-types (héritage joint) : on les joint par tier_id sans re-polymorphisme.
_T = Tier.__table__
_IND = IndividualProfile.__table__
_LE = LegalEntityProfile.__table__
_GP = GroupProfile.__table__


def _nom_tier() -> Any:
    """Nom lisible du tiers selon son type — miroir de epargne/consultation._nom_membre()."""
    return func.coalesce(
        func.nullif(func.trim(func.concat_ws(" ", _IND.c.last_name, _IND.c.first_name)), ""),
        _LE.c.legal_name,
        _GP.c.group_name,
        _T.c.tier_number,
    )


def lister_produits(db: Session) -> Sequence[Product]:
    """Les produits de crédit actifs (pour le choix à la demande)."""
    return db.execute(select(Product).where(Product.is_active).order_by(Product.code)).scalars().all()


def _requete_demandes(courant: UtilisateurCourant):
    return (
        select(
            Application,
            Product,
            _T.c.tier_number,
            _nom_tier().label("tier_nom"),
            _T.c.is_member,
        )
        .join(Product, Product.id == Application.product_id)
        .join(_T, _T.c.id == Application.tier_id)
        .outerjoin(_IND, _IND.c.tier_id == _T.c.id)
        .outerjoin(_LE, _LE.c.tier_id == _T.c.id)
        .outerjoin(_GP, _GP.c.tier_id == _T.c.id)
        .where(courant.condition_perimetre(Application.agency_id))
    )


def lister_demandes(db: Session, courant: UtilisateurCourant) -> Sequence[Any]:
    """Les demandes de crédit dans le périmètre de l'acteur, les plus récentes d'abord."""
    return db.execute(
        _requete_demandes(courant).order_by(Application.created_at.desc())
    ).all()


def lister_demandes_tier(
    db: Session, courant: UtilisateurCourant, tier_id: uuid.UUID
) -> Sequence[Any]:
    """Les demandes de crédit d'UN tiers, dans le périmètre de l'acteur, les plus récentes
    d'abord — pour l'onglet Crédit de sa fiche. Le router vérifie séparément que le tiers
    lui-même est dans le périmètre (404 sinon) ; le filtre agency_id ici est une seconde
    barrière, pas la seule."""
    return db.execute(
        _requete_demandes(courant)
        .where(Application.tier_id == tier_id)
        .order_by(Application.created_at.desc())
    ).all()


def lire_demande(db: Session, courant: UtilisateurCourant, application_id: uuid.UUID) -> Any | None:
    """Une demande + son produit + le nom du tiers, dans le périmètre, ou None (-> 404)."""
    return db.execute(
        _requete_demandes(courant).where(Application.id == application_id)
    ).first()


def lire_echeancier(
    db: Session, courant: UtilisateurCourant, application_id: uuid.UUID
) -> Sequence[Installment]:
    """L'échéancier PERSISTÉ (CR3) d'une demande, dans le périmètre — liste vide si non
    décaissée ou hors périmètre (le router distingue les deux via lire_demande)."""
    return (
        db.execute(
            select(Installment)
            .join(Application, Application.id == Installment.application_id)
            .where(
                Installment.application_id == application_id,
                courant.condition_perimetre(Application.agency_id),
            )
            .order_by(Installment.numero)
        )
        .scalars()
        .all()
    )
