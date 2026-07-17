"""Service d'authentification — flux de connexion (§6). Sous-bloc 3a.

Périmètre 3a : identifiant + mot de passe → jetons. On lit l'utilisateur, on vérifie le
mot de passe, on contrôle l'état du compte, on émet access + refresh. RIEN d'autre.

Hors 3a, volontairement absent ici :
  - 3b : incrément du compteur d'échecs et verrouillage progressif (C7).
  - 3c : sessions en base et rotation des refresh tokens.
  - 3d : écriture d'audit (C5) et choix d'agence courante en multi-agences.
Ce module NE FAIT AUCUNE ÉCRITURE en base : il lit et il émet des jetons. Le point où
3c/3d écriront (last_login_at, re-hachage) est signalé mais pas exécuté ici.

DEUX SECRETS À NE JAMAIS LAISSER SORTIR :

  1. password_hash ne quitte jamais ce module. Aucun retour, aucun message d'exception,
     aucun log ne le porte.
  2. La RAISON d'un échec ne sort jamais non plus. À l'extérieur, les quatre causes
     (compte inexistant, mot de passe faux, compte désactivé, compte verrouillé) donnent
     le MÊME message générique — sinon la réponse dirait à un attaquant si le compte
     existe, s'il est actif, s'il est verrouillé. En interne, la cause est distinguée
     (CauseEchec, portée par l'exception hors de args) pour 3b (compteur) et 3d (audit).

PIÈGE DE TIMING — l'ordre des contrôles EST un oracle. Voir authentifier().
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.security.jwt import creer_access_token, creer_refresh_token
from app.modules.security.models import User
from app.modules.security.password import (
    HASH_LEURRE,
    rehachage_necessaire,
    verifier_mot_de_passe,
)

# Message unique renvoyé à l'extérieur pour TOUT échec. Un seul littéral, partagé, pour
# qu'aucune divergence ne se glisse entre les cas. Ne jamais y ajouter la cause.
MESSAGE_ECHEC_GENERIQUE = "Identifiant ou mot de passe incorrect."


class CauseEchec(StrEnum):
    """Cause INTERNE d'un échec. Ne franchit jamais la frontière du service.

    Existe pour les sous-blocs suivants : 3b n'incrémente le compteur que sur
    MOT_DE_PASSE_INVALIDE, 3d journalise la cause exacte. À l'extérieur, les quatre
    valeurs sont indistinguables (même message, même timing).
    """

    COMPTE_INEXISTANT = "compte_inexistant"
    MOT_DE_PASSE_INVALIDE = "mot_de_passe_invalide"
    COMPTE_DESACTIVE = "compte_desactive"
    COMPTE_VERROUILLE = "compte_verrouille"


class EchecAuthentificationError(Exception):
    """Échec de connexion. Message générique dehors, cause distincte dedans.

    Le message (args[0]) est toujours MESSAGE_ECHEC_GENERIQUE : c'est lui, et lui seul,
    que la couche API renverra. cause et user_id sont des attributs hors de args, donc
    absents du repr par défaut — ils servent 3b et 3d, jamais la réponse HTTP.

    user_id est None quand le compte est introuvable : il n'y a personne à désigner.
    """

    def __init__(self, cause: CauseEchec, user_id: uuid.UUID | None = None) -> None:
        super().__init__(MESSAGE_ECHEC_GENERIQUE)
        self.cause = cause
        self.user_id = user_id


@dataclass(frozen=True)
class ResultatConnexion:
    """Succès de connexion. Ne porte aucun champ sensible.

    rehash_recommande : le mot de passe est correct mais son hash date de paramètres
    Argon2 antérieurs (cf. rehachage_necessaire). 3a ne réécrit rien — il reste en
    lecture seule — et laisse le signal à un bloc qui touche déjà la ligne user
    (last_login_at en 3c/3d), pour re-hacher sans transaction supplémentaire.
    """

    user_id: uuid.UUID
    access_token: str
    refresh_token: str
    rehash_recommande: bool


def _compte_verrouille(user: User) -> bool:
    """Verrou C7 : drapeau explicite, ou fenêtre locked_until encore ouverte.

    locked_until est TIMESTAMPTZ : la comparaison se fait en aware UTC. Une fenêtre
    échue (locked_until dans le passé) ne verrouille plus — c'est 3b qui gère la levée.
    """
    if user.is_locked:
        return True
    if user.locked_until is None:
        return False
    return user.locked_until > datetime.now(UTC)


def authentifier(db: Session, identifiant: str, mot_de_passe: str) -> ResultatConnexion:
    """Authentifie par (username OU email) + mot de passe. Émet access + refresh.

    identifiant : username (sensible à la casse, VARCHAR) ou email (insensible, CITEXT).
    Une seule requête couvre les deux ; la casse de l'email est gérée par le type CITEXT
    côté base, pas par le code.

    ORDRE CRITIQUE — ne pas réorganiser. Exactement UNE vérification Argon2 est faite
    AVANT tout branchement sur l'état du compte :

      - compte trouvé   → on vérifie contre son password_hash ;
      - compte absent   → on vérifie quand même, contre HASH_LEURRE.

    Sans cela, un compte désactivé ou verrouillé renverrait sans passer par Argon2, donc
    en ~0 ms au lieu de ~22 ms : l'écart révélerait « ce compte existe et il est bloqué »,
    contournant l'anti-énumération par une autre porte. Le branchement qui suit ne coûte
    que des nanosecondes — l'ordre des `if` ne fixe donc que la CAUSE rapportée, jamais le
    temps de réponse. Couvert par les tests de timing de test_auth.py.

    Lève EchecAuthentificationError (message générique) dans tous les cas d'échec.
    """
    user = db.execute(
        select(User).where(
            (User.username == identifiant) | (User.email == identifiant),
            # Un compte soft-deleted est traité comme inexistant : il ne doit jamais
            # s'authentifier, et le dire ne doit pas révéler qu'il a existé.
            User.deleted_at.is_(None),
        )
    ).scalar_one_or_none()

    # Toujours exactement un Argon2, quel que soit le sort du compte (cf. ORDRE CRITIQUE).
    hash_a_verifier = user.password_hash if user is not None else HASH_LEURRE
    mot_de_passe_ok = verifier_mot_de_passe(mot_de_passe, hash_a_verifier)

    # À partir d'ici, tout est en mémoire : l'ordre des contrôles ne change que la cause
    # interne, plus le timing. Précédence : existence, puis état admin, puis verrou, puis
    # mot de passe — un compte bloqué n'expose pas si le mot de passe était bon.
    if user is None:
        raise EchecAuthentificationError(CauseEchec.COMPTE_INEXISTANT)
    if not user.is_active:
        raise EchecAuthentificationError(CauseEchec.COMPTE_DESACTIVE, user.id)
    if _compte_verrouille(user):
        raise EchecAuthentificationError(CauseEchec.COMPTE_VERROUILLE, user.id)
    if not mot_de_passe_ok:
        raise EchecAuthentificationError(CauseEchec.MOT_DE_PASSE_INVALIDE, user.id)

    # Succès. En 3a, l'agence courante du jeton EST l'agence de rattachement : le choix
    # d'une autre agence (multi-agences, C6) viendra en 3d. roles vient de la relation
    # viewonly User.roles (selectin) ; on n'émet que des codes de rôles, pas d'UUID.
    roles = tuple(role.code for role in user.roles)
    access_token = creer_access_token(
        user_id=user.id,
        roles=roles,
        primary_agency_id=user.primary_agency_id,
        agency_id=user.primary_agency_id,
    )
    refresh_token = creer_refresh_token(user_id=user.id)

    return ResultatConnexion(
        user_id=user.id,
        access_token=access_token,
        refresh_token=refresh_token,
        rehash_recommande=rehachage_necessaire(user.password_hash),
    )
