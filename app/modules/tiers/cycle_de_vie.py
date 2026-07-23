"""Cycle de vie des tiers (T1e) — transitions de statut et activation (stub).

LA MACHINE À ÉTATS EST UNE DONNÉE, pas du code éparpillé : la table TRANSITIONS ci-dessous est
la source unique de vérité. Chaque transition connaît ses statuts source, sa cible, et son
éventuelle contrainte de type.

DEUX NIVEAUX, LE BON MESSAGE AU BON ENDROIT. La contrainte de type des transitions (decede
réservé aux personnes physiques, dissous aux morales/groupements) est la COPIE EXACTE des CHECK
de la migration 0008 (ck_tiers_deces_pp, ck_tiers_dissolution_pm). Le service la vérifie AVANT
d'écrire -> 409 propre au lieu d'un 500 opaque. La base reste le dernier rempart si un chemin de
code l'oubliait un jour.

SOFT DELETE UNIQUEMENT. deactivate pose deleted_at + status='desactive' ; jamais de DELETE.
La fiche sort alors des lectures normales. decede/dissous, eux, RESTENT visibles (pas de
deleted_at) : un membre décédé ne s'efface pas de l'annuaire — il y a une succession.

DOUBLE TRACE (D5). Chaque transition écrit un lifecycle_event DANS la transaction, puis
ecrire_audit() EN DERNIER (verrou consultatif du chaînage).

PÉRIMÈTRE À L'ÉCRITURE. On ne pilote pas une fiche hors de son agence : _charger_pour_ecriture
applique condition_perimetre + FOR UPDATE. Hors périmètre -> 404 (n'existe pas de mon point de
vue), jamais 403. Le FOR UPDATE sérialise deux transitions concurrentes sur la même fiche.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.service import ContexteRequete, ecrire_audit
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.tiers.models import LifecycleEvent, Tier

RESSOURCE = "tier"
_AGENCE = Tier.__table__.c.primary_agency_id  # colonne Core (le périmètre attend un optionnel)

# Rendu lisible d'un statut pour les messages d'erreur (français, comme le reste des messages
# backend). Minimal, propre au module ; la table de traduction riche vit côté frontend.
_STATUT_LISIBLE: dict[str, str] = {
    "prospect": "prospect",
    "actif": "actif",
    "suspendu_temporaire": "suspendu",
    "suspendu_lcb": "suspendu (LBC/FT)",
    "desactive": "désactivé",
    "decede": "décédé",
    "dissous": "dissous",
    "fusionne": "fusionné",
}


# --- erreurs ---------------------------------------------------------------------------


class CibleIntrouvableError(Exception):
    """Fiche inexistante, supprimée, OU hors périmètre. Indistinctement -> 404."""


class TransitionIllegaleError(Exception):
    """Le statut courant n'autorise pas cette action. -> 409, en nommant le statut."""

    def __init__(self, statut: str) -> None:
        self.statut = statut
        super().__init__(statut)


class TypeIncompatibleError(Exception):
    """L'action ne s'applique pas à ce type de tiers (miroir du CHECK D4). -> 409."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class ActivationImpossibleError(Exception):
    """L'activation requiert des conditions non remplies. -> 412, avec TOUTES les manquantes."""

    def __init__(self, conditions: list["ConditionActivation"]) -> None:
        self.conditions = conditions
        super().__init__("activation impossible")


# --- conditions d'activation (stub T1e, point de greffe T3) ----------------------------


@dataclass(frozen=True)
class ConditionActivation:
    code: str
    libelle: str


KYC_NON_VALIDE = ConditionActivation("KYC_NON_VALIDE", "Validation KYC requise — module à venir.")


def conditions_activation_manquantes(db: Session, tier: Tier) -> list[ConditionActivation]:
    """Conditions NON remplies pour activer une fiche. On COLLECTE TOUT, on ne s'arrête pas à
    la première : un chargé de clientèle doit pouvoir tout corriger en une fois.

    En T1e le KYC n'existe pas -> condition systématiquement manquante. T3 ajoutera SES
    vérifications ICI (pièce vérifiée, téléphone, filtrage sanctions…), sans toucher à
    l'endpoint ni à la forme de réponse.
    """
    manquantes: list[ConditionActivation] = []
    manquantes.append(KYC_NON_VALIDE)
    # TODO(T3) : if not piece_verifiee -> manquantes.append(PIECE_NON_VERIFIEE) ; etc.
    return manquantes


# --- machine à états -------------------------------------------------------------------


@dataclass(frozen=True)
class Transition:
    event_type: str
    sources: frozenset[str]
    cible: str
    types: frozenset[str] | None  # contrainte de tier_type (miroir du CHECK D4), ou None
    message_type: str | None  # message si le type ne convient pas
    soft_delete: bool = False


TRANSITIONS: dict[str, Transition] = {
    "suspend": Transition("suspended", frozenset({"actif"}), "suspendu_temporaire", None, None),
    "reactivate": Transition(
        "reactivated", frozenset({"suspendu_temporaire"}), "actif", None, None
    ),
    "deactivate": Transition(
        "deactivated",
        frozenset({"prospect", "actif", "suspendu_temporaire", "suspendu_lcb"}),
        "desactive",
        None,
        None,
        soft_delete=True,
    ),
    "mark_deceased": Transition(
        "marked_deceased",
        frozenset({"prospect", "actif", "suspendu_temporaire"}),
        "decede",
        frozenset({"individual"}),
        "Un décès ne peut être enregistré que sur une personne physique.",
    ),
    "mark_dissolved": Transition(
        "marked_dissolved",
        frozenset({"prospect", "actif", "suspendu_temporaire"}),
        "dissous",
        frozenset({"legal_entity", "group"}),
        "Une dissolution ne concerne qu'une personne morale ou un groupement.",
    ),
}


def _charger_pour_ecriture(db: Session, courant: UtilisateurCourant, tier_id: uuid.UUID) -> Tier:
    """Charge la fiche SOUS VERROU, dans le périmètre de l'acteur. Sinon CibleIntrouvable (404)."""
    tier = db.execute(
        select(Tier)
        .where(
            Tier.id == tier_id,
            courant.condition_perimetre(_AGENCE),
            Tier.deleted_at.is_(None),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if tier is None:
        raise CibleIntrouvableError()
    return tier


def _tracer_et_auditer(
    db: Session,
    courant: UtilisateurCourant,
    tier: Tier,
    event_type: str,
    ancien: str,
    nouveau: str,
    motif: str | None,
    contexte: ContexteRequete,
) -> None:
    """lifecycle_event DANS la transaction, puis audit EN DERNIER, puis commit."""
    db.add(
        LifecycleEvent(
            tier_id=tier.id,
            event_type=event_type,
            previous_status=ancien,
            new_status=nouveau,
            reason=motif,
            performed_by=courant.user_id,
        )
    )
    db.flush()

    valeurs: dict[str, Any] = {"status": nouveau, "tier_number": tier.tier_number}
    if motif:
        valeurs["motif"] = motif
    ecrire_audit(
        db,
        action=f"tier.{event_type}",
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=tier.id,
        agency_id=courant.agency_id,
        old_values={"status": ancien},
        new_values=valeurs,
    )
    db.commit()


def executer_transition(
    db: Session,
    courant: UtilisateurCourant,
    tier_id: uuid.UUID,
    nom: str,
    contexte: ContexteRequete,
    motif: str | None = None,
) -> Tier:
    """Applique une transition de la table TRANSITIONS. 409 si statut/type incompatible."""
    transition = TRANSITIONS[nom]
    tier = _charger_pour_ecriture(db, courant, tier_id)

    if tier.status not in transition.sources:
        raise TransitionIllegaleError(tier.status)
    # Miroir du CHECK D4 : arrêté ici en 409, jamais laissé filer jusqu'au 500 de la base.
    if transition.types is not None and tier.tier_type not in transition.types:
        assert transition.message_type is not None
        raise TypeIncompatibleError(transition.message_type)

    ancien = tier.status
    maintenant = datetime.now(UTC)
    tier.status = transition.cible
    tier.updated_by = courant.user_id
    if nom == "suspend":
        tier.suspended_at = maintenant
        tier.suspended_by = courant.user_id
        tier.suspension_reason = motif
    elif nom == "reactivate":
        tier.suspended_at = None
        tier.suspended_by = None
        tier.suspension_reason = None
    elif transition.soft_delete:
        tier.deleted_at = maintenant  # soft delete : jamais de suppression physique

    _tracer_et_auditer(
        db, courant, tier, transition.event_type, ancien, transition.cible, motif, contexte
    )
    return tier


def activer(
    db: Session, courant: UtilisateurCourant, tier_id: uuid.UUID, contexte: ContexteRequete
) -> Tier:
    """Active une fiche prospect — STUB en T1e : renvoie systématiquement les conditions
    manquantes (412), car le KYC n'existe pas encore. La logique d'activation effective est en
    place ; seules les CONDITIONS sont vides de contenu réel jusqu'à T3."""
    tier = _charger_pour_ecriture(db, courant, tier_id)
    if tier.status != "prospect":
        raise TransitionIllegaleError(tier.status)

    manquantes = conditions_activation_manquantes(db, tier)
    if manquantes:
        raise ActivationImpossibleError(manquantes)

    # Jamais atteint en T1e (il reste toujours au moins KYC_NON_VALIDE) — mais la transition
    # est écrite, prête pour T3.
    ancien = tier.status
    tier.status = "actif"
    tier.activated_at = datetime.now(UTC)
    tier.activated_by = courant.user_id
    tier.updated_by = courant.user_id
    _tracer_et_auditer(db, courant, tier, "activated", ancien, "actif", None, contexte)
    return tier


def message_transition_illegale(statut: str) -> str:
    """Message 409 clair, nommant le statut courant en français."""
    return f"Action impossible : la fiche est « {_STATUT_LISIBLE.get(statut, statut)} »."
