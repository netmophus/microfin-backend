"""Consultation du journal d'audit (lecture seule) — service de requête.

Rend visible le journal inviolable construit au tout début du projet. LECTURE SEULE, sans
détour : le modèle AuditLog refuse déjà INSERT/UPDATE/DELETE via l'ORM, et la base le
défend par trigger + chaînage. Ici on ne fait que lire.

PAS DE CLOISONNEMENT PAR AGENCE. Tous les rôles qui détiennent audit.read
(AUDITEUR_INTERNE, DIRECTION_GENERALE, RESPONSABLE_LBC_FT, ADMIN_TECHNIQUE) détiennent aussi
perimetre.reseau : l'audit est une fonction de supervision, l'auditeur voit tout le réseau
par nature. Le jour où un rôle aurait audit.read SANS la portée réseau, il faudrait filtrer
sur agency_id — mais ce n'est pas le cas, et l'introduire maintenant serait deviner.

PAGINATION OBLIGATOIRE. Le journal grossit à chaque action et se partitionne par mois : on
ne charge JAMAIS tout. Le filtre de PÉRIODE n'est pas qu'un confort — en bornant occurred_at,
il laisse PostgreSQL élaguer les partitions hors plage, donc ne pas les balayer.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session, aliased

from app.modules.audit.models import AuditLog
from app.modules.security.models import User

TAILLE_PAGE_DEFAUT = 25
TAILLE_PAGE_MAX = 100


@dataclass(frozen=True)
class FiltresAudit:
    """Filtres de consultation. Tous facultatifs ; combinés en ET."""

    action: str | None = None
    acteur_id: uuid.UUID | None = None
    cible_id: uuid.UUID | None = None
    # Bornes de période sur occurred_at. debut inclus, fin exclue.
    date_debut: datetime | None = None
    date_fin: datetime | None = None


@dataclass(frozen=True)
class LigneAudit:
    """Une entrée du journal, noms d'acteur et de cible déjà résolus."""

    id: uuid.UUID
    occurred_at: datetime
    action: str
    acteur_id: uuid.UUID | None
    acteur_nom: str | None
    resource_type: str | None
    cible_id: uuid.UUID | None
    cible_nom: str | None
    ip_address: str | None
    old_values: dict[str, Any] | None
    new_values: dict[str, Any] | None


@dataclass(frozen=True)
class PageAudit:
    lignes: Sequence[LigneAudit]
    total: int
    page: int
    taille: int


def _nom(first: str | None, last: str | None, username: str | None) -> str | None:
    """Nom d'affichage : « Prénom Nom », l'identifiant à défaut, None si l'utilisateur a
    disparu (user_id porte une FK, mais resource_id est un simple UUID sans contrainte)."""
    if username is None:
        return None
    complet = f"{first or ''} {last or ''}".strip()
    return complet or username


def _conditions(filtres: FiltresAudit) -> list[Any]:
    conditions: list[Any] = []
    if filtres.action:
        conditions.append(AuditLog.action == filtres.action)
    if filtres.acteur_id is not None:
        conditions.append(AuditLog.user_id == filtres.acteur_id)
    if filtres.cible_id is not None:
        conditions.append(AuditLog.resource_id == filtres.cible_id)
    if filtres.date_debut is not None:
        conditions.append(AuditLog.occurred_at >= filtres.date_debut)
    if filtres.date_fin is not None:
        conditions.append(AuditLog.occurred_at < filtres.date_fin)
    return conditions


def lister_audit(
    db: Session,
    filtres: FiltresAudit | None = None,
    page: int = 1,
    taille: int = TAILLE_PAGE_DEFAUT,
) -> PageAudit:
    """Page du journal, triée par date DÉCROISSANTE (le plus récent en haut).

    acteur (user_id, FK vers users) et cible (resource_id, quand resource_type = « user »)
    sont résolus en noms par deux jointures externes distinctes sur users. Externes : un
    compte supprimé garde sa ligne (soft-delete) mais ne doit pas faire disparaître
    l'événement du journal — le journal survit à ses acteurs.
    """
    filtres = filtres or FiltresAudit()
    taille = max(1, min(taille, TAILLE_PAGE_MAX))
    page = max(1, page)
    conditions = _conditions(filtres)

    total = db.execute(select(func.count()).select_from(AuditLog).where(*conditions)).scalar_one()

    acteur = aliased(User)
    cible = aliased(User)
    lignes = db.execute(
        select(
            AuditLog.id,
            AuditLog.occurred_at,
            AuditLog.action,
            AuditLog.user_id,
            AuditLog.resource_type,
            AuditLog.resource_id,
            AuditLog.ip_address,
            AuditLog.old_values,
            AuditLog.new_values,
            acteur.first_name,
            acteur.last_name,
            acteur.username,
            cible.first_name,
            cible.last_name,
            cible.username,
        )
        .select_from(AuditLog)
        .outerjoin(acteur, acteur.id == AuditLog.user_id)
        .outerjoin(
            cible,
            and_(cible.id == AuditLog.resource_id, AuditLog.resource_type == "user"),
        )
        .where(*conditions)
        # id en second critère : deux événements de même occurred_at (now() est figé au
        # début de transaction) gardent un ordre stable entre deux pages.
        .order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
        .offset((page - 1) * taille)
        .limit(taille)
    ).all()

    return PageAudit(
        lignes=[
            LigneAudit(
                id=r.id,
                occurred_at=r.occurred_at,
                action=r.action,
                acteur_id=r.user_id,
                acteur_nom=_nom(r[9], r[10], r[11]),
                resource_type=r.resource_type,
                cible_id=r.resource_id,
                cible_nom=_nom(r[12], r[13], r[14]),
                # INET revient en IPv4Address, pas str (dette connue) : on convertit ici,
                # au bord, pour que le schéma de sortie reçoive bien une chaîne.
                ip_address=str(r.ip_address) if r.ip_address is not None else None,
                old_values=r.old_values,
                new_values=r.new_values,
            )
            for r in lignes
        ],
        total=total,
        page=page,
        taille=taille,
    )
