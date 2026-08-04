"""Vérificateur d'engagements du module Crédit — ce qui empêche de désactiver un tiers.

Branché DÈS CR3 (décaissement), pas attendu jusqu'aux remboursements (CR4) : un garde-fou vert
mais inerte ne protège rien (leçon déjà tirée avec l'Épargne). Condition (affinée en CR4,
annoncée dès CR3) : un crédit DÉCAISSÉ bloque tant qu'il lui reste au moins une échéance
'a_echoir' — dès que toutes ses échéances sont 'paye', il ne bloque plus. Miroir exact de
l'Épargne, qui distingue solde nul vs compte fermé (voir epargne/engagements.py)."""

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.engagements import Engagement, enregistrer_verificateur


def verifier_engagements_credit(db: Session, tier_id: uuid.UUID) -> list[Engagement]:
    """Rend un engagement par crédit DÉCAISSÉ du tiers ayant encore une échéance 'a_echoir'."""
    demandes = db.execute(
        text(
            "SELECT a.application_number, a.montant_decide "
            "FROM credit.applications a "
            "WHERE a.tier_id = :t AND a.status = 'decaisse' "
            "AND EXISTS ("
            "  SELECT 1 FROM credit.installments i "
            "  WHERE i.application_id = a.id AND i.status = 'a_echoir'"
            ")"
        ),
        {"t": tier_id},
    ).all()

    engagements: list[Engagement] = []
    for numero, montant in demandes:
        libelle = (
            f"Ce tiers a un crédit décaissé {numero} ({montant} F) non soldé : "
            "il doit être entièrement remboursé avant de désactiver ce tiers."
        )
        engagements.append(Engagement(domaine="credit", reference=numero, libelle=libelle))
    return engagements


def enregistrer() -> None:
    """Branche le vérificateur de crédit dans le registre. Appelé à l'assemblage de l'app."""
    enregistrer_verificateur(verifier_engagements_credit)
