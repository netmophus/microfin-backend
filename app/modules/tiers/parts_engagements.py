"""Vérificateur d'engagements du sociétariat — détenir des parts empêche la désactivation.

On ne fait pas disparaître un sociétaire qui a du capital dans la coopérative. Tant qu'un tier
détient des parts (libérées OU souscrites non libérées), sa désactivation est refusée ; le
remboursement (PS2) est ce qui la lèvera — la même boucle que l'épargne (compte ouvert -> refus ;
fermeture -> autorisé). Branché dans le registre neutre `app/core/engagements.py`, sans que le
module Tiers connaisse le détail : un méta-test vérifie que le garde-fou MORD (non-inertie).
"""

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.engagements import Engagement, enregistrer_verificateur


def verifier_engagements_parts(db: Session, tier_id: uuid.UUID) -> list[Engagement]:
    """Rend un engagement si le tier détient des parts (libérées ou non). Message ACTIONNABLE :
    il dit le capital (en francs) et QUOI faire (rembourser / annuler) avant de désactiver."""
    ligne = db.execute(
        text(
            "SELECT shares_liberees, shares_non_liberees FROM tiers.member_shares "
            "WHERE tier_id = :t"
        ),
        {"t": tier_id},
    ).one_or_none()
    if ligne is None:
        return []
    liberees, non_liberees = ligne
    total = liberees + non_liberees
    if total <= 0:
        return []
    unit_value = (
        db.execute(text("SELECT unit_value FROM tiers.share_parameters LIMIT 1")).scalar()
        or 0
    )
    capital = f"{liberees * unit_value:,} F".replace(",", " ")
    if liberees > 0:
        libelle = (
            f"Ce membre détient {total} part(s) sociale(s) — capital {capital}. "
            "Remboursez-les avant de le désactiver."
        )
    else:  # que des souscriptions non libérées (aucun capital versé) -> annuler
        libelle = (
            f"Ce membre a {non_liberees} part(s) souscrite(s) non libérée(s). "
            "Annulez-les avant de le désactiver."
        )
    return [Engagement(domaine="parts_sociales", reference=str(tier_id), libelle=libelle)]


def enregistrer() -> None:
    """Branche le vérificateur des parts dans le registre. Appelé à l'assemblage de l'app."""
    enregistrer_verificateur(verifier_engagements_parts)
