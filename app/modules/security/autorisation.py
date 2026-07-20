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
condition_perimetre(colonne), qui rend la condition SQL de cloisonnement à poser dans le
WHERE. Le gate de permission est GROSSIER (403 « tu ne peux pas faire cette action du tout »,
sans fuite). Le cloisonnement fin par agence est un FILTRE de requête : une ligne hors
périmètre n'est pas trouvée → 404 naturel, jamais un 403 qui révélerait son existence.

RÉSOLUTION. Le token porte les CODES de rôles, pas les permissions : on résout rôles →
permissions en base à chaque requête. La matrice reste ainsi source unique de vérité, et un
correctif de matrice prend effet immédiatement (pas au bout des 15 min du token).

RÉVOCATION. L'état du compte (is_active/verrou/deleted) n'est PAS re-vérifié ici : la
résolution de permissions ne touche pas la table users. La fenêtre de 15 min de l'access
token est assumée ; désactiver un utilisateur devra révoquer ses sessions (4c) pour tuer le
refresh tout de suite.
"""

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import false, select, true
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement
from starlette.applications import Starlette

from app.core.database import get_db
from app.modules.security.jwt import JetonError, decoder_access_token
from app.modules.security.models import Permission, Role, RolePermission

# Marqueur de portée réseau (seed §5+). Le détenir = voir toutes les agences ; sinon
# cloisonné à l'agence courante. Doit rester synchronisé avec le seed.
PERMISSION_RESEAU = "perimetre.reseau"

MESSAGE_NON_AUTHENTIFIE = "Authentification requise."
MESSAGE_PERMISSION_INSUFFISANTE = "Vous n'avez pas la permission requise."
MESSAGE_MOT_DE_PASSE_A_RENOUVELER = (
    "Votre mot de passe doit être renouvelé avant toute autre action."
)
# Code machine renvoyé avec le 403 quand le renouvellement est dû. Le SPA s'en sert pour
# rediriger vers l'écran de changement au lieu d'afficher « accès refusé » — un message
# d'échec sur une contrainte que l'utilisateur PEUT lever lui-même serait une impasse.
CODE_MOT_DE_PASSE_A_RENOUVELER = "password_change_required"


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
    # §6 — mot de passe provisoire (compte créé, ou réinitialisé par un administrateur).
    # Tant qu'il est vrai, exige() refuse TOUTE action.
    doit_changer_mot_de_passe: bool = False

    def condition_perimetre_sur(
        self, construire: Callable[[uuid.UUID], ColumnElement[bool]]
    ) -> ColumnElement[bool]:
        """Variante pour les périmètres qui ne se lisent pas sur une seule colonne.

        `construire` reçoit l'agence à filtrer et rend la condition correspondante — un OR,
        un EXISTS sur une table de liaison, ce que le module veut. Elle n'est appelée QUE
        dans le cas où filtrer a un sens ; les deux cas limites (réseau, aucune agence) sont
        tranchés ici, une fois pour toutes.

        C'est le point : la règle fail-secure ne doit exister qu'à UN endroit. Un module qui
        recopierait « si voit_tout … sinon si agency_id is None … » finirait par en oublier
        une branche, et ce serait exactement la faille que ce fichier vient de corriger.
        """
        if self.voit_tout:
            return true()
        if self.agency_id is None:
            return false()
        return construire(self.agency_id)

    def condition_perimetre(self, colonne: ColumnElement[uuid.UUID | None]) -> ColumnElement[bool]:
        """Rend la condition SQL de cloisonnement à poser dans le WHERE d'une requête.

            .where(courant.condition_perimetre(User.primary_agency_id))

        Cas simple — le périmètre se lit sur une seule colonne. Pour un périmètre composite
        (rattachement OU habilitation), voir condition_perimetre_sur.

        Trois cas, dont le troisième est celui qui compte :

          - voit tout le réseau        → vrai, aucun filtre ;
          - cloisonné à une agence     → colonne == cette agence ;
          - ni réseau, NI agence       → faux : il ne voit RIEN.

        Ce troisième cas est atteignable — primary_agency_id est nullable, un compte peut
        n'être rattaché à aucune agence — et c'est pour lui que cette méthode rend une
        CONDITION plutôt qu'un identifiant d'agence. Rendre « l'agence à filtrer » obligeait
        l'appelant à interpréter None, or None y était ambigu : « voit tout » ET « aucune
        agence » donnaient la même valeur, si bien qu'un compte sans agence et sans
        perimetre.reseau se serait retrouvé SANS filtre, donc omniscient. Ici l'ambiguïté
        n'existe plus : le cas indécidable devient un refus, jamais une ouverture.

        Le filtre doit vivre DANS la requête, pas dans un contrôle après lecture : une ligne
        hors périmètre ne doit pas être trouvée du tout, sinon on choisit entre révéler son
        existence (403) et l'oubli d'un garde-fou.
        """
        return self.condition_perimetre_sur(lambda agence: colonne == agence)


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
        doit_changer_mot_de_passe=claims.must_change_password,
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
        # AVANT la permission : un mot de passe provisoire n'ouvre rien, même à qui détient
        # le droit. Placé ici parce que exige() est le point de passage OBLIGÉ de toute
        # route protégée — la contrainte hérite donc de la couverture du méta-test, au lieu
        # d'être un contrôle de plus que chaque module devrait penser à écrire.
        if courant.doit_changer_mot_de_passe:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=MESSAGE_MOT_DE_PASSE_A_RENOUVELER,
                headers={"X-Erreur-Code": CODE_MOT_DE_PASSE_A_RENOUVELER},
            )
        if self.permission_requise not in courant.permissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=MESSAGE_PERMISSION_INSUFFISANTE,
            )
        return courant


def exige(permission: str) -> ExigerPermission:
    """Fabrique la dépendance qui exige `permission`. Usage : Depends(exige("users.read"))."""
    return ExigerPermission(permission)


class ExigerAuthentification:
    """Protection « authentifié, sans permission particulière » — cas rare et justifié.

    Sert aux routes que TOUT utilisateur connecté doit pouvoir appeler, quel que soit son
    rôle : changer son propre mot de passe, demain consulter son profil. Leur donner une
    permission obligerait à l'accorder aux onze rôles, ce qui ferait du bruit dans la
    matrice sans rien y décider.

    Elle porte permission_requise = None : le méta-test la reconnaît comme une protection
    DÉLIBÉRÉE, et ces routes n'ont donc pas à rejoindre ROUTES_PUBLIQUES. C'est important —
    l'allowlist doit continuer à ne lister que des routes réellement ouvertes, sinon elle
    cesse d'être lisible comme l'inventaire de la surface non authentifiée.
    """

    permission_requise: str | None = None

    def __call__(
        self, courant: Annotated[UtilisateurCourant, Depends(utilisateur_courant)]
    ) -> UtilisateurCourant:
        return courant


def exige_authentification() -> ExigerAuthentification:
    """Dépendance « il faut un jeton valide, rien de plus ».

    NE VÉRIFIE PAS doit_changer_mot_de_passe, contrairement à exige() : c'est justement par
    une de ces routes que l'utilisateur lève le drapeau. La contrôler ici enfermerait le
    compte dehors — il ne pourrait plus rien faire, pas même ce qu'on lui demande de faire.
    """
    return ExigerAuthentification()


# --- détection des routes non protégées (garde-fou anti-oubli) ----------------------


def _calls_du_dependant(dependant: Dependant) -> Iterator[object]:
    """Parcourt récursivement les callables de l'arbre de dépendances d'une route."""
    for sous in dependant.dependencies:
        if sous.call is not None:
            yield sous.call
        yield from _calls_du_dependant(sous)


# Marqueur d'une protection délibérée. Les deux dépendances de ce module le portent :
# ExigerPermission avec un code, ExigerAuthentification avec None. C'est l'ATTRIBUT qui
# fait foi, pas sa valeur — sinon une route « authentifiée sans permission » passerait
# pour non protégée et devrait rejoindre l'allowlist des routes ouvertes, qui cesserait
# alors de dire la vérité.
ATTRIBUT_PROTECTION = "permission_requise"


def route_protegee(route: APIRoute) -> bool:
    """Une route est protégée si son arbre de dépendances porte une protection déclarée."""
    return any(hasattr(call, ATTRIBUT_PROTECTION) for call in _calls_du_dependant(route.dependant))


def routes_api(conteneur: object) -> Iterator[APIRoute]:
    """Toutes les APIRoute d'une application, y compris celles montées par include_router.

    LA DESCENTE EST LE POINT DÉLICAT. app.routes ne contient PAS les routes des routeurs
    inclus à plat : depuis FastAPI 0.13x, include_router y dépose un objet _IncludedRouter
    qui garde ses routes dans .original_router.routes. Un parcours naïf de app.routes ne
    voit donc que les routes déclarées par @app.get — c'est-à-dire presque aucune, puisque
    tout module sérieux passe par un APIRouter.

    Le défaut est SILENCIEUX : le garde-fou reste vert en n'inspectant rien. Il a été trouvé
    au bloc 4b, en constatant que /auth/login et /users n'apparaissaient nulle part dans le
    parcours. D'où test_le_meta_test_voit_les_routes_montees_par_routeur, qui vérifie que
    cette descente ramène bien des routes connues : sans lui, une montée de version de
    FastAPI pourrait rendre le garde-fou aveugle sans faire rougir un seul test.

    La descente est volontairement tolérante (routes, puis original_router.routes) : elle
    doit survivre à la prochaine réorganisation interne de FastAPI, pas coller à celle-ci.
    """
    routes = getattr(conteneur, "routes", None)
    if routes is None:
        original = getattr(conteneur, "original_router", None)
        routes = getattr(original, "routes", ()) if original is not None else ()
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        else:
            yield from routes_api(route)


def routes_sans_permission(app: Starlette, publiques: frozenset[str]) -> list[str]:
    """Liste les routes (hors allowlist publique) qui n'exigent AUCUNE permission.

    Réutilisable par tout module : son test parcourt l'application et affirme que le
    résultat est vide. Oublier de protéger une route devient un échec de test, pas une
    faille découverte en production.
    """
    manquantes: list[str] = []
    for route in routes_api(app):
        if route.path in publiques:
            continue
        if not route_protegee(route):
            methodes = ",".join(sorted(route.methods or set()))
            manquantes.append(f"{methodes} {route.path}")
    return manquantes
