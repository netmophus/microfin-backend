"""Écritures sur les rôles PERSONNALISÉS (lot 2) — création, métadonnées, permissions,
suppression.

Séparé de utilisateurs_ecriture.py (qui gère l'ATTRIBUTION d'un rôle à un utilisateur, bloc
4d) : ce fichier gère la DÉFINITION du rôle lui-même — surface de risque différente.

RÔLES SYSTÈME HORS PÉRIMÈTRE ICI. Toute écriture sur un rôle is_system=True est refusée
(RoleSystemeNonModifiableError) — leur édition est le lot 4, une fois le seed converti au
marqueur « géré manuellement » (lot 3). Le POST crée toujours is_system=False, jamais lu
d'un paramètre client.

GARDE-FOU ANTI-BLOCAGE (décidé) : il doit rester À TOUT MOMENT au moins un rôle actif qui
détient roles.permissions.manage — pas seulement « pas sur son propre rôle ». _verifier_au_
moins_un_gardien couvre les DEUX chemins qui pourraient faire disparaître le dernier porteur :
PUT .../permissions (qui la retirerait) ET DELETE (qui supprimerait le rôle qui la porte).
Théorique au lot 2 (la permission est sur ADMIN_TECHNIQUE, rôle système non supprimable ici),
mais posée au bon endroit pour rester correcte quand le lot 4 rendra les rôles système
éditables.

MOTIF (PUT .../permissions) : jamais stocké en colonne dédiée, seulement dans new_values de
l'audit — qui/quoi/quand/motif suffit, pas de migration pour ça.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.modules.audit.service import ContexteRequete, ecrire_audit
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.security.models import Permission, Role, RolePermission

RESSOURCE = "role"
PERMISSION_GARDIENNE = "roles.permissions.manage"


class ActionRole:
    """Actions d'audit du périmètre rôles (lot 2). Format module.action, comme les autres."""

    CREATED = "role.created"
    UPDATED = "role.updated"
    DELETED = "role.deleted"
    PERMISSIONS_REPLACED = "role.permissions_replaced"


# --- erreurs ---------------------------------------------------------------------------


class RoleIntrouvableError(Exception):
    """Le code de rôle demandé ne correspond à aucun rôle."""


class RoleSystemeNonModifiableError(Exception):
    """Édition d'un rôle système : hors périmètre du lot 2 (viendra au lot 4)."""

    def __init__(self, code: str) -> None:
        super().__init__(f"Le rôle système « {code} » n'est pas modifiable ici.")
        self.code = code


class CodeRoleDejaUtiliseError(Exception):
    """Le code demandé pour un nouveau rôle est déjà pris."""

    def __init__(self, code: str) -> None:
        super().__init__(f"Le code « {code} » est déjà utilisé.")
        self.code = code


class PermissionInconnueError(Exception):
    """Un code de permission du corps de requête ne correspond à rien en base."""

    def __init__(self, code: str) -> None:
        super().__init__(f"Permission inconnue : {code}.")
        self.code = code


class DernierGardienPermissionsError(Exception):
    """Ce changement ferait disparaître le dernier rôle détenant roles.permissions.manage."""


# --- helpers ----------------------------------------------------------------------------


def _role_par_code(db: Session, code: str) -> Role:
    role = db.execute(select(Role).where(Role.code == code)).scalar_one_or_none()
    if role is None:
        raise RoleIntrouvableError(code)
    return role


def _permissions_du_role(db: Session, role_id: uuid.UUID) -> frozenset[str]:
    return frozenset(
        db.execute(
            select(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .where(RolePermission.role_id == role_id)
        ).scalars()
    )


def _verifier_au_moins_un_gardien(
    db: Session, role_code_modifie: str, permissions_apres: frozenset[str]
) -> None:
    """Refuse si, une fois le changement appliqué, plus aucun rôle actif ne détiendrait
    roles.permissions.manage. Appelée par remplacer_permissions ET supprimer — les deux
    seuls chemins qui peuvent faire disparaître ce droit d'un rôle qui le porte."""
    if PERMISSION_GARDIENNE in permissions_apres:
        return
    autres = frozenset(
        db.execute(
            select(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(Role, Role.id == RolePermission.role_id)
            .where(Role.code != role_code_modifie)
        ).scalars()
    )
    if PERMISSION_GARDIENNE not in autres:
        raise DernierGardienPermissionsError()


def _etat_auditable(role: Role) -> dict[str, object]:
    return {"code": role.code, "name": role.name, "description": role.description}


# --- création ----------------------------------------------------------------------------


@dataclass(frozen=True)
class NouveauRole:
    code: str
    name: str
    description: str | None


def creer(
    db: Session, courant: UtilisateurCourant, nouveau: NouveauRole, contexte: ContexteRequete
) -> Role:
    """Crée un rôle PERSONNALISÉ. is_system posé à False ici, jamais lu d'un paramètre client."""
    existant = db.execute(select(Role).where(Role.code == nouveau.code)).scalar_one_or_none()
    if existant is not None:
        raise CodeRoleDejaUtiliseError(nouveau.code)

    role = Role(
        code=nouveau.code,
        name=nouveau.name,
        description=nouveau.description,
        is_system=False,
        # Lot 3 : né verrouillé — un rôle personnalisé n'est de toute façon jamais dans le
        # seed, mais on pose le flag dès la naissance pour rester correct et cohérent avec
        # modifier_metadonnees/remplacer_permissions (mêmes règles partout).
        gere_manuellement=True,
    )
    db.add(role)
    db.flush()

    ecrire_audit(
        db,
        action=ActionRole.CREATED,
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=role.id,
        new_values=_etat_auditable(role),
    )
    db.commit()
    return role


# --- métadonnées ---------------------------------------------------------------------------

CHAMPS_MODIFIABLES = ("name", "description")


def modifier_metadonnees(
    db: Session,
    courant: UtilisateurCourant,
    code: str,
    modifications: dict[str, object],
    contexte: ContexteRequete,
) -> Role:
    """Modification partielle du nom/de la description d'un rôle personnalisé."""
    role = _role_par_code(db, code)
    if role.is_system:
        raise RoleSystemeNonModifiableError(code)

    inconnus = set(modifications) - set(CHAMPS_MODIFIABLES)
    if inconnus:
        raise ValueError(f"Champs non modifiables : {sorted(inconnus)}")

    avant = _etat_auditable(role)
    for champ, valeur in modifications.items():
        setattr(role, champ, valeur)
    # Lot 3 : cet appel verrouille le rôle — seed-security ne le touchera plus jamais.
    role.gere_manuellement = True
    db.flush()
    apres = _etat_auditable(role)

    # N'auditer QUE ce qui a bougé (même règle que la fiche utilisateur).
    changes = {champ for champ in avant if avant[champ] != apres[champ]}
    if changes:
        ecrire_audit(
            db,
            action=ActionRole.UPDATED,
            contexte=contexte,
            acteur_id=courant.user_id,
            resource_type=RESSOURCE,
            resource_id=role.id,
            old_values={champ: avant[champ] for champ in changes},
            new_values={champ: apres[champ] for champ in changes},
        )
    db.commit()
    return role


# --- suppression ---------------------------------------------------------------------------


def supprimer(
    db: Session, courant: UtilisateurCourant, code: str, contexte: ContexteRequete
) -> None:
    """Supprime un rôle PERSONNALISÉ.

    Refuse AVANT le trigger DB (migration 0004) pour un rôle système : ce chemin ne devrait
    jamais l'atteindre, le trigger reste un filet de sécurité, pas le contrôle nominal.
    """
    role = _role_par_code(db, code)
    if role.is_system:
        raise RoleSystemeNonModifiableError(code)

    _verifier_au_moins_un_gardien(db, code, frozenset())

    avant = _etat_auditable(role)
    role_id = role.id
    db.delete(role)
    db.flush()

    ecrire_audit(
        db,
        action=ActionRole.DELETED,
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=role_id,
        old_values=avant,
    )
    db.commit()


# --- permissions ---------------------------------------------------------------------------


def remplacer_permissions(
    db: Session,
    courant: UtilisateurCourant,
    code: str,
    permission_codes: frozenset[str],
    motif: str,
    contexte: ContexteRequete,
) -> Role:
    """Remplace ATOMIQUEMENT le jeu de permissions d'un rôle personnalisé."""
    role = _role_par_code(db, code)
    if role.is_system:
        raise RoleSystemeNonModifiableError(code)

    permissions = list(
        db.execute(select(Permission).where(Permission.code.in_(permission_codes))).scalars()
    )
    trouvees = {p.code for p in permissions}
    manquantes = permission_codes - trouvees
    if manquantes:
        raise PermissionInconnueError(sorted(manquantes)[0])

    nouveau_set = frozenset(permission_codes)
    _verifier_au_moins_un_gardien(db, code, nouveau_set)

    avant = sorted(_permissions_du_role(db, role.id))
    db.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
    for permission in permissions:
        db.add(RolePermission(role_id=role.id, permission_id=permission.id))
    # Lot 3 : cet appel verrouille le rôle — seed-security ne le touchera plus jamais.
    role.gere_manuellement = True
    db.flush()

    ecrire_audit(
        db,
        action=ActionRole.PERMISSIONS_REPLACED,
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=role.id,
        old_values={"permissions": avant},
        new_values={"permissions": sorted(nouveau_set), "motif": motif},
    )
    db.commit()
    db.refresh(role)
    return role
