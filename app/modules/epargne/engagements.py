"""Vérificateur d'engagements du module Épargne — ce qui empêche de désactiver un membre.

Règle (trois états d'un compte) :
  - compte OUVERT à solde NON NUL  -> bloque : « soldez-le d'abord » ;
  - compte OUVERT à solde ZÉRO      -> bloque aussi : « fermez-le d'abord » (un compte ouvert est
    une relation active, même vide) ;
  - compte FERMÉ (soldé ET clôturé) -> n'obstrue rien, la relation est terminée proprement.

La condition n'est donc pas « solde à zéro » mais « aucun compte OUVERT ». Vider un compte sans le
fermer ne suffit pas. Le vérificateur lit les comptes ACTIFS du membre et rend un engagement par
compte, avec le bon message.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.engagements import Engagement, enregistrer_verificateur


def verifier_engagements_epargne(db: Session, tier_id: uuid.UUID) -> list[Engagement]:
    """Rend un engagement par compte d'épargne OUVERT (status='actif') du membre."""
    comptes = db.execute(
        text(
            "SELECT account_number, balance FROM epargne.accounts "
            "WHERE tier_id = :t AND status = 'actif'"
        ),
        {"t": tier_id},
    ).all()

    engagements: list[Engagement] = []
    for numero, solde in comptes:
        if solde != 0:
            libelle = (
                f"Ce membre a un compte d'épargne {numero} avec un solde de {solde} F : "
                "soldez-le puis fermez-le avant de désactiver."
            )
        else:
            libelle = (
                f"Ce membre a un compte d'épargne {numero} ouvert (solde 0) : "
                "fermez-le avant de désactiver."
            )
        engagements.append(Engagement(domaine="epargne", reference=numero, libelle=libelle))
    return engagements


def enregistrer() -> None:
    """Branche le vérificateur d'épargne dans le registre. Appelé à l'assemblage de l'app."""
    enregistrer_verificateur(verifier_engagements_epargne)
