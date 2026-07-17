"""Service d'authentification — connexion et verrouillage progressif (§6). Sous-blocs 3a + 3b.

Périmètre : identifiant + mot de passe → jetons (3a), plus le compteur d'échecs et le
verrouillage progressif C7 (3b, premier sous-bloc qui ÉCRIT en base).

Hors périmètre, volontairement absent :
  - 3c : sessions en base et rotation des refresh tokens.
  - 3d : écriture d'audit (C5) et choix d'agence courante en multi-agences.

3b écrit sur DEUX chemins seulement — échec de mot de passe (compte existant, actif) et
succès. Les trois autres (inexistant, désactivé, verrouillé) restent sans écriture. Les
mutations et le commit sont isolés dans _enregistrer_echec / _enregistrer_succes : c'est
LE point où 3d insérera la trace d'audit, dans la MÊME transaction que le compteur.

DEUX SECRETS À NE JAMAIS LAISSER SORTIR :

  1. password_hash ne quitte jamais ce module. Aucun retour, aucun message, aucun log.
  2. La RAISON d'un échec ne sort jamais non plus. À l'extérieur, les quatre causes
     donnent le MÊME message générique — sinon la réponse dirait à un attaquant si le
     compte existe, s'il est actif, s'il est verrouillé. En interne, la cause est
     distinguée (CauseEchec, hors de args) pour le compteur (3b) et l'audit (3d).

DEUX PIÈGES, détaillés à leur emplacement :
  - l'ordre des contrôles est un oracle de timing (voir authentifier) ;
  - le commit du compteur doit précéder le raise, sinon un échec perd l'incrément.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import Select, select
from sqlalchemy.orm import Session, lazyload

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

# --- verrouillage progressif (C7) --------------------------------------------------

# 5 échecs consécutifs → verrou (§6).
SEUIL_VERROUILLAGE = 5

# Durée = 15 min * 2^palier : 15 / 30 / 60 / 120. Le palier sature à 3, donc la durée
# plafonne à 120 min — un attaquant ne peut pas faire enfler le délai indéfiniment.
DUREE_BASE_VERROU = timedelta(minutes=15)
PALIER_DUREE_MAX = 3

# lockout_count stocké est borné : au-delà du plafond de durée il n'a plus d'effet, et
# on évite qu'un entier grandisse sans fin sous un pilonnage prolongé.
PLAFOND_LOCKOUT_COUNT = PALIER_DUREE_MAX + 1

# 24 h sans nouveau verrou → la progression 15/30/60/120 repart de 15 (C7).
FENETRE_REINITIALISATION = timedelta(hours=24)


class CauseEchec(StrEnum):
    """Cause INTERNE d'un échec. Ne franchit jamais la frontière du service.

    Le compteur (3b) ne s'incrémente que sur MOT_DE_PASSE_INVALIDE ; 3d journalisera la
    cause exacte. À l'extérieur, les quatre valeurs sont indistinguables (même message,
    même ordre de grandeur de timing).
    """

    COMPTE_INEXISTANT = "compte_inexistant"
    MOT_DE_PASSE_INVALIDE = "mot_de_passe_invalide"
    COMPTE_DESACTIVE = "compte_desactive"
    COMPTE_VERROUILLE = "compte_verrouille"


class EchecAuthentificationError(Exception):
    """Échec de connexion. Message générique dehors, cause distincte dedans.

    Le message (args[0]) est toujours MESSAGE_ECHEC_GENERIQUE : c'est lui, et lui seul,
    que la couche API renverra. cause et user_id sont des attributs hors de args, donc
    absents du repr par défaut — ils servent le compteur (3b) et l'audit (3d), jamais la
    réponse HTTP. user_id est None quand le compte est introuvable.
    """

    def __init__(self, cause: CauseEchec, user_id: uuid.UUID | None = None) -> None:
        super().__init__(MESSAGE_ECHEC_GENERIQUE)
        self.cause = cause
        self.user_id = user_id


@dataclass(frozen=True)
class ResultatConnexion:
    """Succès de connexion. Ne porte aucun champ sensible.

    rehash_recommande : le mot de passe est correct mais son hash date de paramètres
    Argon2 antérieurs (cf. rehachage_necessaire). Signalé, pas exécuté : le re-hachage
    (écriture de password_hash) reste au bloc qui possède ce sujet, pas à 3b.
    """

    user_id: uuid.UUID
    access_token: str
    refresh_token: str
    rehash_recommande: bool


def _selectionner_pour_maj(identifiant: str) -> Select[tuple[User]]:
    """Requête de chargement de l'utilisateur, VERROU DE LIGNE compris (FOR UPDATE).

    with_for_update() sérialise deux tentatives parallèles sur le MÊME compte : la
    seconde attend le commit de la première, donc lit un compteur à jour. Sans lui, deux
    requêtes simultanées liraient le même failed_attempts et l'incrémenteraient chacune
    à la même valeur — un compteur de sécurité contournable par la concurrence ne protège
    rien. Des comptes différents ne se gênent pas (verrou de ligne, pas de table).

    Un compte soft-deleted est exclu (deleted_at IS NULL) : traité comme inexistant, il
    ne s'authentifie jamais et le refuser ne révèle pas qu'il a existé.
    """
    return (
        select(User)
        .where(
            (User.username == identifiant) | (User.email == identifiant),
            User.deleted_at.is_(None),
        )
        # User.roles et primary_agency sont configurés en selectin (chargement eager) :
        # sans ceci, les charger ferait deux requêtes de plus À CHAQUE tentative, y
        # compris sur les chemins d'échec qui ne les utilisent pas — travail gaspillé, et
        # écart de timing élargi entre « compte existant » et « compte inexistant ». On les
        # rend paresseux ici : ils ne se chargent qu'au succès, quand on lit les rôles.
        .options(lazyload(User.roles), lazyload(User.primary_agency))
        .with_for_update()
    )


def _verrou_actif(user: User, maintenant: datetime) -> bool:
    """Le compte est-il verrouillé À CET INSTANT ?

    Un verrou temporisé (is_locked + locked_until) n'est actif que tant que locked_until
    est dans le futur : la fenêtre écoulée, le compte n'est plus bloqué (le drapeau
    is_locked résiduel est nettoyé dans le chemin d'écriture). Un is_locked sans échéance
    (locked_until NULL) reste un verrou — réservé à un futur verrou administratif.
    """
    if user.locked_until is not None and user.locked_until > maintenant:
        return True
    return user.is_locked and user.locked_until is None


def _palier_effectif(user: User, maintenant: datetime) -> int:
    """Palier d'escalade à utiliser pour le PROCHAIN verrou, règle des 24 h appliquée.

    Si le dernier verrou date de plus de 24 h (ou n'a jamais eu lieu), la progression
    repart de zéro → durée de base 15 min. C'est ici, au moment de poser un verrou, que
    la remise à zéro C7 prend effet — pas à chaque tentative.
    """
    if user.last_lockout_at is None:
        return 0
    if maintenant - user.last_lockout_at > FENETRE_REINITIALISATION:
        return 0
    return user.lockout_count


def _duree_verrou(palier: int) -> timedelta:
    """15 / 30 / 60 / 120 min. Le palier sature à PALIER_DUREE_MAX, la durée à 120."""
    # 1 << k == 2^k, en int franc : « 2 ** k » est typé Any par mypy (pow peut rendre
    # un float pour un exposant négatif), ce qui contaminerait le type de retour.
    multiplicateur = 1 << min(palier, PALIER_DUREE_MAX)
    return DUREE_BASE_VERROU * multiplicateur


def _enregistrer_echec(db: Session, user: User, maintenant: datetime) -> None:
    """Compte l'échec, pose le verrou au seuil, puis COMMITTE — avant que l'appelant lève.

    Le commit précède le raise à dessein : un échec de connexion ne doit pas empêcher la
    persistance du compteur, sinon le verrouillage ne servirait à rien.

    POINT DE COUTURE 3d : la trace d'audit (échec / verrouillage) s'insérera juste avant
    le commit, dans CETTE transaction. L'ordre des verrous reste constant — ligne user
    (FOR UPDATE) d'abord, verrou consultatif du chaînage d'audit ensuite — donc pas de
    deadlock avec le trigger de chaînage.
    """
    # Un verrou temporisé échu laisse is_locked à true : on le nettoie avant de compter,
    # pour repartir sur une série fraîche (failed_attempts a été remis à 0 à la pose).
    if user.is_locked and not _verrou_actif(user, maintenant):
        user.is_locked = False
        user.locked_until = None

    tentatives = user.failed_attempts + 1
    if tentatives >= SEUIL_VERROUILLAGE:
        palier = _palier_effectif(user, maintenant)
        user.locked_until = maintenant + _duree_verrou(palier)
        user.is_locked = True
        user.lockout_count = min(palier + 1, PLAFOND_LOCKOUT_COUNT)
        user.last_lockout_at = maintenant
        # Les échecs sont « consommés » dans le verrou : à l'expiration, le compte
        # repart sur SEUIL tentatives fraîches, et non une seule.
        user.failed_attempts = 0
    else:
        user.failed_attempts = tentatives

    db.commit()


def _enregistrer_succes(db: Session, user: User, maintenant: datetime) -> None:
    """Remet le compteur à zéro, lève tout verrou résiduel, puis COMMITTE.

    POINT DE COUTURE 3d : la trace d'audit (connexion réussie) s'insérera juste avant le
    commit, dans cette transaction.
    """
    user.failed_attempts = 0
    user.is_locked = False
    user.locked_until = None
    # Tenue à jour de la valeur en base : après 24 h calmes, l'escalade est réputée nulle.
    if (
        user.last_lockout_at is not None
        and maintenant - user.last_lockout_at > FENETRE_REINITIALISATION
    ):
        user.lockout_count = 0

    db.commit()


def authentifier(db: Session, identifiant: str, mot_de_passe: str) -> ResultatConnexion:
    """Authentifie par (username OU email) + mot de passe. Émet access + refresh.

    identifiant : username (sensible à la casse, VARCHAR) ou email (insensible, CITEXT).
    Une seule requête couvre les deux ; la casse de l'email est gérée par le type CITEXT.

    ORDRE CRITIQUE — NE PAS RÉORGANISER. Exactement UNE vérification Argon2 est faite
    AVANT tout branchement sur l'état du compte : contre le password_hash si le compte
    existe, contre HASH_LEURRE sinon. « Ranger » les contrôles dans l'ordre naturel —
    tester is_active / verrou d'abord et sortir tôt — ferait qu'un compte désactivé ou
    verrouillé répondrait en ~0 ms au lieu de ~22 ms, et cet écart révélerait « ce compte
    existe et il est bloqué », contournant l'anti-énumération par une autre porte. Le
    branchement qui suit ne coûte que des nanosecondes ; l'ordre des `if` ne fixe donc que
    la CAUSE interne, jamais le temps de réponse.

    ÉCART DE TIMING ASSUMÉ (3b) : le chemin « compte existant, mot de passe faux » fait
    désormais un UPDATE + COMMIT (~quelques ms) que le chemin « compte inexistant » ne
    fait pas. Argon2 (~22 ms) domine, aucun chemin ne retombe à ~0 ms : l'oracle reste
    fermé. On ne cherche pas à égaliser au millimètre — un commit à vide côté inexistant
    serait quasi gratuit (PostgreSQL n'écrit pas de WAL) et ne compenserait rien. Mesuré
    et gardé par les tests de timing.

    Lève EchecAuthentificationError (message générique) dans tous les cas d'échec.
    """
    maintenant = datetime.now(UTC)
    user = db.execute(_selectionner_pour_maj(identifiant)).scalar_one_or_none()

    # Toujours exactement un Argon2, quel que soit le sort du compte (cf. ORDRE CRITIQUE).
    hash_a_verifier = user.password_hash if user is not None else HASH_LEURRE
    mot_de_passe_ok = verifier_mot_de_passe(mot_de_passe, hash_a_verifier)

    # Précédence : existence, état admin, verrou, mot de passe. Un compte bloqué n'expose
    # pas si le mot de passe était bon, et ne réincrémente rien tant qu'il est verrouillé.
    if user is None:
        raise EchecAuthentificationError(CauseEchec.COMPTE_INEXISTANT)
    if not user.is_active:
        raise EchecAuthentificationError(CauseEchec.COMPTE_DESACTIVE, user.id)
    if _verrou_actif(user, maintenant):
        # Aucune écriture : un verrou actif ne se re-déclenche pas et ne réincrémente pas
        # lockout_count à chaque tentative, sinon le délai enflerait artificiellement.
        raise EchecAuthentificationError(CauseEchec.COMPTE_VERROUILLE, user.id)
    if not mot_de_passe_ok:
        _enregistrer_echec(db, user, maintenant)  # committe AVANT de lever
        raise EchecAuthentificationError(CauseEchec.MOT_DE_PASSE_INVALIDE, user.id)

    # Succès : remise à zéro du compteur, puis émission des jetons. En 3a/3b, l'agence
    # courante du jeton EST l'agence de rattachement ; le choix d'une autre agence
    # (multi-agences, C6) viendra en 3d. roles vient de la relation viewonly User.roles.
    _enregistrer_succes(db, user, maintenant)

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
