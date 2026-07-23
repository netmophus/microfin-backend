"""Lecture des tiers (T1d) — fiche, liste cloisonnée, frise. Aucune écriture.

DEUX NIVEAUX, DEUX REQUÊTES. La vue RÉSUMÉE (read.basic) et la fiche COMPLÈTE (read) ne sont
pas un même chargement filtré après coup : ce sont deux requêtes SQL distinctes. Le résumé
(`_requete_resume`) ne SELECT QUE des colonnes non sensibles (numéro, nom d'affichage, type,
statut, agence) ; il ne lit JAMAIS les colonnes KYC/socio-éco. Un porteur de read.basic ne
peut rien faire fuiter parce que rien de sensible n'est chargé pour lui.

CLOISONNEMENT DANS LA REQUÊTE. Le filtre `condition_perimetre(primary_agency_id)` vit dans le
WHERE, jamais en contrôle après lecture. Une fiche hors périmètre n'est pas trouvée -> le
service rend None -> l'appelant lève 404 (n'existe pas de mon point de vue), jamais 403. Et le
total de la liste suit le MÊME filtre, sinon le compteur trahirait l'effectif du réseau.

`deleted_at IS NULL` : une fiche désactivée sort des lectures normales (tiers.read.deleted,
différé, la montrera aux auditeurs).
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, Row, Select, func, or_, select
from sqlalchemy.orm import Session

from app.modules.security.autorisation import UtilisateurCourant
from app.modules.security.models import User
from app.modules.tiers.models import (
    Contact,
    GroupProfile,
    IndividualProfile,
    LegalEntityProfile,
    LifecycleEvent,
    Tier,
)
from app.modules.tiers.schemas import EvenementTimeline, PageTiers, TierResume

TAILLE_PAGE_DEFAUT = 25
TAILLE_PAGE_MAX = 100

# Tables (Core) : le résumé se construit par colonnes explicites, pas par l'entité ORM — ainsi
# les colonnes sensibles ne sont même pas nommées dans le SELECT.
_T = Tier.__table__
_IND = IndividualProfile.__table__
_LE = LegalEntityProfile.__table__
_GP = GroupProfile.__table__
_LC = LifecycleEvent.__table__
_U = User.__table__
_C = Contact.__table__


@dataclass(frozen=True)
class FiltresTiers:
    q: str | None = None
    tier_type: str | None = None
    status: str | None = None


def _nom_affichage() -> ColumnElement[str]:
    """Nom lisible selon le type, en une expression SQL. tier_number en dernier recours."""
    return func.coalesce(
        func.concat_ws(" ", _IND.c.last_name, _IND.c.first_name),
        _LE.c.legal_name,
        _GP.c.group_name,
        _T.c.tier_number,
    )


def _requete_resume(courant: UtilisateurCourant, filtres: FiltresTiers) -> Select[tuple[Any, ...]]:
    """SELECT des SEULES colonnes sûres, joint aux enfants pour le nom, filtré par périmètre."""
    nom = _nom_affichage()
    stmt = (
        select(
            _T.c.id,
            _T.c.tier_number,
            _T.c.tier_type,
            _T.c.status,
            _T.c.primary_agency_id,
            nom.label("display_name"),
        )
        .select_from(_T)
        .outerjoin(_IND, _IND.c.tier_id == _T.c.id)
        .outerjoin(_LE, _LE.c.tier_id == _T.c.id)
        .outerjoin(_GP, _GP.c.tier_id == _T.c.id)
        .where(
            courant.condition_perimetre(_T.c.primary_agency_id),
            _T.c.deleted_at.is_(None),
        )
    )
    if filtres.tier_type:
        stmt = stmt.where(_T.c.tier_type == filtres.tier_type)
    if filtres.status:
        stmt = stmt.where(_T.c.status == filtres.status)
    if filtres.q:
        motif = f"%{filtres.q}%"
        stmt = stmt.where(or_(_T.c.tier_number.ilike(motif), nom.ilike(motif)))
    return stmt


def _en_resume(ligne: Row[Any]) -> TierResume:
    return TierResume(
        id=ligne.id,
        tier_number=ligne.tier_number,
        tier_type=ligne.tier_type,
        display_name=ligne.display_name,
        status=ligne.status,
        primary_agency_id=ligne.primary_agency_id,
    )


def lister(
    db: Session,
    courant: UtilisateurCourant,
    filtres: FiltresTiers,
    *,
    page: int = 1,
    taille: int = TAILLE_PAGE_DEFAUT,
) -> PageTiers:
    """Liste paginée de RÉSUMÉS, cloisonnée. Le total suit le même filtre que les lignes."""
    base = _requete_resume(courant, filtres)
    total = db.execute(select(func.count()).select_from(base.subquery())).scalar_one()
    lignes = db.execute(
        base.order_by(_T.c.created_at.desc(), _T.c.id.desc())
        .limit(taille)
        .offset((page - 1) * taille)
    ).all()
    return PageTiers(
        lignes=[_en_resume(ligne) for ligne in lignes],
        total=total,
        page=page,
        taille=taille,
    )


def lire_resume(db: Session, courant: UtilisateurCourant, tier_id: uuid.UUID) -> TierResume | None:
    """Vue résumée d'UNE fiche (read.basic) — colonnes sûres seulement, None si hors périmètre."""
    ligne = db.execute(_requete_resume(courant, FiltresTiers()).where(_T.c.id == tier_id)).first()
    return _en_resume(ligne) if ligne is not None else None


def lire_complet(db: Session, courant: UtilisateurCourant, tier_id: uuid.UUID) -> Tier | None:
    """Fiche COMPLÈTE (read) — chargement polymorphe, ou None si hors périmètre.

    select(Tier) rend le sous-type concret (Individual/LegalEntity/Group) via la jointure
    d'héritage ; le router le convertit avec le bloc de champs adéquat.
    """
    return db.execute(
        select(Tier).where(
            Tier.id == tier_id,
            # Colonne Core (non nullable côté ORM) : le périmètre attend une colonne optionnelle.
            courant.condition_perimetre(_T.c.primary_agency_id),
            Tier.deleted_at.is_(None),
        )
    ).scalar_one_or_none()


def telephone_principal(db: Session, tier_id: uuid.UUID) -> str | None:
    """Téléphone principal de la fiche, LU DEPUIS LES CONTACTS (T2b). Repli sur la colonne legacy
    tiers.primary_phone tant qu'elle existe : une fiche ancienne dont le backfill a échoué (numéro
    inexploitable) garde son numéro affiché. Le périmètre est déjà vérifié par l'appelant."""
    e164: str | None = db.execute(
        select(_C.c.phone_number).where(
            _C.c.tier_id == tier_id,
            _C.c.contact_type == "phone",
            _C.c.is_primary.is_(True),
            _C.c.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if e164 is not None:
        return e164
    legacy: str | None = db.execute(
        select(_T.c.primary_phone).where(_T.c.id == tier_id)
    ).scalar_one_or_none()
    return legacy


def _est_visible(db: Session, courant: UtilisateurCourant, tier_id: uuid.UUID) -> bool:
    """La fiche est-elle dans le périmètre de l'appelant ? (sans charger ses données)."""
    return (
        db.execute(
            select(_T.c.id).where(
                _T.c.id == tier_id,
                courant.condition_perimetre(_T.c.primary_agency_id),
                _T.c.deleted_at.is_(None),
            )
        ).first()
        is not None
    )


def timeline(
    db: Session, courant: UtilisateurCourant, tier_id: uuid.UUID
) -> list[EvenementTimeline] | None:
    """Frise chronologique d'une fiche, ou None si elle est hors périmètre (-> 404).

    On vérifie d'abord la visibilité de la fiche : sans elle, on divulguerait l'existence d'un
    tiers d'une autre agence en révélant ses événements.
    """
    if not _est_visible(db, courant, tier_id):
        return None
    lignes = db.execute(
        select(
            _LC.c.performed_at,
            _LC.c.event_type,
            _LC.c.previous_status,
            _LC.c.new_status,
            _LC.c.reason,
            func.concat_ws(" ", _U.c.last_name, _U.c.first_name).label("auteur_nom"),
        )
        .select_from(_LC)
        .outerjoin(_U, _U.c.id == _LC.c.performed_by)
        .where(_LC.c.tier_id == tier_id)
        .order_by(_LC.c.performed_at.desc(), _LC.c.id.desc())
    ).all()
    return [
        EvenementTimeline(
            occurred_at=ligne.performed_at,
            event_type=ligne.event_type,
            previous_status=ligne.previous_status,
            new_status=ligne.new_status,
            reason=ligne.reason,
            auteur_nom=ligne.auteur_nom or None,
        )
        for ligne in lignes
    ]
