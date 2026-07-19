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


class ComptesDejaPresentsError(Exception):
    """La base porte déjà des comptes : l'amorçage n'a plus lieu d'être."""


class RoleIntrouvableError(Exception):
    """Le rôle d'administration n'existe pas — le seed n'a pas été joué."""


class AgenceIntrouvableError(Exception):
    """Le code d'agence demandé ne correspond à rien."""


@dataclass(frozen=True)
class ResultatCreationAdmin:
    """Le compte créé et son mot de passe EN CLAIR — à afficher une fois, puis à oublier."""

    user_id: uuid.UUID
    username: str
    email: str
    mot_de_passe: str


def creer_admin(
    db: Session,
    *,
    username: str,
    email: str,
    matricule: str,
    last_name: str,
    first_name: str,
    agence_code: str | None = None,
    force: bool = False,
) -> ResultatCreationAdmin:
    """Crée l'administrateur d'installation. Ne committe pas : l'appelant décide.

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

    agence_id: uuid.UUID | None = None
    if agence_code is not None:
        agence = db.execute(select(Agency).where(Agency.code == agence_code)).scalar_one_or_none()
        if agence is None:
            raise AgenceIntrouvableError(agence_code)
        agence_id = agence.id

    genere = generer_mot_de_passe()
    admin = User(
        matricule=matricule,
        email=email,
        username=username,
        password_hash=genere.hash,
        last_name=last_name,
        first_name=first_name,
        primary_agency_id=agence_id,
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
    )
