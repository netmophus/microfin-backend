"""Écritures sur les rôles — création (personnalisés uniquement), métadonnées, permissions,
suppression (personnalisés uniquement), réinitialisation (système uniquement).

Séparé de utilisateurs_ecriture.py (qui gère l'ATTRIBUTION d'un rôle à un utilisateur, bloc
4d) : ce fichier gère la DÉFINITION du rôle lui-même — surface de risque différente.

RÔLES SYSTÈME (lot 4) : modifier_metadonnees et remplacer_permissions s'appliquent
DÉSORMAIS aussi à eux — c'est le verrou gere_manuellement (lot 3) qui rend ça sûr : dès
qu'un rôle système est édité, il sort de la convergence du seed. Seule supprimer() reste
interdite sur un rôle système (on n'en supprime jamais un — le trigger DB, migration 0004,
le bloquerait de toute façon). Le POST crée toujours is_system=False, jamais lu d'un
paramètre client : créer un rôle SYSTÈME n'a pas de sens, il n'y a que 11 rôles système,
tous posés par le seed.

RÉINITIALISATION (lot 4) : reinitialiser() est l'inverse — repose gere_manuellement=FALSE
sur un rôle système verrouillé puis rejoue le seed pour LUI SEUL (en réalité pour les 11,
mais les autres, non verrouillés, reconvergent déjà et ce passage est un no-op pour eux).
Aucune logique de convergence dupliquée : c'est littéralement `executer_seed`.

GARDE-FOU ANTI-BLOCAGE (décidé) : il doit rester À TOUT MOMENT au moins un rôle actif qui
détient roles.permissions.manage — pas seulement « pas sur son propre rôle ». _verifier_au_
moins_un_gardien couvre TROIS chemins qui pourraient faire disparaître le dernier porteur :
PUT .../permissions, DELETE, et désormais reinitialiser (si une matrice future retirait la
permission au rôle qu'on réinitialise). Le lot 4 le rend concret : ADMIN_TECHNIQUE est
aujourd'hui le seul porteur, et est maintenant éditable.

MOTIF (PUT .../permissions) : jamais stocké en colonne dédiée, seulement dans new_values de
l'audit — qui/quoi/quand/motif suffit, pas de migration pour ça. Même règle pour
reinitialiser, à la différence que le motif y est FACULTATIF (l'action se justifie d'elle-
même ; le motif, s'il est fourni, est tracé en plus).
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.cli.seed_security import MATRICE, executer_seed
from app.modules.audit.service import ContexteRequete, ecrire_audit
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.security.models import Permission, Role, RolePermission

RESSOURCE = "role"
PERMISSION_GARDIENNE = "roles.permissions.manage"


class ActionRole:
    """Actions d'audit du périmètre rôles (lot 2/4). Format module.action, comme les autres."""

    CREATED = "role.created"
    UPDATED = "role.updated"
    DELETED = "role.deleted"
    PERMISSIONS_REPLACED = "role.permissions_replaced"
    RESET_TO_DEFAULT = "role.reset_to_default"


# --- erreurs ---------------------------------------------------------------------------


class RoleIntrouvableError(Exception):
    """Le code de rôle demandé ne correspond à aucun rôle."""


class RoleSystemeNonSupprimableError(Exception):
    """Un rôle système ne se supprime jamais — seule action encore interdite au lot 4."""

    def __init__(self, code: str) -> None:
        super().__init__(f"Le rôle système « {code} » ne peut pas être supprimé.")
        self.code = code


class RoleNonSystemeError(Exception):
    """reinitialiser() ne s'applique qu'aux rôles système : un rôle personnalisé n'a pas
    de « réglage d'usine » vers lequel revenir."""

    def __init__(self, code: str) -> None:
        super().__init__(f"Le rôle « {code} » n'est pas un rôle système.")
        self.code = code


class RoleNonVerrouilleError(Exception):
    """reinitialiser() sur un rôle déjà géré par le seed : rien à réinitialiser."""

    def __init__(self, code: str) -> None:
        super().__init__(f"Le rôle « {code} » n'a pas été modifié : rien à réinitialiser.")
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
    """Modification partielle du nom/de la description — rôle personnalisé OU système
    (lot 4). Verrouille le rôle dans les deux cas (voir gere_manuellement plus bas)."""
    role = _role_par_code(db, code)

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
    """Supprime un rôle PERSONNALISÉ. Seule action encore interdite sur un rôle système
    au lot 4 : on n'en supprime jamais (les 11 rôles système sont permanents).

    Refuse AVANT le trigger DB (migration 0004) pour un rôle système : ce chemin ne devrait
    jamais l'atteindre, le trigger reste un filet de sécurité, pas le contrôle nominal.
    """
    role = _role_par_code(db, code)
    if role.is_system:
        raise RoleSystemeNonSupprimableError(code)

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
    """Remplace ATOMIQUEMENT le jeu de permissions — rôle personnalisé OU système (lot 4).
    Verrouille le rôle dans les deux cas. Le garde-fou anti-blocage (ci-dessous) couvre
    déjà le cas où ce changement retirerait roles.permissions.manage à ADMIN_TECHNIQUE."""
    role = _role_par_code(db, code)

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


# --- réinitialisation (lot 4) ---------------------------------------------------------------


def reinitialiser(
    db: Session,
    courant: UtilisateurCourant,
    code: str,
    contexte: ContexteRequete,
    motif: str | None = None,
) -> Role:
    """Réinitialise un rôle SYSTÈME verrouillé à son « réglage d'usine » : repose
    gere_manuellement=FALSE puis rejoue le seed, qui reconverge alors ce rôle (métadonnées
    ET permissions) exactement comme à l'installation. Motif FACULTATIF : tracé dans
    l'audit s'il est fourni, jamais exigé (l'action se justifie d'elle-même)."""
    role = _role_par_code(db, code)
    if not role.is_system:
        raise RoleNonSystemeError(code)
    if not role.gere_manuellement:
        raise RoleNonVerrouilleError(code)

    # Garde-fou anti-blocage AVANT d'agir : le jeu que ce rôle aura APRÈS reconvergence est
    # celui déclaré dans la matrice courante — si elle ne lui donne plus roles.permissions.
    # manage et qu'aucun autre rôle actif ne la détient, on refuse.
    _verifier_au_moins_un_gardien(db, code, MATRICE.get(code, frozenset()))

    avant = _etat_auditable(role)
    avant_permissions = sorted(_permissions_du_role(db, role.id))

    role.gere_manuellement = False
    db.flush()
    executer_seed(db)
    db.refresh(role)

    apres = _etat_auditable(role)
    apres_permissions = sorted(_permissions_du_role(db, role.id))

    new_values: dict[str, object] = {**apres, "permissions": apres_permissions}
    if motif:
        new_values["motif"] = motif

    ecrire_audit(
        db,
        action=ActionRole.RESET_TO_DEFAULT,
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=role.id,
        old_values={**avant, "permissions": avant_permissions},
        new_values=new_values,
    )
    db.commit()
    db.refresh(role)
    return role
