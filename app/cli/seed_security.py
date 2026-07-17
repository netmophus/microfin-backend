"""Seed du socle Sécurité : 11 rôles système et 17 permissions (§4 et §5 du document).

Données versionnées, rejouées à chaque installation d'une IMF et à chaque montée de
version — pas un script jetable. La commande est idempotente : les rôles et permissions
sont upsertés par `code`, et les habilitations des rôles système convergent vers la
matrice ci-dessous (les accords qui n'y figurent plus sont révoqués).

La matrice rôles↔permissions n'est PAS spécifiée par le document. Celle-ci est une
proposition fondée sur le moindre privilège et la séparation des pouvoirs (§4), validée
avec l'utilisateur avant application.

Aucun modèle ORM ici : le socle n'en déclare encore aucun (cf.
test_aucune_table_metier_declaree). Le seed passe donc par du SQL explicite.
"""

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class RoleSysteme:
    """Rôle livré avec le produit : is_system, donc ni modifiable ni supprimable."""

    code: str
    name: str
    description: str
    requires_2fa: bool
    password_expiry_days: int


@dataclass(frozen=True)
class Permission:
    """Droit atomique, au format module.action."""

    code: str
    module: str
    description: str
    # Le scope reste NULL : le document ne l'affecte à aucune permission, et le porter
    # ici serait une sémantique que rien n'applique. Le cloisonnement par agence passe
    # par le claim agency_id du JWT (C6), pas par cette colonne.
    scope: str | None = None


@dataclass(frozen=True)
class RapportSeed:
    """Ce que le seed a effectivement fait, pour l'afficher et pour les tests."""

    roles: int
    permissions: int
    accords: int
    revocations: int


# --- §4 — les 11 rôles système -------------------------------------------------------
# Profils sensibles (2FA imposée, mot de passe à 60 j au lieu de 90) : ceux qui
# administrent le système ou peuvent exporter le journal d'audit.
ROLES: tuple[RoleSysteme, ...] = (
    RoleSysteme(
        "CAISSIER", "Caissier", "Opérations de guichet, encaissements/décaissements", False, 90
    ),
    RoleSysteme(
        "CHARGE_CLIENTELE",
        "Chargé de clientèle",
        "Gestion de la relation client, ouverture de comptes",
        False,
        90,
    ),
    RoleSysteme(
        "CHARGE_PRET",
        "Chargé de prêt",
        "Instruction et suivi des dossiers de crédit",
        False,
        90,
    ),
    RoleSysteme(
        "MEMBRE_COMITE_CREDIT",
        "Membre comité crédit",
        "Vote sur l'octroi des crédits",
        False,
        90,
    ),
    RoleSysteme("COMPTABLE", "Comptable", "Tenue de la comptabilité, écritures, états", False, 90),
    RoleSysteme(
        "RESPONSABLE_AGENCE",
        "Responsable d'agence",
        "Supervision d'une agence, validations locales",
        False,
        90,
    ),
    RoleSysteme(
        "AUDITEUR_INTERNE",
        "Auditeur interne",
        "Consultation et contrôle, lecture de l'audit",
        True,
        60,
    ),
    RoleSysteme(
        "RESPONSABLE_LBC_FT",
        "Responsable LBC/FT",
        "Surveillance anti-blanchiment, déclarations CENTIF",
        False,
        90,
    ),
    RoleSysteme(
        "DIRECTION_GENERALE",
        "Direction générale",
        "Pilotage, validations de haut niveau (maker-checker)",
        True,
        60,
    ),
    RoleSysteme(
        "ADMIN_FONCTIONNEL",
        "Admin fonctionnel",
        "Configuration métier, gestion des utilisateurs et rôles",
        True,
        60,
    ),
    RoleSysteme(
        "ADMIN_TECHNIQUE",
        "Admin technique",
        "Administration système, paramètres techniques",
        True,
        60,
    ),
)

# --- §5 — les 17 permissions du périmètre Sécurité -----------------------------------
PERMISSIONS: tuple[Permission, ...] = (
    Permission("users.read", "users", "Consulter les utilisateurs (sans champs sensibles)"),
    Permission("users.create", "users", "Créer un utilisateur"),
    Permission("users.update", "users", "Modifier une fiche (activation/désactivation incluse)"),
    Permission("users.delete", "users", "Supprimer un utilisateur (soft delete)"),
    Permission("users.unlock", "users", "Déverrouiller un compte avant la fin du délai auto"),
    Permission("users.reset_password", "users", "Réinitialiser le mot de passe d'un tiers"),
    Permission("users.reset_2fa", "users", "Réinitialiser la 2FA (perte de téléphone)"),
    Permission("users.manage_agencies", "users", "Gérer les habilitations d'agences"),
    Permission("roles.read", "roles", "Consulter les rôles"),
    Permission("roles.create", "roles", "Créer un rôle personnalisé (jamais un rôle système)"),
    Permission("roles.update", "roles", "Modifier un rôle (jamais un rôle système)"),
    Permission("roles.delete", "roles", "Supprimer un rôle (jamais un rôle système)"),
    Permission("roles.assign", "roles", "Affecter ou retirer un rôle à un utilisateur"),
    Permission("sessions.read", "sessions", "Voir les sessions actives"),
    Permission("sessions.revoke", "sessions", "Fermer une ou toutes les sessions"),
    Permission("audit.read", "audit", "Consulter le journal d'audit (lecture seule)"),
    Permission("audit.export", "audit", "Exporter le journal signé"),
)

# --- Matrice rôles -> permissions ----------------------------------------------------
# Moindre privilège : les 5 rôles purement opérationnels n'ont AUCUNE permission du
# périmètre Sécurité. Les leurs viendront avec leurs modules (cash, tiers, credit,
# accounting).
#
# Séparation des pouvoirs sur les deux leviers dangereux :
#   - ADMIN_FONCTIONNEL affecte les rôles (roles.assign) mais ne peut pas en définir le
#     contenu : il ne peut donc pas forger un rôle sur mesure puis se l'attribuer.
#   - DIRECTION_GENERALE définit le référentiel des rôles (create/update/delete) mais ne
#     les distribue pas.
# Aucun des deux ne détient les deux moitiés.
#
# audit.export (exfiltration du journal complet) reste chez AUDITEUR_INTERNE et
# DIRECTION_GENERALE uniquement.
MATRICE: dict[str, frozenset[str]] = {
    "CAISSIER": frozenset(),
    "CHARGE_CLIENTELE": frozenset(),
    "CHARGE_PRET": frozenset(),
    "MEMBRE_COMITE_CREDIT": frozenset(),
    "COMPTABLE": frozenset(),
    # Déverrouiller oui (ne donne aucun accès), réinitialiser un mot de passe non :
    # cela permettrait d'entrer dans le compte d'un caissier et d'agir sous son nom.
    "RESPONSABLE_AGENCE": frozenset(
        {"users.read", "users.unlock", "sessions.read", "sessions.revoke"}
    ),
    # Lecture seule intégrale : voir qui existe, qui détient quoi, et lire le journal.
    "AUDITEUR_INTERNE": frozenset(
        {"users.read", "roles.read", "sessions.read", "audit.read", "audit.export"}
    ),
    "RESPONSABLE_LBC_FT": frozenset({"users.read", "audit.read"}),
    "DIRECTION_GENERALE": frozenset(
        {
            "users.read",
            "roles.read",
            "roles.create",
            "roles.update",
            "roles.delete",
            "sessions.read",
            "audit.read",
            "audit.export",
        }
    ),
    "ADMIN_FONCTIONNEL": frozenset(
        {
            "users.read",
            "users.create",
            "users.update",
            "users.delete",
            "users.unlock",
            "users.reset_password",
            "users.reset_2fa",
            "users.manage_agencies",
            "roles.read",
            "roles.assign",
            "sessions.read",
            "sessions.revoke",
        }
    ),
    # Administration système, pas administration des personnes : aucun droit sur users.*.
    "ADMIN_TECHNIQUE": frozenset({"sessions.read", "sessions.revoke", "audit.read"}),
}


_UPSERT_ROLE = text(
    """
    INSERT INTO security.roles
        (code, name, description, is_system, requires_2fa, password_expiry_days)
    VALUES
        (:code, :name, :description, TRUE, :requires_2fa, :password_expiry_days)
    ON CONFLICT (code) DO UPDATE SET
        name                 = EXCLUDED.name,
        description          = EXCLUDED.description,
        is_system            = TRUE,
        requires_2fa         = EXCLUDED.requires_2fa,
        password_expiry_days = EXCLUDED.password_expiry_days,
        updated_at           = NOW()
    """
)

_UPSERT_PERMISSION = text(
    """
    INSERT INTO security.permissions (code, scope, module, description)
    VALUES (:code, :scope, :module, :description)
    ON CONFLICT (code) DO UPDATE SET
        scope       = EXCLUDED.scope,
        module      = EXCLUDED.module,
        description = EXCLUDED.description
    """
)

_ACCORDER = text(
    """
    INSERT INTO security.role_permissions (role_id, permission_id)
    SELECT r.id, p.id
      FROM security.roles r, security.permissions p
     WHERE r.code = :role_code AND p.code = :permission_code
    ON CONFLICT (role_id, permission_id) DO NOTHING
    """
)

# Convergence : un droit retiré de la matrice doit disparaître des bases déjà installées,
# sinon une permission révoquée pour raison de sécurité survivrait à la montée de version.
# Restreint aux rôles système : les rôles personnalisés d'une IMF ne nous appartiennent pas.
_REVOQUER_HORS_MATRICE = text(
    """
    DELETE FROM security.role_permissions rp
     USING security.roles r, security.permissions p
     WHERE rp.role_id = r.id
       AND rp.permission_id = p.id
       AND r.is_system
       AND (r.code || '|' || p.code) <> ALL(:accordes)
    RETURNING rp.role_id
    """
).bindparams(sa.bindparam("accordes", type_=postgresql.ARRAY(sa.Text())))


def executer_seed(db: Session) -> RapportSeed:
    """Applique la matrice sur la session fournie. Ne committe pas : l'appelant décide."""
    for role in ROLES:
        db.execute(
            _UPSERT_ROLE,
            {
                "code": role.code,
                "name": role.name,
                "description": role.description,
                "requires_2fa": role.requires_2fa,
                "password_expiry_days": role.password_expiry_days,
            },
        )

    for permission in PERMISSIONS:
        db.execute(
            _UPSERT_PERMISSION,
            {
                "code": permission.code,
                "scope": permission.scope,
                "module": permission.module,
                "description": permission.description,
            },
        )

    accords = 0
    for role_code, permission_codes in MATRICE.items():
        for permission_code in sorted(permission_codes):
            db.execute(_ACCORDER, {"role_code": role_code, "permission_code": permission_code})
            accords += 1

    attendus = [
        f"{role_code}|{permission_code}"
        for role_code, permission_codes in MATRICE.items()
        for permission_code in permission_codes
    ]
    # RETURNING plutôt que rowcount : ce dernier n'est pas typé sur Result.
    revocations = len(db.execute(_REVOQUER_HORS_MATRICE, {"accordes": attendus}).fetchall())

    return RapportSeed(
        roles=len(ROLES),
        permissions=len(PERMISSIONS),
        accords=accords,
        revocations=revocations,
    )
