"""Endpoint de consultation du journal d'audit — GET /audit.

LECTURE SEULE, par principe : le journal est inviolable. Aucune route de modification ou de
suppression n'existe ici, et n'en existera jamais. Exige audit.read.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.audit.consultation import (
    TAILLE_PAGE_DEFAUT,
    TAILLE_PAGE_MAX,
    FiltresAudit,
    lister_audit,
)
from app.modules.security.autorisation import UtilisateurCourant, exige

router = APIRouter(prefix="/audit", tags=["audit"])


class AuditItem(BaseModel):
    """Une entrée du journal. Le CODE d'action est renvoyé tel quel — c'est le frontend qui
    le traduit en français lisible (table de correspondance côté écran)."""

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


class PageAudit(BaseModel):
    lignes: list[AuditItem]
    total: int
    page: int
    taille: int


@router.get("", response_model=PageAudit)
def lister_journal(
    _: Annotated[UtilisateurCourant, Depends(exige("audit.read"))],
    db: Annotated[Session, Depends(get_db)],
    action: Annotated[
        str | None, Query(description="Code d'action exact (ex. user.created).")
    ] = None,
    acteur_id: Annotated[uuid.UUID | None, Query(description="Qui a agi.")] = None,
    cible_id: Annotated[uuid.UUID | None, Query(description="Personne concernée.")] = None,
    date_debut: Annotated[datetime | None, Query(description="Début de période (inclus).")] = None,
    date_fin: Annotated[datetime | None, Query(description="Fin de période (exclue).")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    taille: Annotated[int, Query(ge=1, le=TAILLE_PAGE_MAX)] = TAILLE_PAGE_DEFAUT,
) -> PageAudit:
    """Journal paginé, du plus récent au plus ancien. Exige audit.read.

    Le filtre de période borne occurred_at et laisse PostgreSQL élaguer les partitions hors
    plage — c'est la façon de rester rapide sur un journal qui grossit sans fin.
    """
    resultat = lister_audit(
        db,
        FiltresAudit(
            action=action,
            acteur_id=acteur_id,
            cible_id=cible_id,
            date_debut=date_debut,
            date_fin=date_fin,
        ),
        page=page,
        taille=taille,
    )
    return PageAudit(
        lignes=[
            AuditItem(
                id=ligne.id,
                occurred_at=ligne.occurred_at,
                action=ligne.action,
                acteur_id=ligne.acteur_id,
                acteur_nom=ligne.acteur_nom,
                resource_type=ligne.resource_type,
                cible_id=ligne.cible_id,
                cible_nom=ligne.cible_nom,
                ip_address=ligne.ip_address,
                old_values=ligne.old_values,
                new_values=ligne.new_values,
            )
            for ligne in resultat.lignes
        ],
        total=resultat.total,
        page=resultat.page,
        taille=resultat.taille,
    )
