"""Coordonnées des tiers (T2b) — téléphones / emails / adresses.

Une seule table (Contact), discriminée. Ce module porte l'écriture (ajout, suppression logique,
désignation du principal) et la lecture. La normalisation des téléphones vit dans telephone.py.

RÈGLES :
  - PÉRIMÈTRE : on ne touche pas les coordonnées d'un tiers hors de son agence. _charger_tier
    applique condition_perimetre -> 404 si invisible (jamais 403).
  - UNE PRINCIPALE PAR TYPE : désigner un principal débascule d'abord l'ancien du même type,
    dans la transaction ; l'index partiel de 0009 est le dernier rempart.
  - SUPPRESSION LOGIQUE AVEC MOTIF : deleted_at + deleted_by + deletion_reason.
  - DOUBLE TRACE : un lifecycle_event 'updated' (la fiche a changé) DANS la transaction, puis
    ecrire_audit() EN DERNIER (D5).
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.modules.audit.service import ContexteRequete, ecrire_audit
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.tiers.models import Contact, LifecycleEvent, Tier
from app.modules.tiers.telephone import TelephoneInvalideError, normaliser

RESSOURCE = "contact"
_AGENCE = Tier.__table__.c.primary_agency_id


class TierIntrouvableError(Exception):
    """Tiers inexistant, supprimé, OU hors périmètre. -> 404."""


class ContactIntrouvableError(Exception):
    """Contact inexistant, supprimé, ou d'un autre tiers. -> 404."""


# Ré-exporté pour que le router traduise le 422 (avec forcable) sans importer telephone.py.
__all__ = ["ContactIntrouvableError", "TelephoneInvalideError", "TierIntrouvableError"]


@dataclass(frozen=True)
class DonneesAdresse:
    address_line1: str | None = None
    address_line2: str | None = None
    quarter: str | None = None
    landmark: str | None = None
    city_id: uuid.UUID | None = None
    region_id: uuid.UUID | None = None
    country_id: uuid.UUID | None = None
    postal_code: str | None = None


def _charger_tier(db: Session, courant: UtilisateurCourant, tier_id: uuid.UUID) -> Tier:
    tier = db.execute(
        select(Tier).where(
            Tier.id == tier_id,
            courant.condition_perimetre(_AGENCE),
            Tier.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if tier is None:
        raise TierIntrouvableError()
    return tier


def _charger_contact(db: Session, tier_id: uuid.UUID, contact_id: uuid.UUID) -> Contact:
    contact = db.execute(
        select(Contact).where(
            Contact.id == contact_id,
            Contact.tier_id == tier_id,
            Contact.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if contact is None:
        raise ContactIntrouvableError()
    return contact


def _debasculer_principal(db: Session, tier_id: uuid.UUID, contact_type: str) -> None:
    """Retire le drapeau principal de l'éventuel contact principal vivant du même type."""
    db.execute(
        update(Contact)
        .where(
            Contact.tier_id == tier_id,
            Contact.contact_type == contact_type,
            Contact.is_primary.is_(True),
            Contact.deleted_at.is_(None),
        )
        .values(is_primary=False)
    )


def _tracer_et_auditer(
    db: Session,
    courant: UtilisateurCourant,
    tier: Tier,
    action: str,
    description: str,
    contexte: ContexteRequete,
    contact_id: uuid.UUID,
) -> None:
    """Une coordonnée modifiée est un changement de la FICHE : lifecycle_event 'updated' +
    audit dédié (action tier.contact_*), l'audit portant le détail."""
    db.add(
        LifecycleEvent(
            tier_id=tier.id,
            event_type="updated",
            previous_status=tier.status,
            new_status=tier.status,
            reason=description,
            performed_by=courant.user_id,
        )
    )
    db.flush()
    ecrire_audit(
        db,
        action=action,
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=contact_id,
        agency_id=courant.agency_id,
        new_values={"tier_id": str(tier.id), "detail": description},
    )
    db.commit()


# --- ajouts ----------------------------------------------------------------------------


def ajouter_telephone(
    db: Session,
    courant: UtilisateurCourant,
    tier_id: uuid.UUID,
    *,
    phone: str,
    contact_subtype: str | None,
    is_primary: bool,
    forcer: bool,
    contexte: ContexteRequete,
) -> Contact:
    """Ajoute un téléphone NORMALISÉ. Lève TelephoneInvalideError si refusé (le router en fait un
    422, avec forcable pour proposer « enregistrer quand même »)."""
    tier = _charger_tier(db, courant, tier_id)
    resultat = normaliser(phone, forcer=forcer)  # peut lever TelephoneInvalideError

    if is_primary:
        _debasculer_principal(db, tier_id, "phone")
    contact = Contact(
        tier_id=tier_id,
        contact_type="phone",
        contact_subtype=contact_subtype,
        phone_raw=phone.strip(),
        phone_number=resultat.e164,
        phone_country_code=resultat.country_code,
        phone_normalized=resultat.normalise,
        is_primary=is_primary,
        created_by=courant.user_id,
        updated_by=courant.user_id,
    )
    db.add(contact)
    db.flush()
    detail = f"Téléphone ajouté : {resultat.e164}" + ("" if resultat.normalise else " (forcé)")
    _tracer_et_auditer(db, courant, tier, "tier.contact_added", detail, contexte, contact.id)
    return contact


def ajouter_email(
    db: Session,
    courant: UtilisateurCourant,
    tier_id: uuid.UUID,
    *,
    email: str,
    contact_subtype: str | None,
    is_primary: bool,
    contexte: ContexteRequete,
) -> Contact:
    tier = _charger_tier(db, courant, tier_id)
    if is_primary:
        _debasculer_principal(db, tier_id, "email")
    contact = Contact(
        tier_id=tier_id,
        contact_type="email",
        contact_subtype=contact_subtype,
        email_address=email.strip(),
        is_primary=is_primary,
        created_by=courant.user_id,
        updated_by=courant.user_id,
    )
    db.add(contact)
    db.flush()
    detail = f"Email ajouté : {email.strip()}"
    _tracer_et_auditer(db, courant, tier, "tier.contact_added", detail, contexte, contact.id)
    return contact


def ajouter_adresse(
    db: Session,
    courant: UtilisateurCourant,
    tier_id: uuid.UUID,
    *,
    donnees: DonneesAdresse,
    contact_subtype: str | None,
    is_primary: bool,
    contexte: ContexteRequete,
) -> Contact:
    tier = _charger_tier(db, courant, tier_id)
    if is_primary:
        _debasculer_principal(db, tier_id, "address")
    contact = Contact(
        tier_id=tier_id,
        contact_type="address",
        contact_subtype=contact_subtype,
        address_line1=donnees.address_line1,
        address_line2=donnees.address_line2,
        quarter=donnees.quarter,
        landmark=donnees.landmark,
        city_id=donnees.city_id,
        region_id=donnees.region_id,
        country_id=donnees.country_id,
        postal_code=donnees.postal_code,
        is_primary=is_primary,
        created_by=courant.user_id,
        updated_by=courant.user_id,
    )
    db.add(contact)
    db.flush()
    apercu = donnees.address_line1 or donnees.landmark or ""
    _tracer_et_auditer(
        db, courant, tier, "tier.contact_added", f"Adresse ajoutée : {apercu}", contexte, contact.id
    )
    return contact


# --- désigner principal / supprimer ----------------------------------------------------


def definir_principal(
    db: Session,
    courant: UtilisateurCourant,
    tier_id: uuid.UUID,
    contact_id: uuid.UUID,
    contexte: ContexteRequete,
) -> Contact:
    tier = _charger_tier(db, courant, tier_id)
    contact = _charger_contact(db, tier_id, contact_id)
    if not contact.is_primary:
        _debasculer_principal(db, tier_id, contact.contact_type)
        contact.is_primary = True
        contact.updated_by = courant.user_id
        db.flush()
        _tracer_et_auditer(
            db, courant, tier, "tier.contact_primary_set", "Coordonnée principale modifiée",
            contexte, contact.id,
        )
    else:
        db.commit()
    return contact


def supprimer_contact(
    db: Session,
    courant: UtilisateurCourant,
    tier_id: uuid.UUID,
    contact_id: uuid.UUID,
    motif: str | None,
    contexte: ContexteRequete,
) -> None:
    """Suppression LOGIQUE avec motif. La coordonnée sort des listes ; on ne l'efface jamais."""
    from datetime import UTC, datetime

    tier = _charger_tier(db, courant, tier_id)
    contact = _charger_contact(db, tier_id, contact_id)
    contact.deleted_at = datetime.now(UTC)
    contact.deleted_by = courant.user_id
    contact.deletion_reason = motif
    contact.is_primary = False  # une coordonnée supprimée ne reste pas « principale »
    db.flush()
    _tracer_et_auditer(
        db, courant, tier, "tier.contact_removed", "Coordonnée supprimée", contexte, contact.id
    )


# --- lecture ---------------------------------------------------------------------------


def lister_contacts(
    db: Session, courant: UtilisateurCourant, tier_id: uuid.UUID
) -> list[Contact] | None:
    """Coordonnées vivantes d'un tiers, ou None si le tiers est hors périmètre (-> 404)."""
    try:
        _charger_tier(db, courant, tier_id)
    except TierIntrouvableError:
        return None
    return list(
        db.execute(
            select(Contact)
            .where(Contact.tier_id == tier_id, Contact.deleted_at.is_(None))
            .order_by(Contact.contact_type, Contact.is_primary.desc(), Contact.created_at)
        )
        .scalars()
        .all()
    )
