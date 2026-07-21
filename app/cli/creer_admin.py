"""Création du compte administrateur d'installation — brique fondatrice de `init-imf`.

POURQUOI CETTE COMMANDE EXISTE. `POST /users` exige d'être authentifié avec `users.create`.
Sur une base fraîche, il n'y a aucun compte : personne ne peut donc en créer un, et le
logiciel est littéralement impossible à démarrer. Une IMF qui installe le produit serait
bloquée à la première minute. C'est le seul chemin de création hors API, et il doit le
rester — d'où une commande d'ADMINISTRATION SERVEUR, qui exige un accès à la machine et à
la base, pas une route HTTP qu'on pourrait atteindre depuis le réseau.

CE QU'ELLE FAIT ET NE FAIT PAS

  - Elle crée UN compte ADMIN_FONCTIONNEL. Ce rôle assigne les rôles (`roles.assign`) mais
    ne les définit pas : conformément à la séparation des pouvoirs du §4, l'administrateur
    d'installation ne peut pas se forger un rôle sur mesure puis se l'octroyer.
  - Elle GÉNÈRE le mot de passe et l'affiche UNE SEULE FOIS. Il n'est ni stocké en clair,
    ni journalisé, ni auditable, et la commande ne saura pas le redonner : le perdre oblige
    à recommencer, ce qui est le comportement voulu.
  - Elle pose `must_change_password = true`. L'installateur change le mot de passe à sa
    première connexion, si bien que le mot de passe affiché sur un écran de terminal — donc
    potentiellement dans un historique de session ou une capture — ne vaut plus rien ensuite.
  - Elle REFUSE de s'exécuter si un compte existe déjà (sauf `--force`). Cette commande est
    un amorçage, pas une porte dérobée permanente : la laisser créer des administrateurs sur
    une base en production contournerait l'audit et le cloisonnement des agences.

Le rattachement à une agence est FACULTATIF, et rester sans agence est ici légitime : à
l'installation, aucune agence n'existe encore, et l'administrateur fonctionnel détient
`perimetre.reseau` — il voit donc tout le réseau sans être cloisonné.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.parameters.models import Agency
from app.modules.security.models import Role, User, UserRole
from app.modules.security.mots_de_passe import generer_mot_de_passe

ROLE_ADMIN = "ADMIN_FONCTIONNEL"

# Code de l'agence créée à l'amorçage. Généré, pas saisi : à l'installation, personne n'est
# là pour le décider, et il n'existe pas encore de codification d'agences propre à l'IMF.
CODE_AGENCE_SIEGE = "AG-001"


class ComptesDejaPresentsError(Exception):
    """La base porte déjà des comptes : l'amorçage n'a plus lieu d'être."""


class RoleIntrouvableError(Exception):
    """Le rôle d'administration n'existe pas — le seed n'a pas été joué."""


@dataclass(frozen=True)
class ResultatCreationAdmin:
    """Le compte créé, son mot de passe EN CLAIR, et l'agence de rattachement.

    Une IMF a TOUJOURS au moins une agence — même la plus petite mutuelle a un guichet. Une
    base sans agence ne correspond à aucune réalité ; l'amorçage en crée donc une (le siège)
    et y rattache l'administrateur. Sans ce rattachement, tout compte créé ensuite sans
    portée réseau serait invisible de quiconque n'a pas cette portée — un piège à corriger
    plus tard sur chaque compte.
    """

    user_id: uuid.UUID
    username: str
    email: str
    mot_de_passe: str
    agence_code: str
    agence_nom: str


def _agence_siege(db: Session, nom: str) -> Agency:
    """Rend l'agence de rattachement : la première existante, ou le siège créé à l'instant.

    Idempotent : à un amorçage normal (base neuve) le siège est créé ; au dépannage
    (--force sur une base déjà installée) on réutilise l'agence existante plutôt que d'en
    empiler une seconde. « Première existante » suffit — au dépannage, l'important est que
    l'admin soit rattaché quelque part, pas à quelle agence précise.
    """
    existante = db.execute(select(Agency).order_by(Agency.created_at).limit(1)).scalar_one_or_none()
    if existante is not None:
        return existante

    siege = Agency(code=CODE_AGENCE_SIEGE, name=nom)
    db.add(siege)
    db.flush()
    return siege


def creer_admin(
    db: Session,
    *,
    username: str,
    email: str,
    matricule: str,
    last_name: str,
    first_name: str,
    agence_nom: str = "Siège",
    force: bool = False,
) -> ResultatCreationAdmin:
    """Amorce une installation : agence siège + administrateur rattaché. Ne committe pas.

    `force` autorise la création alors que des comptes existent déjà. Réservé au dépannage
    — un réseau dont tous les administrateurs sont verrouillés, par exemple. Ce n'est pas
    un mode d'exploitation : toute création ultérieure doit passer par l'API, qui l'audite
    et applique le cloisonnement.
    """
    if not force:
        comptes = db.execute(
            select(func.count()).select_from(User).where(User.deleted_at.is_(None))
        ).scalar_one()
        if comptes:
            raise ComptesDejaPresentsError(comptes)

    role = db.execute(select(Role).where(Role.code == ROLE_ADMIN)).scalar_one_or_none()
    if role is None:
        raise RoleIntrouvableError(ROLE_ADMIN)

    agence = _agence_siege(db, agence_nom)

    genere = generer_mot_de_passe()
    admin = User(
        matricule=matricule,
        email=email,
        username=username,
        password_hash=genere.hash,
        last_name=last_name,
        first_name=first_name,
        primary_agency_id=agence.id,
        # Le mot de passe transite par un écran de terminal : il doit être périssable.
        must_change_password=True,
    )
    db.add(admin)
    db.flush()
    db.add(UserRole(user_id=admin.id, role_id=role.id))
    db.flush()

    # Pas d'écriture dans audit.audit_logs : la colonne user_id y désigne l'ACTEUR et
    # porte une FK vers security.users. À l'amorçage, l'acteur est un administrateur
    # système derrière un terminal, qui n'a pas de compte — il n'existe aucun identifiant
    # honnête à inscrire. Consigner l'acte sous l'identité du compte CRÉÉ reproduirait
    # exactement le faux que le bloc 4c a corrigé : « ce compte s'est créé lui-même ».
    # La trace de l'amorçage est ailleurs — c'est la première ligne de la table, et son
    # created_at fait foi.
    return ResultatCreationAdmin(
        user_id=admin.id,
        username=admin.username,
        email=admin.email,
        mot_de_passe=genere.clair,
        agence_code=agence.code,
        agence_nom=agence.name,
    )
