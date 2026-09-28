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

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.security.autorisation import UtilisateurCourant, exige
from app.modules.security.models import Permission, Role, RolePermission
from app.modules.security.roles_ecriture import (
    CodeRoleDejaUtiliseError,
    DernierGardienPermissionsError,
    NouveauRole,
    PermissionInconnueError,
    RoleSystemeNonModifiableError,
    creer,
    modifier_metadonnees,
    remplacer_permissions,
    supprimer,
)
from app.modules.security.roles_ecriture import (
    RoleIntrouvableError as RoleIntrouvableEcritureError,
)
from app.modules.security.router import _contexte
from app.modules.security.router_permissions import PermissionItem

router = APIRouter(prefix="/roles", tags=["rôles"])

MESSAGE_ROLE_INTROUVABLE = "Rôle introuvable."
CODE_ROLE = r"^[A-Z][A-Z0-9_]{1,49}$"


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


class CreerRoleRequest(BaseModel):
    """Entrée de POST /roles. Pas de champ is_system : toujours False côté serveur (lot 2)."""

    code: str = Field(pattern=CODE_ROLE, description="MAJUSCULES, chiffres, underscore.")
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)


class ModifierRoleRequest(BaseModel):
    """Entrée de PATCH /roles/{code}. Modification PARTIELLE : seuls les champs fournis
    bougent (model_fields_set, même patron que ModifierUtilisateurRequest)."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)

    def modifications(self) -> dict[str, object]:
        return {champ: getattr(self, champ) for champ in self.model_fields_set}


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


def _vers_apercu(db: Session, role: Role) -> RoleApercu:
    nb_permissions = db.execute(
        select(func.count(RolePermission.permission_id)).where(
            RolePermission.role_id == role.id
        )
    ).scalar_one()
    return RoleApercu(
        code=role.code,
        name=role.name,
        description=role.description,
        is_system=role.is_system,
        nb_permissions=nb_permissions,
    )


@router.post("", response_model=RoleApercu, status_code=status.HTTP_201_CREATED)
def creer_role(
    corps: CreerRoleRequest,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("roles.create"))],
    db: Annotated[Session, Depends(get_db)],
) -> RoleApercu:
    """Crée un rôle PERSONNALISÉ (lot 2). is_system=False, toujours — jamais un paramètre."""
    try:
        role = creer(
            db,
            courant,
            NouveauRole(code=corps.code, name=corps.name, description=corps.description),
            _contexte(request),
        )
    except Exception as erreur:
        raise _traduire(erreur) from None
    return _vers_apercu(db, role)


@router.patch("/{code}", response_model=RoleApercu)
def modifier_role(
    code: str,
    corps: ModifierRoleRequest,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("roles.update"))],
    db: Annotated[Session, Depends(get_db)],
) -> RoleApercu:
    """Modifie le nom/la description d'un rôle PERSONNALISÉ. Refuse sur un rôle système
    (édition système : lot 4)."""
    try:
        role = modifier_metadonnees(db, courant, code, corps.modifications(), _contexte(request))
    except Exception as erreur:
        raise _traduire(erreur) from None
    return _vers_apercu(db, role)


@router.delete("/{code}", status_code=status.HTTP_204_NO_CONTENT)
def supprimer_role(
    code: str,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("roles.delete"))],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    """Supprime un rôle PERSONNALISÉ. Refuse sur un rôle système (le trigger DB, 0004, ne
    devrait jamais être le chemin normal)."""
    try:
        supprimer(db, courant, code, _contexte(request))
    except Exception as erreur:
        raise _traduire(erreur) from None


class RemplacerPermissionsRequest(BaseModel):
    """Entrée de PUT /roles/{code}/permissions. Remplace le jeu ENTIER, pas un ajout/retrait
    partiel — le motif est obligatoire, il n'est jamais stocké ailleurs que dans l'audit."""

    permission_codes: list[str] = Field(default_factory=list)
    motif: str = Field(min_length=1, max_length=500)


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


@router.put("/{code}/permissions", response_model=RolePermissionsDetail)
def remplacer_permissions_du_role(
    code: str,
    corps: RemplacerPermissionsRequest,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("roles.permissions.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> RolePermissionsDetail:
    """Remplace ATOMIQUEMENT le jeu de permissions d'un rôle PERSONNALISÉ. Refuse sur un rôle
    système. Garde-fou anti-blocage : refuse si plus aucun rôle actif ne détiendrait
    roles.permissions.manage après le changement."""
    try:
        role = remplacer_permissions(
            db,
            courant,
            code,
            frozenset(corps.permission_codes),
            corps.motif,
            _contexte(request),
        )
    except Exception as erreur:
        raise _traduire(erreur) from None

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


def _traduire(erreur: Exception) -> HTTPException:
    """Traduit une erreur du service d'écriture (lot 2) en réponse HTTP. Un seul endroit."""
    if isinstance(erreur, RoleIntrouvableEcritureError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_ROLE_INTROUVABLE)
    if isinstance(erreur, RoleSystemeNonModifiableError):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="L'édition des rôles système n'est pas encore disponible.",
        )
    if isinstance(erreur, CodeRoleDejaUtiliseError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Le code « {erreur.code} » est déjà utilisé.",
        )
    if isinstance(erreur, PermissionInconnueError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Permission inconnue : {erreur.code}.",
        )
    if isinstance(erreur, DernierGardienPermissionsError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "Impossible d'enregistrer : plus aucun rôle ne pourrait gérer les "
                "permissions après ce changement. Il doit toujours en rester au moins un."
            ),
        )
    raise erreur
