"""Registre des ENGAGEMENTS bloquants — le point de couplage propre entre modules.

Désactiver un tiers doit refuser tant qu'il reste un engagement ouvert : compte d'épargne
ouvert (module Épargne), prêt en cours (module Crédit, à venir)… Plutôt que de faire connaître
ces modules au module Tiers (couplage en dur, et un cycle d'import), chaque module métier
ENREGISTRE ici son vérificateur. La désactivation les appelle tous et agrège TOUTES les raisons
d'un coup — même motif que « toutes les conditions d'un coup » de l'activation.

Le Tiers ne connaît donc aucun module métier ; le Crédit se branchera de la même façon, sans
toucher au Tiers. L'enregistrement se fait à l'assemblage de l'application (app.main), et un
méta-test vérifie qu'il a bien eu lieu (sinon le garde-fou serait vert mais INERTE).
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session


@dataclass(frozen=True)
class Engagement:
    """Un engagement ouvert qui empêche la désactivation d'un tiers."""

    domaine: str  # 'epargne', 'credit'…
    reference: str  # ce qui identifie l'engagement (n° de compte, de prêt…)
    libelle: str  # message ACTIONNABLE, prêt à afficher


VerificateurEngagements = Callable[[Session, uuid.UUID], list[Engagement]]

_VERIFICATEURS: list[VerificateurEngagements] = []


def enregistrer_verificateur(verificateur: VerificateurEngagements) -> None:
    """Enregistre un vérificateur d'engagements. Idempotent (rejouable sans doublonner)."""
    if verificateur not in _VERIFICATEURS:
        _VERIFICATEURS.append(verificateur)


def verificateurs_enregistres() -> tuple[VerificateurEngagements, ...]:
    """Expose les vérificateurs enregistrés (pour le méta-test de non-inertie)."""
    return tuple(_VERIFICATEURS)


def engagements_bloquants(db: Session, tier_id: uuid.UUID) -> list[Engagement]:
    """Agrège les engagements ouverts de TOUS les modules enregistrés (rien ne s'arrête au 1er)."""
    resultats: list[Engagement] = []
    for verificateur in _VERIFICATEURS:
        resultats.extend(verificateur(db, tier_id))
    return resultats
