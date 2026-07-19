"""Cycle de vie du mot de passe (bloc 4c) — génération, changement, historique (C12).

Trois opérations, un seul endroit, parce qu'elles partagent les mêmes invariants : écrire
un mot de passe, c'est toujours hacher, historiser, purger au-delà de 12, et décider du
sort de must_change_password et des sessions.

LE CLAIR NE SURVIT PAS À L'APPEL. Un mot de passe généré est rendu à l'appelant et n'est
écrit NULLE PART ailleurs : ni en base (seul le hash), ni dans l'audit (le service refuse
activement les clés sensibles), ni dans un log. C'est pourquoi generer_mot_de_passe rend une
chaîne nue plutôt que de la ranger quelque part « au cas où » : il n'y a pas de cas où.

HISTORIQUE (C12). Chaque écriture pousse l'ANCIEN hash dans user_passwords_history et purge
au-delà des 12 derniers. On historise l'ancien et non le nouveau : le hash courant vit déjà
dans users.password_hash, l'y dupliquer ferait compter deux fois le même mot de passe dans
la fenêtre des douze.
"""

import secrets
import string
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.modules.security.models import User, UserPasswordHistory
from app.modules.security.password import (
    POLITIQUE_PAR_DEFAUT,
    PolitiqueMotDePasse,
    RegleMotDePasse,
    hasher_mot_de_passe,
    mot_de_passe_deja_utilise,
    valider_politique,
    verifier_mot_de_passe,
)

# C12 — profondeur de l'historique interdit à la réutilisation.
PROFONDEUR_HISTORIQUE = 12

# Longueur du mot de passe GÉNÉRÉ. Au-dessus du minimum de la politique (12) : un mot de
# passe provisoire transite à l'oral ou sur papier, il doit résister sans être ressaisi
# mille fois. 16 caractères tirés au sort donnent une entropie hors de portée d'une attaque
# hors ligne, même si le hash fuitait.
LONGUEUR_GENEREE = 16

MESSAGE_MOT_DE_PASSE_ACTUEL_INVALIDE = "Le mot de passe actuel est incorrect."


class MotDePasseInvalideError(Exception):
    """Le nouveau mot de passe est refusé. `violations` dit pourquoi, sans le citer."""

    def __init__(self, violations: tuple[RegleMotDePasse, ...] = ()) -> None:
        super().__init__("Mot de passe non conforme.")
        self.violations = violations


class MotDePasseDejaUtiliseError(Exception):
    """Le nouveau mot de passe figure parmi les 12 derniers (C12)."""


class MotDePasseActuelInvalideError(Exception):
    """L'ancien mot de passe fourni ne correspond pas."""


@dataclass(frozen=True)
class ResultatGeneration:
    """Un mot de passe généré et son hash.

    `clair` ne doit être lu qu'une fois, pour être rendu dans LA réponse de création ou de
    réinitialisation. Ne jamais le stocker, l'auditer ni le journaliser.
    """

    clair: str
    hash: str


def generer_mot_de_passe(
    politique: PolitiqueMotDePasse = POLITIQUE_PAR_DEFAUT,
) -> ResultatGeneration:
    """Tire un mot de passe conforme à la politique, avec `secrets` (CSPRNG).

    La construction garantit la conformité au lieu de tirer puis retenter : un caractère
    est pris dans chaque famille exigée, le reste est complété au hasard, puis l'ensemble
    est mélangé — sans quoi les premiers caractères trahiraient l'ordre des familles.

    L'assertion finale n'est pas décorative : si la politique était durcie d'une règle que
    cette fonction ignore, elle produirait des mots de passe que le service refuserait
    ensuite. Mieux vaut échouer ici, bruyamment.
    """
    familles: list[str] = []
    if politique.exige_majuscule:
        familles.append(string.ascii_uppercase)
    if politique.exige_minuscule:
        familles.append(string.ascii_lowercase)
    if politique.exige_chiffre:
        familles.append(string.digits)
    if politique.exige_caractere_special:
        familles.append("!@#$%&*+-=?")

    alphabet = "".join(familles) or string.ascii_letters + string.digits
    longueur = max(LONGUEUR_GENEREE, politique.longueur_minimale)

    caracteres = [secrets.choice(famille) for famille in familles]
    caracteres += [secrets.choice(alphabet) for _ in range(longueur - len(caracteres))]
    secrets.SystemRandom().shuffle(caracteres)
    clair = "".join(caracteres)

    resultat = valider_politique(clair, politique)
    assert resultat.est_conforme, f"générateur non conforme à la politique : {resultat.violations}"

    return ResultatGeneration(clair=clair, hash=hasher_mot_de_passe(clair))


def _hashs_precedents(db: Session, user_id: uuid.UUID) -> list[str]:
    return list(
        db.execute(
            select(UserPasswordHistory.password_hash)
            .where(UserPasswordHistory.user_id == user_id)
            .order_by(UserPasswordHistory.created_at.desc())
            .limit(PROFONDEUR_HISTORIQUE)
        ).scalars()
    )


def _historiser_et_purger(db: Session, user: User, maintenant: datetime) -> None:
    """Pousse le hash COURANT dans l'historique, puis élague au-delà de 12 (C12)."""
    db.add(UserPasswordHistory(user_id=user.id, password_hash=user.password_hash))
    db.flush()

    a_garder = db.execute(
        select(UserPasswordHistory.id)
        .where(UserPasswordHistory.user_id == user.id)
        .order_by(UserPasswordHistory.created_at.desc())
        .limit(PROFONDEUR_HISTORIQUE)
    ).scalars()
    db.execute(
        delete(UserPasswordHistory).where(
            UserPasswordHistory.user_id == user.id,
            UserPasswordHistory.id.notin_(list(a_garder)),
        )
    )


def ecrire_mot_de_passe(
    db: Session,
    user: User,
    nouveau_hash: str,
    *,
    doit_changer: bool,
    maintenant: datetime | None = None,
) -> None:
    """Pose un nouveau hash : historise l'ancien, purge, met à jour les dates et le drapeau.

    Ne committe pas — l'appelant tient la transaction, pour que l'audit y entre aussi.
    """
    maintenant = maintenant or datetime.now(UTC)
    _historiser_et_purger(db, user, maintenant)
    user.password_hash = nouveau_hash
    user.password_changed_at = maintenant
    user.must_change_password = doit_changer
    db.flush()


def changer_son_mot_de_passe(
    db: Session,
    user: User,
    mot_de_passe_actuel: str,
    nouveau: str,
    politique: PolitiqueMotDePasse = POLITIQUE_PAR_DEFAUT,
) -> None:
    """Changement SELF-SERVICE : l'utilisateur prouve l'ancien pour poser le nouveau.

    C'est la seule porte par laquelle must_change_password se lève. Sans elle, un compte
    créé avec un mot de passe provisoire serait mort-né : exige() lui refuserait tout, y
    compris le moyen de lever la contrainte.

    L'ANCIEN MOT DE PASSE EST EXIGÉ même si l'appelant est déjà authentifié. Un access token
    peut avoir été volé (poste non verrouillé, XSS malgré les protections) ; sans cette
    preuve, le voleur changerait le mot de passe et prendrait le compte définitivement. La
    re-preuve transforme un vol de session, temporaire, en une impasse.

    L'ordre des contrôles est délibéré : l'ancien mot de passe d'abord, la politique
    ensuite. Vérifier la conformité avant l'identité dirait à un porteur de jeton volé si
    son essai de nouveau mot de passe est acceptable — une information gratuite.
    """
    if not verifier_mot_de_passe(mot_de_passe_actuel, user.password_hash):
        raise MotDePasseActuelInvalideError(MESSAGE_MOT_DE_PASSE_ACTUEL_INVALIDE)

    resultat = valider_politique(nouveau, politique)
    if not resultat.est_conforme:
        raise MotDePasseInvalideError(resultat.violations)

    # C12 : ni le mot de passe courant, ni les 12 derniers.
    if mot_de_passe_deja_utilise(nouveau, [user.password_hash, *_hashs_precedents(db, user.id)]):
        raise MotDePasseDejaUtiliseError()

    ecrire_mot_de_passe(db, user, hasher_mot_de_passe(nouveau), doit_changer=False)
