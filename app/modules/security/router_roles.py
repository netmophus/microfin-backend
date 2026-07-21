"""Lecture des rôles disponibles — GET /roles.

Alimente le sélecteur de rôles de la fiche utilisateur. L'attribution et le retrait, eux,
vivent sur /users/{id}/roles (router_users) : ce sont des écritures SUR un utilisateur.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.security.autorisation import UtilisateurCourant, exige
from app.modules.security.models import Role

router = APIRouter(prefix="/roles", tags=["rôles"])


class RoleItem(BaseModel):
    """Un rôle, tel que le sélecteur l'affiche. Construit champ par champ (règle projet)."""

    code: str
    name: str
    description: str | None


@router.get("", response_model=list[RoleItem])
def lister_roles(
    _: Annotated[UtilisateurCourant, Depends(exige("roles.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[RoleItem]:
    """Liste tous les rôles, ordonnés par code. Exige roles.read."""
    lignes = db.execute(select(Role.code, Role.name, Role.description).order_by(Role.code))
    return [RoleItem(code=r.code, name=r.name, description=r.description) for r in lignes]
