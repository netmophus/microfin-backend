"""Comptes de DÉVELOPPEMENT — un compte par rôle métier réel, durable et documenté.

POURQUOI. Éprouver le logiciel « pour de vrai » (le smoke test manuel auquel on tient) exige un
compte par rôle : un responsable d'agence pour désactiver/réactiver et valider le KYC, un LBC/FT
pour la vigilance, un chargé de clientèle pour enrôler, un caissier pour la vue guichet, un
auditeur pour la lecture réseau. Les recréer à la main à chaque base neuve est une perte de temps
et une source d'erreurs. Cette commande les pose de façon IDEMPOTENTE, avec un mot de passe FIXE
et CONNU (documenté dans docs/comptes-dev.md), sans must_change_password : on se connecte direct.

CE N'EST PAS LE SEED DE PRODUCTION. Il est distinct de `seed-security` (rôles/permissions, prod) et
de `creer-admin` (amorçage réel, mot de passe généré périssable). Il REFUSE de s'exécuter si
ENV=production (sauf --force explicite) : des comptes à mot de passe public n'ont rien à faire en
prod. Le mot de passe n'est pas un secret — c'est une commodité de dev, assumée comme telle.

Chantier coffre-fort/caisses, sous-chantier 1 Bloc 3 : propose AUSSI un jeu de départ des
niveaux coffre/principale pour le siège de dev (`seed_comptabilite.seed_niveaux_caisse_dev`) —
volontairement câblé ICI, pas dans `seed-comptabilite` (qui tourne sur TOUTE installation,
dev ou prod) : ce défaut n'a rien à faire en production, où une IMF paramètre ses comptes à
l'écran (Bloc 2)."""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.cli.seed_comptabilite import seed_niveaux_caisse_dev
from app.modules.parameters.models import Agency
from app.modules.security.models import Role, User, UserRole
from app.modules.security.password import hasher_mot_de_passe

# Mot de passe COMMUN à tous les comptes de dev. Public par nature (documenté). Satisfait la
# politique (majuscule, minuscule, chiffre, spécial, >= 12) au cas où un écran la revérifierait.
MOT_DE_PASSE_DEV = "MotDePasse!Dev1"


@dataclass(frozen=True)
class CompteDev:
    username: str
    role: str
    matricule: str
    last_name: str
    first_name: str


# Un compte par rôle qui porte des permissions métier réelles. L'admin fonctionnel vient de
# `creer-admin` (amorçage), il n'est pas répété ici.
COMPTES: tuple[CompteDev, ...] = (
    CompteDev("resp", "RESPONSABLE_AGENCE", "DEV-RESP", "Responsable", "Agence"),
    CompteDev("lbcft", "RESPONSABLE_LBC_FT", "DEV-LBC", "Responsable", "LBC-FT"),
    CompteDev("auditeur", "AUDITEUR_INTERNE", "DEV-AUD", "Auditeur", "Interne"),
    CompteDev("charge", "CHARGE_CLIENTELE", "DEV-CHG", "Charge", "Clientele"),
    CompteDev("caissier", "CAISSIER", "DEV-CAI", "Caissier", "Guichet"),
    CompteDev("pret", "CHARGE_PRET", "DEV-PRT", "Charge", "Pret"),
    CompteDev("comite", "MEMBRE_COMITE_CREDIT", "DEV-CMT", "Membre", "ComiteCredit"),
    CompteDev("comptable", "COMPTABLE", "DEV-CPT", "Comptable", "SFD"),
    # Distinct du compte réel "anne" (GG001) — celui-ci est un compte de DEV, mot de passe
    # public connu, jamais à confondre avec un compte de production.
    CompteDev("direction", "DIRECTION_GENERALE", "DEV-DIR", "Direction", "Generale"),
    # Distinct de "sysadmin" (ADMIN_FONCTIONNEL) : ADMIN_TECHNIQUE définit les rôles
    # (roles.permissions.read/create/update/delete) sans pouvoir les attribuer — ce compte
    # isole ce rôle pour le tester séparément de sysadmin.
    CompteDev("technique", "ADMIN_TECHNIQUE", "DEV-TEC", "Admin", "Technique"),
)


@dataclass
class RapportSeedDev:
    crees: list[str]
    ignores: list[str]  # déjà présents (idempotence)
    # Chantier coffre-fort/caisses, sous-chantier 1 Bloc 3 : niveaux coffre/principale
    # nouvellement rattachés pour le siège de dev (0 si déjà paramétrés, jamais écrasés).
    niveaux_caisse_rattaches: int = 0


class RoleManquantError(Exception):
    """Un rôle système attendu n'existe pas — jouer d'abord seed-security."""


class AgenceManquanteError(Exception):
    """Aucune agence : jouer d'abord creer-admin (qui crée le siège)."""


def executer_seed_dev(db: Session) -> RapportSeedDev:
    """Crée (ou saute) les comptes de dev, tous rattachés au siège. Ne committe pas.

    Idempotente : un compte dont le username existe déjà est laissé tel quel (jamais réécrit,
    pour ne pas piétiner un mot de passe qu'on aurait volontairement changé).
    """
    agence = db.execute(select(Agency).order_by(Agency.created_at).limit(1)).scalar_one_or_none()
    if agence is None:
        raise AgenceManquanteError()

    roles = {r.code: r for r in db.execute(select(Role)).scalars()}
    manquants = [c.role for c in COMPTES if c.role not in roles]
    if manquants:
        raise RoleManquantError(", ".join(sorted(set(manquants))))

    hash_commun = hasher_mot_de_passe(MOT_DE_PASSE_DEV)
    crees: list[str] = []
    ignores: list[str] = []

    for compte in COMPTES:
        existe = db.execute(
            select(func.count()).select_from(User).where(User.username == compte.username)
        ).scalar_one()
        if existe:
            ignores.append(compte.username)
            continue

        user = User(
            matricule=compte.matricule,
            email=f"{compte.username}@dev.local",
            username=compte.username,
            password_hash=hash_commun,
            last_name=compte.last_name,
            first_name=compte.first_name,
            primary_agency_id=agence.id,  # cloisonnés (resp/chargé/caissier) : une agence leur faut
            must_change_password=False,  # comptes de dev : on se connecte sans détour
        )
        db.add(user)
        db.flush()
        db.add(UserRole(user_id=user.id, role_id=roles[compte.role].id))
        db.flush()
        crees.append(compte.username)

    # Bloc 3 : propose coffre/principale pour CE siège de dev seulement — jamais le réseau,
    # jamais écrasé si déjà paramétré (voir seed_comptabilite.py::seed_niveaux_caisse_dev).
    niveaux_rattaches = seed_niveaux_caisse_dev(db, agence.id)

    return RapportSeedDev(crees=crees, ignores=ignores, niveaux_caisse_rattaches=niveaux_rattaches)
