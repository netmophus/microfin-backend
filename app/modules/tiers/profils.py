"""Modification du profil d'une personne morale ou d'un groupement (lot PM/groupements).

Avant ce lot, seule la personne physique avait une route d'édition (PATCH /kyc) : une société ou un
groupement dont on se trompait sur le RCCM ou la forme juridique restait faux pour toujours.

Seuls les champs ENVOYÉS changent (un champ obligatoire ne peut pas être vidé, cf. les schémas).
Chaque modification est tracée : événement de cycle de vie, puis audit avec les anciennes et les
nouvelles valeurs des seuls champs réellement modifiés. Un RCCM ou un NIF déjà porté par une autre
fiche est signalé clairement, SANS nommer l'autre fiche (elle peut être hors du périmètre).
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.audit.service import ContexteRequete, ecrire_audit
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.tiers.models import (
    GroupProfile,
    LegalEntityProfile,
    LifecycleEvent,
    Tier,
)

RESSOURCE = "tier"
_AGENCE = Tier.__table__.c.primary_agency_id
_CONTRAINTE_RCCM = "uq_legal_entity_profiles_rccm"
_CONTRAINTE_NIF = "uq_legal_entity_profiles_nif"
_CHAMPS_TEXTE_LIBRE = ("rccm_number", "nif_number")


class TierIntrouvableError(Exception):
    """Fiche inexistante, supprimée, ou hors périmètre. -> 404."""


class TypeFicheError(Exception):
    """La fiche n'est pas du type attendu par la route. -> 422."""


class IdentifiantDejaUtiliseError(Exception):
    """RCCM ou NIF déjà rattaché à une AUTRE fiche. -> 422. Le message est celui de l'écran."""


class ReferenceInvalideError(Exception):
    """Pays ou devise inexistant. -> 422."""


def _jsonable(valeur: Any) -> Any:
    if isinstance(valeur, uuid.UUID | date | Decimal):
        return str(valeur) if not isinstance(valeur, date) else valeur.isoformat()
    return valeur


def _nettoyer(changements: dict[str, Any]) -> dict[str, Any]:
    """RCCM et NIF : espaces retirés ; une saisie vide devient NULL (l'index unique tolère NULL)."""
    propres = dict(changements)
    for champ in _CHAMPS_TEXTE_LIBRE:
        if champ in propres and propres[champ] is not None:
            propres[champ] = propres[champ].strip() or None
    return propres


def _traduire_integrite(erreur: IntegrityError) -> Exception:
    nom = str(getattr(getattr(erreur.orig, "diag", None), "constraint_name", "") or "")
    if nom == _CONTRAINTE_RCCM:
        return IdentifiantDejaUtiliseError("Ce numéro RCCM est déjà rattaché à une autre fiche.")
    if nom == _CONTRAINTE_NIF:
        return IdentifiantDejaUtiliseError("Ce NIF est déjà rattaché à une autre fiche.")
    return ReferenceInvalideError()


def modifier_profil(
    db: Session,
    courant: UtilisateurCourant,
    tier_id: uuid.UUID,
    type_attendu: str,
    changements: dict[str, Any],
    contexte: ContexteRequete,
) -> Tier:
    """Applique `changements` (champs envoyés uniquement) au profil. Committe."""
    tier = db.execute(
        select(Tier)
        .where(Tier.id == tier_id, courant.condition_perimetre(_AGENCE), Tier.deleted_at.is_(None))
        .with_for_update()
    ).scalar_one_or_none()
    if tier is None:
        raise TierIntrouvableError()
    if tier.tier_type != type_attendu or not isinstance(tier, LegalEntityProfile | GroupProfile):
        raise TypeFicheError()

    propres = _nettoyer(changements)
    avant: dict[str, Any] = {}
    apres: dict[str, Any] = {}
    for champ, valeur in propres.items():
        courante = getattr(tier, champ)
        if courante != valeur:
            avant[champ] = _jsonable(courante)
            apres[champ] = _jsonable(valeur)
    if not apres:
        return tier  # rien n'a changé : ni écriture, ni trace

    try:
        with db.begin_nested():
            for champ in apres:
                setattr(tier, champ, propres[champ])
            tier.updated_by = courant.user_id
            db.flush()
    except IntegrityError as erreur:
        raise _traduire_integrite(erreur) from None

    champs = ", ".join(sorted(apres))
    db.add(
        LifecycleEvent(
            tier_id=tier.id,
            event_type="updated",
            previous_status=tier.status,
            new_status=tier.status,
            reason=f"Profil modifié : {champs}",
            performed_by=courant.user_id,
        )
    )
    db.flush()
    ecrire_audit(
        db,
        action="tier.profile.updated",
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=tier.id,
        agency_id=courant.agency_id,
        old_values=avant,
        new_values=apres,
    )
    db.commit()
    return tier
