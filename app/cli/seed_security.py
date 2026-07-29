"""Seed des rôles et de la matrice RBAC : 11 rôles système et 46 permissions.

Historiquement le seul socle Sécurité (18 permissions) ; le module Tiers y ajoute ses
permissions métier (4 en T1c : read/read.basic/create/update ; 3 en T1e : suspend/deactivate/
validate). Ce fichier est de fait la matrice RBAC INTER-MODULES : la convergence
(_REVOQUER_HORS_MATRICE) étant globale aux rôles système, la matrice doit rester unique et
complète. Chaque futur module y contribuera ses permissions et leurs affectations.

18 = le §5 en fige 17, plus « perimetre.reseau » ajoutée au bloc 4a — le
marqueur de PORTÉE RÉSEAU. Un rôle qui la détient voit toutes les agences ; sinon il est
cloisonné à son agence courante (claim agency_id du JWT, C6). Modéliser la portée comme
une permission la rend attribuable à un rôle personnalisé sans toucher au code, et
auditable en base (« qui voit tout ? » = une ligne de matrice). Divergence au §5 à porter
au document (17 → 18).

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

# --- §5 — les 17 permissions du périmètre Sécurité + la portée réseau (4a) ------------
PERMISSIONS: tuple[Permission, ...] = (
    # Marqueur de portée (4a). Transverse à tous les modules : la détenir, c'est voir
    # tout le réseau ; ne pas la détenir, c'est être cloisonné à son agence courante.
    Permission(
        "perimetre.reseau",
        "perimetre",
        "Portée réseau : accès à toutes les agences, sinon cloisonné à l'agence courante",
    ),
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
    # --- Module Tiers (T1c) : premier module métier à peupler la matrice.
    Permission("tiers.read", "tiers", "Consulter une fiche tiers, la liste et sa frise"),
    Permission(
        "tiers.read.basic",
        "tiers",
        "Vue limitée d'un tiers (identification, sans données KYC/socio-éco)",
    ),
    Permission("tiers.create", "tiers", "Créer une fiche tiers (physique, morale, groupement)"),
    Permission("tiers.update", "tiers", "Modifier une fiche tiers"),
    # --- Cycle de vie (T1e). Séparation VOLONTAIRE : suspendre est réversible et quotidien,
    # désactiver fait SORTIR la fiche de l'annuaire (un membre avec épargne/crédit en cours qui
    # disparaît = incident relevé par un contrôleur BCEAO) -> réservé au responsable.
    Permission("tiers.suspend", "tiers", "Suspendre/réactiver, enregistrer décès ou dissolution"),
    Permission("tiers.deactivate", "tiers", "Désactiver une fiche (soft delete)"),
    Permission("tiers.validate", "tiers", "Valider l'activation d'une fiche (KYC)"),
    # Valider un profil à RISQUE ÉLEVÉ : réservé au LBC/FT (le blanchiment est son métier).
    # Le responsable d'agence valide faible/moyen ; l'élevé remonte au LBC/FT (T3c).
    Permission("tiers.validate.high_risk", "tiers", "Valider l'activation d'un profil à risque"),
    # Vérifier une pièce (T2c) = acte de CONTRÔLE distinct de la saisie : le chargé de clientèle
    # saisit la pièce, mais l'ATTESTER (elle a été vue et validée) est réservé au responsable
    # d'agence et au LBC/FT. Une pièce attestée puis modifiée (supprimée + re-saisie) perd son
    # tampon — la vérification suit la pièce, pas le numéro.
    Permission("tiers.identity.verify", "tiers", "Vérifier une pièce d'identité (contrôle)"),
    # Voir les fiches DÉSACTIVÉES (sorties de l'annuaire par soft delete). Acte de SUPERVISION,
    # pas de saisie : réservé au contrôle (responsable d'agence, auditeur, LBC/FT, direction).
    # Le chargé de clientèle et le caissier ne les voient pas — une fiche désactivée n'existe
    # plus pour eux (les doublons de pièce sont attrapés à la saisie par le contrôle T2c, ils
    # n'ont donc pas besoin de parcourir les désactivés).
    Permission("tiers.read.deleted", "tiers", "Consulter les fiches désactivées (supervision)"),
    # --- Module Comptabilité (C0) : le plan de comptes est une DONNÉE gérée par le comptable.
    # read = consulter le plan ; manage = importer, créer, désactiver, changer libellé/sens
    # (dans les limites des garde-fous : jamais un compte système, jamais un compte mouvementé).
    Permission("compta.plan.read", "compta", "Consulter le plan de comptes"),
    Permission("compta.plan.manage", "compta", "Gérer le plan de comptes (import, création, MAJ)"),
    # --- Écritures + cadre (C1/C2). post = créer un brouillon et le valider ; reverse =
    # contre-passer une pièce validée (la seule correction possible) ; exercice.manage =
    # ouvrir/clôturer un exercice (clôture reportée).
    Permission("compta.ecriture.read", "compta", "Consulter les écritures et journaux"),
    Permission("compta.ecriture.post", "compta", "Saisir un brouillon et valider une écriture"),
    Permission("compta.ecriture.reverse", "compta", "Contre-passer une écriture validée"),
    Permission("compta.exercice.manage", "compta", "Ouvrir/clôturer un exercice comptable"),
    # --- Module Épargne (E0/E2). open = ouvrir un compte (membre actif) ; close = clôturer ;
    # product.manage = gérer le référentiel des produits (config métier). Les opérations
    # (dépôt/retrait) auront leur permission au bloc E3.
    Permission("epargne.product.read", "epargne", "Consulter les produits d'épargne"),
    Permission("epargne.product.manage", "epargne", "Gérer les produits d'épargne"),
    Permission("epargne.account.read", "epargne", "Consulter les comptes d'épargne"),
    Permission("epargne.account.open", "epargne", "Ouvrir un compte d'épargne (membre actif)"),
    Permission("epargne.account.close", "epargne", "Clôturer un compte d'épargne"),
    # Opérations de guichet (E3) : dépôt et retrait. Réservées au caissier (et au responsable).
    Permission("epargne.operation.deposit", "epargne", "Enregistrer un dépôt au guichet"),
    Permission("epargne.operation.withdraw", "epargne", "Enregistrer un retrait au guichet"),
    # Intérêts (F4). executer = lancer le versement des intérêts d'une période. Acte
    # d'INSTITUTION (le taux, le moment, la périodicité engagent toute l'IMF), pas une opération
    # d'agence : réservé à la direction, jamais au responsable d'agence cloisonné — sinon chaque
    # agence verserait quand elle veut, avec des incohérences comptables et un risque réglementaire.
    Permission("epargne.interet.executer", "epargne", "Verser les intérêts d'une période"),
    # rapprochement.read = vue de contrôle Σ soldes d'épargne (auxiliaire) vs solde comptable
    # (général) du compte collectif. Acte de CONTRÔLE, réservé à l'audit/la direction/le comptable.
    Permission(
        "epargne.rapprochement.read", "epargne", "Voir le rapprochement épargne/comptabilité"
    ),
    # --- Parts sociales (PS1). subscribe = souscrire/libérer des parts (guichet : encaissement).
    # read = consulter les parts d'un tier. Le remboursement (départ) aura sa permission en PS2.
    Permission("tiers.shares.subscribe", "tiers", "Souscrire / libérer des parts sociales"),
    Permission("tiers.shares.read", "tiers", "Consulter les parts sociales d'un tiers"),
    # Remboursement / annulation (PS2, sortie du sociétariat) : rendre le capital est un acte de
    # GESTION sensible -> réservé au responsable (comme la fermeture d'un compte d'épargne).
    Permission("tiers.shares.refund", "tiers", "Rembourser / annuler des parts sociales"),
)

# --- Matrice rôles -> permissions ----------------------------------------------------
# Moindre privilège : les rôles opérationnels n'ont AUCUNE permission du périmètre Sécurité.
# Leurs droits MÉTIER arrivent avec leurs modules. Le module Tiers (T1c) est le premier à en
# accorder : le chargé de clientèle enrôle (create/read/update), le caissier identifie au
# guichet (read.basic — SANS les données KYC), le chargé de prêt consulte la fiche complète
# (read) pour instruire un crédit. Comptable et comité crédit restent vides : leur besoin
# tiers naît dans la Compta et le Crédit, non construits — on accorde quand un module consomme.
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
#
# PORTÉE RÉSEAU (perimetre.reseau) : accordée aux rôles dont la fonction couvre TOUT le
# réseau — Direction (pilotage), Auditeur (contrôle réseau), les deux Admins (gestion
# transverse), et Responsable LBC/FT (le blanchiment se surveille sur tout le réseau, pas
# une agence). PAS au Responsable d'agence : il est cloisonné à SON agence, c'est tout
# l'intérêt du cloisonnement.
MATRICE: dict[str, frozenset[str]] = {
    # Le caissier tient le guichet : dépôts et retraits (E3), et voit les comptes/produits.
    "CAISSIER": frozenset(
        {
            "tiers.read.basic",
            "epargne.account.read",
            "epargne.product.read",
            "epargne.operation.deposit",
            "epargne.operation.withdraw",
            "tiers.shares.subscribe",
            "tiers.shares.read",
        }
    ),
    # Le chargé de clientèle enrôle ET ouvre les comptes d'épargne des membres.
    "CHARGE_CLIENTELE": frozenset(
        {
            "tiers.create",
            "tiers.read",
            "tiers.read.basic",
            "tiers.update",
            "tiers.suspend",
            "epargne.account.open",
            "epargne.account.read",
            "epargne.product.read",
            "tiers.shares.subscribe",
            "tiers.shares.read",
        }
    ),
    "CHARGE_PRET": frozenset({"tiers.read", "tiers.read.basic"}),
    "MEMBRE_COMITE_CREDIT": frozenset(),
    # Le comptable tient le plan de comptes ET la comptabilité : plan, écritures (saisie +
    # validation + contre-passation) et exercices. Les états/balance/clôture viendront plus tard.
    "COMPTABLE": frozenset(
        {
            "compta.plan.read",
            "compta.plan.manage",
            "compta.ecriture.read",
            "compta.ecriture.post",
            "compta.ecriture.reverse",
            "compta.exercice.manage",
            "epargne.account.read",
            "epargne.product.read",
            "epargne.rapprochement.read",
            "tiers.shares.read",
        }
    ),
    # Déverrouiller oui (ne donne aucun accès), réinitialiser un mot de passe non :
    # cela permettrait d'entrer dans le compte d'un caissier et d'agir sous son nom.
    # PAS de perimetre.reseau : cloisonné à son agence. Il supervise l'enrôlement, donc
    # create/read/update/suspend comme le chargé, PLUS deactivate (soft delete, réservé) et
    # validate (validation KYC de l'activation).
    "RESPONSABLE_AGENCE": frozenset(
        {
            "users.read",
            "users.unlock",
            "sessions.read",
            "sessions.revoke",
            "tiers.create",
            "tiers.read",
            "tiers.read.basic",
            "tiers.update",
            "tiers.suspend",
            "tiers.deactivate",
            "tiers.validate",
            "tiers.identity.verify",
            "tiers.read.deleted",
            "epargne.account.open",
            "epargne.account.close",
            "epargne.account.read",
            "epargne.product.read",
            "epargne.operation.deposit",
            "epargne.operation.withdraw",
            "tiers.shares.subscribe",
            "tiers.shares.read",
            "tiers.shares.refund",
        }
    ),
    # Lecture seule intégrale : voir qui existe, qui détient quoi, lire le journal et les
    # fiches tiers (contrôle sur tout le réseau).
    "AUDITEUR_INTERNE": frozenset(
        {
            "users.read",
            "roles.read",
            "sessions.read",
            "audit.read",
            "audit.export",
            "tiers.read",
            "tiers.read.basic",
            "tiers.read.deleted",
            "epargne.account.read",
            "epargne.rapprochement.read",
            "tiers.shares.read",
            "perimetre.reseau",
        }
    ),
    # LBC/FT surveille les fiches (KYC, PPE) sur tout le réseau -> tiers.read. Et valide
    # l'activation des profils à risque (maker-checker) -> tiers.validate.
    "RESPONSABLE_LBC_FT": frozenset(
        {
            "users.read",
            "audit.read",
            "tiers.read",
            "tiers.read.basic",
            "tiers.validate",
            "tiers.validate.high_risk",
            "tiers.identity.verify",
            "tiers.read.deleted",
            "perimetre.reseau",
        }
    ),
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
            "tiers.read",
            "tiers.read.basic",
            "tiers.read.deleted",
            "epargne.interet.executer",
            "epargne.rapprochement.read",
            "tiers.shares.read",
            "perimetre.reseau",
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
            "epargne.product.read",
            "epargne.product.manage",
            "perimetre.reseau",
        }
    ),
    # Administration système, pas administration des personnes : aucun droit sur users.*.
    "ADMIN_TECHNIQUE": frozenset(
        {"sessions.read", "sessions.revoke", "audit.read", "perimetre.reseau"}
    ),
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
