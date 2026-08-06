"""Vérificateur d'engagements du module Crédit — ce qui empêche de désactiver un tiers.

Branché DÈS CR3 (décaissement), pas attendu jusqu'aux remboursements (CR4) : un garde-fou vert
mais inerte ne protège rien (leçon déjà tirée avec l'Épargne). Condition (affinée en CR4,
annoncée dès CR3, corrigée en CR5b) : un crédit DÉCAISSÉ bloque tant qu'il lui reste au moins
une échéance NON SOLDÉE (`status != 'paye'`) — dès que toutes ses échéances sont 'paye', il ne
bloque plus. `!= 'paye'`, PAS `== 'a_echoir'` : depuis le paiement partiel (migration 0037), une
échéance encore due peut être 'partiellement_paye', ni l'un ni l'autre au sens strict — une
sélection sur 'a_echoir' seul la manquerait et laisserait désactiver un tiers dont l'IMF détient
encore une créance ouverte (voir test_desactivation_refusee_si_echeance_partiellement_payee).
Miroir exact de l'Épargne, qui distingue solde nul vs compte fermé (voir epargne/engagements.py)."""

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.engagements import Engagement, enregistrer_verificateur


def verifier_engagements_credit(db: Session, tier_id: uuid.UUID) -> list[Engagement]:
    """Rend un engagement par crédit DÉCAISSÉ du tiers ayant encore une échéance NON SOLDÉE
    (à échoir OU partiellement payée)."""
    demandes = db.execute(
        text(
            "SELECT a.application_number, a.montant_decide "
            "FROM credit.applications a "
            "WHERE a.tier_id = :t AND a.status = 'decaisse' "
            "AND EXISTS ("
            "  SELECT 1 FROM credit.installments i "
            "  WHERE i.application_id = a.id AND i.status != 'paye'"
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
