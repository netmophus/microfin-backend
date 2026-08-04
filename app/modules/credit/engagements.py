"""Vérificateur d'engagements du module Crédit — ce qui empêche de désactiver un tiers.

Branché DÈS CR3 (décaissement), pas attendu jusqu'aux remboursements (CR4) : un garde-fou vert
mais inerte ne protège rien (leçon déjà tirée avec l'Épargne). Condition pour l'instant : toute
demande DÉCAISSÉE bloque, point — pas encore de notion de « soldé », les remboursements
n'existent pas. CR4 affinera avec le capital restant dû réel (comme l'Épargne distingue solde
nul vs compte fermé, voir epargne/engagements.py)."""

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.engagements import Engagement, enregistrer_verificateur


def verifier_engagements_credit(db: Session, tier_id: uuid.UUID) -> list[Engagement]:
    """Rend un engagement par crédit DÉCAISSÉ (status='decaisse') du tiers."""
    demandes = db.execute(
        text(
            "SELECT application_number, montant_decide FROM credit.applications "
            "WHERE tier_id = :t AND status = 'decaisse'"
        ),
        {"t": tier_id},
    ).all()

    engagements: list[Engagement] = []
    for numero, montant in demandes:
        libelle = (
            f"Ce tiers a un crédit décaissé {numero} ({montant} F) en cours : "
            "il doit être soldé avant de désactiver ce tiers."
        )
        engagements.append(Engagement(domaine="credit", reference=numero, libelle=libelle))
    return engagements


def enregistrer() -> None:
    """Branche le vérificateur de crédit dans le registre. Appelé à l'assemblage de l'app."""
    enregistrer_verificateur(verifier_engagements_credit)
