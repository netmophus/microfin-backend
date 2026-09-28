"""Lecture du catalogue des permissions — GET /permissions.

Lot 1 (lecture seule) de l'écran « Rôles et habilitations » : aucune écriture ici, aucun
rattachement à un rôle — juste le catalogue complet, pour l'écran comme pour
`router_roles.py::lire_permissions_role` qui réutilise `PermissionItem`.

Gardé par `roles.permissions.read`, DISTINCTE de `roles.read` (celle-ci n'alimente que le
sélecteur de rôle de la fiche utilisateur, `router_roles.py` — elle ne dit rien des
permissions elles-mêmes, un titulaire de `roles.read` seul ne doit pas voir ce catalogue).
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.security.autorisation import UtilisateurCourant, exige
from app.modules.security.models import Permission

router = APIRouter(prefix="/permissions", tags=["permissions"])


class PermissionItem(BaseModel):
    """Une permission, telle que le catalogue l'affiche. Le groupement par module vit à
    l'écran (colonne déjà présente, tri suffisant ici)."""

    code: str
    module: str
    description: str | None


@router.get("", response_model=list[PermissionItem])
def lister_permissions(
    _: Annotated[UtilisateurCourant, Depends(exige("roles.permissions.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[PermissionItem]:
    """Catalogue complet, trié par module puis code."""
    lignes = db.execute(
        select(Permission.code, Permission.module, Permission.description).order_by(
            Permission.module, Permission.code
        )
    )
    return [
        PermissionItem(code=r.code, module=r.module, description=r.description) for r in lignes
    ]
