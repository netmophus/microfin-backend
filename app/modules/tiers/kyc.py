"""Saisie des données KYC d'une personne physique (T3c) + recalcul du risque.

Point d'entrée pour renseigner origine des fonds, secteur d'activité, statut PPE et mode d'entrée
en relation — les données qui manquent à un prospect pour être activable. Toute mise à jour
DÉCLENCHE un recalcul de risque (declencheur='maj_kyc') : le score suit la donnée, jamais un
recalcul sur clic qu'on oublierait.

Périmètre : cloisonnement (condition_perimetre) -> 404 hors agence. KYC = personne physique en T3.
Double trace : lifecycle_event 'updated' + audit, puis commit (l'audit en dernier, D5). Le recalcul
archive lui-même une évaluation (append-only) AVANT l'audit.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.service import ContexteRequete, ecrire_audit
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.tiers.models import IndividualProfile, LifecycleEvent, Tier
from app.modules.tiers.risque import evaluer_et_enregistrer

RESSOURCE = "tier"
_AGENCE = Tier.__table__.c.primary_agency_id


class TierIntrouvableError(Exception):
    """Tiers inexistant, supprimé, ou hors périmètre. -> 404."""


class TypeNonSupporteError(Exception):
    """Le KYC détaillé ne concerne que la personne physique en T3. -> 422."""


@dataclass(frozen=True)
class DonneesKyc:
    origine_fonds: str | None
    secteur_activite_id: uuid.UUID | None
    ppe_status: bool
    ppe_relation: str | None
    ppe_fonction: str | None
    mode_entree_relation: str | None


def mettre_a_jour_kyc(
    db: Session,
    courant: UtilisateurCourant,
    tier_id: uuid.UUID,
    donnees: DonneesKyc,
    contexte: ContexteRequete,
) -> Tier:
    """Renseigne les données KYC d'une personne physique, puis RECALCULE le risque. Committe."""
    tier = db.execute(
        select(Tier).where(
            Tier.id == tier_id,
            courant.condition_perimetre(_AGENCE),
            Tier.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if tier is None:
        raise TierIntrouvableError()
    if not isinstance(tier, IndividualProfile):
        raise TypeNonSupporteError()

    tier.origine_fonds = donnees.origine_fonds
    tier.secteur_activite_id = donnees.secteur_activite_id
    tier.ppe_status = donnees.ppe_status
    tier.ppe_relation = donnees.ppe_relation
    tier.ppe_fonction = donnees.ppe_fonction
    tier.mode_entree_relation = donnees.mode_entree_relation
    tier.updated_by = courant.user_id
    db.flush()

    # Le score suit la donnée : recalcul + archive à chaque mise à jour KYC (declencheur dédié).
    evaluer_et_enregistrer(db, courant, tier, "maj_kyc")

    db.add(
        LifecycleEvent(
            tier_id=tier.id,
            event_type="updated",
            previous_status=tier.status,
            new_status=tier.status,
            reason="Mise à jour des données KYC",
            performed_by=courant.user_id,
        )
    )
    db.flush()
    ecrire_audit(
        db,
        action="tier.kyc_updated",
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=tier.id,
        agency_id=courant.agency_id,
        new_values={"risk_level": tier.risk_level, "risk_score": tier.risk_score},
    )
    db.commit()
    return tier
