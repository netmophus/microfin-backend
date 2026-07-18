"""Brique d'autorisation (bloc 4a) — l'utilisateur courant et la vérification de permission.

C'est la brique que TOUS les modules (tiers, comptabilité, caisse, épargne, crédit)
utiliseront pour protéger leurs routes. Conçue pour durer et pour que protéger une route
soit trivial :

    # gate pur — la route ignore qui appelle
    @router.get("/users", dependencies=[Depends(exige("users.read"))])
    # gate + l'utilisateur, quand la route a besoin de son périmètre
    def lister(courant: UtilisateurCourant = Depends(exige("users.read"))): ...

401 vs 403, stricts : utilisateur_courant lève 401 (token absent/invalide/expiré/mauvais
type) ; exige lève 403 (authentifié mais pas le droit). Jamais l'un pour l'autre.

PORTÉE (C6). L'utilisateur courant expose voit_tout (détient-il perimetre.reseau ?) et
perimetre_agence() — None s'il voit tout, sinon l'agence à laquelle un service doit filtrer.
Le gate de permission est GROSSIER (403 « tu ne peux pas faire cette action du tout », sans
fuite). Le cloisonnement fin par agence est un FILTRE de requête : une ligne hors périmètre
n'est pas trouvée → 404 naturel, jamais un 403 qui révélerait son existence.

RÉSOLUTION. Le token porte les CODES de rôles, pas les permissions : on résout rôles →
permissions en base à chaque requête. La matrice reste ainsi source unique de vérité, et un
correctif de matrice prend effet immédiatement (pas au bout des 15 min du token).

RÉVOCATION. L'état du compte (is_active/verrou/deleted) n'est PAS re-vérifié ici : la
résolution de permissions ne touche pas la table users. La fenêtre de 15 min de l'access
token est assumée ; désactiver un utilisateur devra révoquer ses sessions (4c) pour tuer le
refresh tout de suite.
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.applications import Starlette

from app.core.database import get_db
from app.modules.security.jwt import JetonError, decoder_access_token
from app.modules.security.models import Permission, Role, RolePermission

# Marqueur de portée réseau (seed §5+). Le détenir = voir toutes les agences ; sinon
# cloisonné à l'agence courante. Doit rester synchronisé avec le seed.
PERMISSION_RESEAU = "perimetre.reseau"

MESSAGE_NON_AUTHENTIFIE = "Authentification requise."
MESSAGE_PERMISSION_INSUFFISANTE = "Vous n'avez pas la permission requise."


@dataclass(frozen=True)
class UtilisateurCourant:
    """L'utilisateur derrière la requête, résolu depuis son access token + la matrice.

    Immuable : ce qu'on a résolu pour cette requête ne change pas en cours de route.
    Ne porte AUCUN champ sensible (ni hash, ni token) : juste identité, rôles, permissions
    et périmètre d'agence.
    """

    user_id: uuid.UUID
    roles: tuple[str, ...]
    permissions: frozenset[str]
    primary_agency_id: uuid.UUID | None
    # Agence COURANTE de la session (C6). Posée par authentifier après vérif user_agencies,
    # signée dans le token : on peut lui faire confiance.
    agency_id: uuid.UUID | None
    voit_tout: bool

    def a_permission(self, code: str) -> bool:
        return code in self.permissions

    def perimetre_agence(self) -> uuid.UUID | None:
        """None = voit tout le réseau (aucun filtre). Sinon l'agence à laquelle filtrer.

        Un service écrit alors : WHERE (:perimetre IS NULL OR agency_id = :perimetre).
        Une ligne hors périmètre n'est pas trouvée → 404 naturel, pas de fuite.
        """
        return None if self.voit_tout else self.agency_id


def _permissions_des_roles(db: Session, codes: tuple[str, ...]) -> frozenset[str]:
    """Résout l'ensemble des codes de permissions détenus par ces rôles (union)."""
    if not codes:
        return frozenset()
    lignes = db.execute(
        select(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(Role, Role.id == RolePermission.role_id)
        .where(Role.code.in_(codes))
    ).scalars()
    return frozenset(lignes)


_bearer = HTTPBearer(auto_error=False, description="Access token (JWT)")


def utilisateur_courant(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> UtilisateurCourant:
    """Dépendance « utilisateur courant ». 401 si le token est absent/invalide/du mauvais type.

    Toute erreur de jeton (signature, expiration, type refresh présenté comme access) donne
    un 401 — jamais un 403 : ne pas être authentifié n'est pas « ne pas avoir le droit ».
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=MESSAGE_NON_AUTHENTIFIE,
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = decoder_access_token(credentials.credentials)
    except JetonError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=MESSAGE_NON_AUTHENTIFIE,
            headers={"WWW-Authenticate": "Bearer"},
        ) from None

    permissions = _permissions_des_roles(db, claims.roles)
    return UtilisateurCourant(
        user_id=claims.sub,
        roles=claims.roles,
        permissions=permissions,
        primary_agency_id=claims.primary_agency_id,
        agency_id=claims.agency_id,
        voit_tout=PERMISSION_RESEAU in permissions,
    )


class ExigerPermission:
    """Dépendance déclarative : exige une permission précise, sinon 403.

    Classe appelable plutôt que fonction, pour deux raisons : elle porte
    permission_requise en attribut typé (le méta-test s'en sert pour détecter les routes
    non protégées), et FastAPI accepte une instance appelable comme dépendance.
    """

    def __init__(self, permission: str) -> None:
        self.permission_requise = permission

    def __call__(
        self, courant: Annotated[UtilisateurCourant, Depends(utilisateur_courant)]
    ) -> UtilisateurCourant:
        if self.permission_requise not in courant.permissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=MESSAGE_PERMISSION_INSUFFISANTE,
            )
        return courant


def exige(permission: str) -> ExigerPermission:
    """Fabrique la dépendance qui exige `permission`. Usage : Depends(exige("users.read"))."""
    return ExigerPermission(permission)


# --- détection des routes non protégées (garde-fou anti-oubli) ----------------------


def _calls_du_dependant(dependant: Dependant) -> Iterator[object]:
    """Parcourt récursivement les callables de l'arbre de dépendances d'une route."""
    for sous in dependant.dependencies:
        if sous.call is not None:
            yield sous.call
        yield from _calls_du_dependant(sous)


def route_protegee(route: APIRoute) -> bool:
    """Une route est protégée si son arbre de dépendances contient un ExigerPermission."""
    return any(
        getattr(call, "permission_requise", None) is not None
        for call in _calls_du_dependant(route.dependant)
    )


def routes_sans_permission(app: Starlette, publiques: frozenset[str]) -> list[str]:
    """Liste les routes (hors allowlist publique) qui n'exigent AUCUNE permission.

    Réutilisable par tout module : son test parcourt app.routes et affirme que le résultat
    est vide. Oublier de protéger une route devient un échec de test, pas une faille
    découverte en production.
    """
    manquantes: list[str] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path in publiques:
            continue
        if not route_protegee(route):
            methodes = ",".join(sorted(route.methods or set()))
            manquantes.append(f"{methodes} {route.path}")
    return manquantes
