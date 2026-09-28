"""Lecture des rôles disponibles — GET /roles.

Alimente le sélecteur de rôles de la fiche utilisateur. L'attribution et le retrait, eux,
vivent sur /users/{id}/roles (router_users) : ce sont des écritures SUR un utilisateur.

GET /roles/habilitations et GET /roles/{code}/permissions sont des routes SÉPARÉES, pas des
extensions de GET /roles : elles servent l'écran « Rôles et habilitations » (lot 1, lecture
seule), gardées par `roles.permissions.read` SEULE — jamais par `roles.read`. Les deux
permissions sont désormais portées par des rôles système différents (ADMIN_TECHNIQUE définit
les rôles, ADMIN_FONCTIONNEL les attribue et garde seul `roles.read` avec `roles.assign`) ;
un titulaire de `roles.permissions.read` sans `roles.read` doit pouvoir charger tout l'écran.
Incident du 28/09/2026 : l'écran appelait GET /roles pour peupler sa liste et recevait un 403
avec le compte `technique` (ADMIN_TECHNIQUE), qui n'a jamais eu `roles.read`.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.security.autorisation import UtilisateurCourant, exige
from app.modules.security.models import Permission, Role, RolePermission
from app.modules.security.router_permissions import PermissionItem

router = APIRouter(prefix="/roles", tags=["rôles"])

MESSAGE_ROLE_INTROUVABLE = "Rôle introuvable."


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


class RoleApercu(BaseModel):
    """Un rôle pour la LISTE de l'écran Rôles et habilitations : assez pour la ligne du
    tableau (badge Système, nombre de permissions), sans charger le détail de chacune."""

    code: str
    name: str
    description: str | None
    is_system: bool
    nb_permissions: int


@router.get("/habilitations", response_model=list[RoleApercu])
def lister_roles_habilitations(
    _: Annotated[UtilisateurCourant, Depends(exige("roles.permissions.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[RoleApercu]:
    """Vue d'ensemble pour l'écran « Rôles et habilitations », AUTONOME de GET /roles (voir
    l'en-tête du module) : un seul aller-retour, pas de N+1 vers /roles/{code}/permissions."""
    lignes = db.execute(
        select(
            Role.code,
            Role.name,
            Role.description,
            Role.is_system,
            func.count(RolePermission.permission_id).label("nb_permissions"),
        )
        .outerjoin(RolePermission, RolePermission.role_id == Role.id)
        .group_by(Role.id)
        .order_by(Role.code)
    )
    return [
        RoleApercu(
            code=r.code,
            name=r.name,
            description=r.description,
            is_system=r.is_system,
            nb_permissions=r.nb_permissions,
        )
        for r in lignes
    ]


class RolePermissionsDetail(BaseModel):
    """Le détail d'UN rôle pour l'écran Rôles et habilitations : ses métadonnées, et la
    liste — déjà résolue — de ses permissions actuelles."""

    code: str
    name: str
    description: str | None
    is_system: bool
    permissions: list[PermissionItem]


@router.get("/{code}/permissions", response_model=RolePermissionsDetail)
def lire_permissions_role(
    code: str,
    _: Annotated[UtilisateurCourant, Depends(exige("roles.permissions.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> RolePermissionsDetail:
    """Un rôle et ses permissions actuelles, triées par module puis code — lecture seule
    (lot 1) : rien ici ne modifie quoi que ce soit."""
    role = db.execute(select(Role).where(Role.code == code)).scalar_one_or_none()
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_ROLE_INTROUVABLE
        )

    lignes = db.execute(
        select(Permission.code, Permission.module, Permission.description)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id == role.id)
        .order_by(Permission.module, Permission.code)
    )
    return RolePermissionsDetail(
        code=role.code,
        name=role.name,
        description=role.description,
        is_system=role.is_system,
        permissions=[
            PermissionItem(code=r.code, module=r.module, description=r.description)
            for r in lignes
        ],
    )
