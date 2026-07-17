"""Service d'authentification — connexion, verrouillage, sessions (§6). Sous-blocs 3a+3b+3c.

Périmètre : identifiant + mot de passe → jetons (3a) ; compteur d'échecs et verrouillage
progressif C7 (3b) ; sessions en base et rotation des refresh tokens avec détection de vol
(3c).

Hors périmètre, volontairement absent :
  - 3d : écriture d'audit (C5) et choix d'agence courante en multi-agences.
  - Bloc 4 : les endpoints API. Ici on fait la LOGIQUE, pas l'API.
  - La purge des sessions révoquées (job de nettoyage différé) : à écrire plus tard.

Les mutations et le commit sont isolés dans des helpers (_enregistrer_echec,
_enregistrer_succes, _appliquer_rotation, _revoquer_toutes_les_sessions) : c'est LE point
où 3d insérera la trace d'audit, dans la MÊME transaction que l'écriture métier. L'ordre
des verrous reste constant — ligne user/session (FOR UPDATE) puis verrou consultatif du
chaînage d'audit — donc pas de deadlock avec le trigger de chaînage.

SECRETS À NE JAMAIS LAISSER SORTIR :

  1. password_hash et le refresh token EN CLAIR ne quittent jamais ce module. En base, le
     refresh n'existe que sous forme de hash SHA-256 (refresh_token_hash) ; le clair n'est
     ni stocké ni journalisé.
  2. La RAISON d'un refus ne sort jamais non plus. À l'extérieur, toutes les causes donnent
     le MÊME message générique — au login pour ne pas révéler l'existence/l'état d'un
     compte, au refresh pour ne pas dire à un attaquant « vol détecté ». En interne, la
     cause est distinguée (hors de args) pour le compteur (3b) et l'audit (3d).

PIÈGES, détaillés à leur emplacement :
  - l'ordre des contrôles au login est un oracle de timing (voir authentifier) ;
  - le commit du compteur doit précéder le raise, sinon un échec perd l'incrément ;
  - la rotation d'un refresh est atomique sous FOR UPDATE (voir rafraichir).
"""

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import Select, select, update
from sqlalchemy.orm import Session, lazyload

from app.modules.security.jwt import (
    JetonExpireError,
    JetonInvalideError,
    TypeDeJetonInvalideError,
    creer_access_token,
    creer_refresh_token,
    decoder_refresh_token,
)
from app.modules.security.models import User, UserSession
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


# Message unique pour TOUT refus de rafraîchissement. Ne jamais dire dehors laquelle des
# causes s'applique — surtout pas « réutilisation détectée », qui préviendrait le voleur.
MESSAGE_REFRESH_REFUSE = "Session invalide. Veuillez vous reconnecter."


class CauseRefresh(StrEnum):
    """Cause INTERNE d'un refus de rafraîchissement. Ne franchit jamais la frontière.

    Distinguée pour l'audit (3d) ; à l'extérieur, toutes donnent MESSAGE_REFRESH_REFUSE.
    """

    TOKEN_INVALIDE = "token_invalide"
    TOKEN_EXPIRE = "token_expire"
    TYPE_INVALIDE = "type_invalide"
    SESSION_INTROUVABLE = "session_introuvable"
    SESSION_EXPIREE = "session_expiree"
    HASH_INCOHERENT = "hash_incoherent"
    COMPTE_INDISPONIBLE = "compte_indisponible"
    REUTILISATION_DETECTEE = "reutilisation_detectee"


class RafraichissementError(Exception):
    """Refus de rafraîchissement. Message générique dehors, cause distincte dedans.

    Même contrat que EchecAuthentificationError : args[0] est toujours le message
    générique ; cause et user_id sont des attributs hors de args, pour l'audit (3d).
    """

    def __init__(self, cause: CauseRefresh, user_id: uuid.UUID | None = None) -> None:
        super().__init__(MESSAGE_REFRESH_REFUSE)
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


# --- sessions et refresh tokens (C7 sessions, 3c) ----------------------------------


def _hash_refresh(refresh_token: str) -> str:
    """SHA-256 du refresh token — jamais le clair en base.

    SHA-256 et non Argon2, décidé : un refresh token est une chaîne aléatoire à haute
    entropie (jti uuid4 + signature), pas un mot de passe humain. La lenteur d'Argon2
    n'apporte rien contre une force brute déjà impraticable ; le seul rôle du hash est
    qu'un vol de la base ne permette pas de REJOUER les tokens. Pas de sel non plus,
    inutile sur une entrée déjà imprévisible.
    """
    return hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()


def _creer_session(
    user_id: uuid.UUID, refresh_token: str, ip: str | None, user_agent: str | None
) -> UserSession:
    """Fabrique la ligne user_sessions d'un refresh token, SANS l'ajouter à la session DB.

    L'id de la session EST le jti du token (pas de colonne jti dédiée, pas de migration) :
    chaque refresh correspond à une session, retrouvée par son jti = clé primaire. issued_at
    et expires_at viennent des claims signés, pour rester cohérents avec le token lui-même.
    """
    claims = decoder_refresh_token(refresh_token)
    return UserSession(
        id=claims.jti,
        user_id=user_id,
        refresh_token_hash=_hash_refresh(refresh_token),
        issued_at=claims.iat,
        expires_at=claims.exp,
        ip=ip,
        user_agent=user_agent,
    )


def _revoquer_toutes_les_sessions(db: Session, user_id: uuid.UUID, maintenant: datetime) -> None:
    """Révoque TOUTES les sessions actives d'un utilisateur — réaction à une détection de vol.

    Ne committe pas : l'appelant committe, pour laisser 3d insérer l'audit « vol détecté »
    dans la même transaction. synchronize_session="fetch" tient à jour les objets déjà en
    mémoire, pour que l'appelant et les tests voient la révocation sans relire la base.
    """
    db.execute(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=maintenant)
        .execution_options(synchronize_session="fetch")
    )


def _appliquer_rotation(
    db: Session, ancienne: UserSession, nouvelle: UserSession, maintenant: datetime
) -> None:
    """Rotation atomique : révoque l'ancienne session, la chaîne à la nouvelle, COMMITTE.

    Sous le FOR UPDATE tenu par rafraichir : révocation + création + chaînage forment un
    tout. Une panne au milieu laisserait soit deux sessions valides pour un même token,
    soit zéro — l'unique commit l'interdit.

    POINT DE COUTURE 3d : la trace d'audit (rafraîchissement) s'insérera juste avant.
    """
    # La nouvelle session doit EXISTER avant que l'ancienne la référence : replaced_by_
    # session_id est une FK auto-référente. On fixe la colonne directement (pas la
    # relation), donc l'unit-of-work ne connaît pas la dépendance et ordonnerait l'UPDATE
    # avant l'INSERT → violation de FK. Le flush explicite insère la nouvelle d'abord.
    db.add(nouvelle)
    db.flush()
    ancienne.revoked_at = maintenant
    ancienne.replaced_by_session_id = nouvelle.id
    db.commit()


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


def _enregistrer_succes(
    db: Session, user: User, session: UserSession, maintenant: datetime
) -> None:
    """Remet le compteur à zéro, PERSISTE la nouvelle session, puis COMMITTE.

    La session est créée dans la MÊME transaction que la remise à zéro du compteur (3c) :
    une connexion réussie et sa session naissent ou échouent ensemble.

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

    db.add(session)
    db.commit()


def authentifier(
    db: Session,
    identifiant: str,
    mot_de_passe: str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> ResultatConnexion:
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

    # Succès : émission des jetons, création de la session, remise à zéro du compteur —
    # le tout dans une seule transaction (cf. _enregistrer_succes). En 3a/3b/3c, l'agence
    # courante du jeton EST l'agence de rattachement ; le choix d'une autre agence
    # (multi-agences, C6) viendra en 3d. roles vient de la relation viewonly User.roles.
    roles = tuple(role.code for role in user.roles)
    access_token = creer_access_token(
        user_id=user.id,
        roles=roles,
        primary_agency_id=user.primary_agency_id,
        agency_id=user.primary_agency_id,
    )
    refresh_token = creer_refresh_token(user_id=user.id)
    session = _creer_session(user.id, refresh_token, ip, user_agent)

    _enregistrer_succes(db, user, session, maintenant)

    return ResultatConnexion(
        user_id=user.id,
        access_token=access_token,
        refresh_token=refresh_token,
        rehash_recommande=rehachage_necessaire(user.password_hash),
    )


def rafraichir(
    db: Session,
    refresh_token: str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> ResultatConnexion:
    """Rotation d'un refresh token : émet un nouveau couple, révoque l'ancien, détecte le vol.

    Étapes : décoder (via jwt.py — signature, expiration ET type refresh, le piège du
    bloc 2) → charger la session par son jti sous FOR UPDATE → vérifier existence,
    non-réutilisation, non-expiration, authenticité du hash, état du compte → tourner.

    DÉTECTION DE VOL (le cœur de 3c). Si la session existe mais est DÉJÀ RÉVOQUÉE, c'est
    qu'un token consommé recircule : quelqu'un rejoue un refresh déjà tourné. On révoque
    TOUTES les sessions de l'utilisateur (déconnexion totale) et on refuse. Ce cas englobe
    le double-submit d'un même token — voulu : présenter deux fois le même refresh est
    traité comme un vol, pas comme une erreur bénigne.

    PAS DE FAUX POSITIF MULTI-APPAREILS. Chaque appareil a SA session (son jti, son token).
    Rafraîchir l'appareil A ne révoque que la session de A ; celle de B, d'un autre jti,
    est intacte. La détection ne se déclenche que sur la réutilisation d'un token révoqué,
    jamais sur des sessions actives distinctes qui coexistent légitimement.

    CONCURRENCE. Le FOR UPDATE sérialise deux rotations parallèles du MÊME token : la
    seconde attend le commit de la première, puis voit la session révoquée → détection de
    vol. Deux sessions valides ne peuvent donc jamais naître d'un seul token.

    Refuse via RafraichissementError (message générique) dans tous les cas.
    """
    maintenant = datetime.now(UTC)

    # Les erreurs de jwt.py sont traduites en RafraichissementError : un seul type de refus
    # pour l'appelant. On ne chaîne pas (from None) — la traceback de jwt porterait le token.
    try:
        claims = decoder_refresh_token(refresh_token)
    except JetonExpireError:
        raise RafraichissementError(CauseRefresh.TOKEN_EXPIRE) from None
    except TypeDeJetonInvalideError:
        raise RafraichissementError(CauseRefresh.TYPE_INVALIDE) from None
    except JetonInvalideError:
        raise RafraichissementError(CauseRefresh.TOKEN_INVALIDE) from None

    session = db.execute(
        select(UserSession).where(UserSession.id == claims.jti).with_for_update()
    ).scalar_one_or_none()

    if session is None:
        # Token signé valide mais aucune session : ni preuve de réutilisation (la session
        # a pu être purgée un jour), ni token que l'on reconnaît. Refus simple, sans
        # révocation totale — voir la décision « session introuvable » de 3c.
        raise RafraichissementError(CauseRefresh.SESSION_INTROUVABLE)

    if session.revoked_at is not None:
        _revoquer_toutes_les_sessions(db, session.user_id, maintenant)
        db.commit()  # 3d : audit « vol détecté » juste avant ce commit
        raise RafraichissementError(CauseRefresh.REUTILISATION_DETECTEE, session.user_id)

    # Défense en profondeur : le token a déjà été jugé non expiré par jwt.py, mais la
    # session porte sa propre échéance (un futur outil d'admin pourrait la raccourcir).
    if session.expires_at <= maintenant:
        raise RafraichissementError(CauseRefresh.SESSION_EXPIREE, session.user_id)

    # Authenticité : le jti a trouvé la ligne, le hash prouve que c'est bien CE token.
    # Comparaison à temps constant, par principe (le jti est déjà public dans le token).
    if not hmac.compare_digest(session.refresh_token_hash, _hash_refresh(refresh_token)):
        raise RafraichissementError(CauseRefresh.HASH_INCOHERENT, session.user_id)

    # État du compte re-vérifié à CHAQUE rotation : sans cela, un compte désactivé,
    # verrouillé ou supprimé après sa connexion garderait un accès pendant 8 h, rendant
    # le verrouillage (3b) et la désactivation contournables via le refresh.
    user = db.get(User, session.user_id)
    if user is None or user.deleted_at is not None or not user.is_active:
        raise RafraichissementError(CauseRefresh.COMPTE_INDISPONIBLE, session.user_id)
    if _verrou_actif(user, maintenant):
        raise RafraichissementError(CauseRefresh.COMPTE_INDISPONIBLE, user.id)

    # Rotation. Les rôles sont RELUS en base ici — c'est précisément pourquoi le refresh
    # n'en portait pas (bloc 2) : une habilitation révoquée entre-temps ne survit pas.
    roles = tuple(role.code for role in user.roles)
    nouvel_access = creer_access_token(
        user_id=user.id,
        roles=roles,
        primary_agency_id=user.primary_agency_id,
        agency_id=user.primary_agency_id,
    )
    nouveau_refresh = creer_refresh_token(user_id=user.id)
    nouvelle_session = _creer_session(user.id, nouveau_refresh, ip, user_agent)

    _appliquer_rotation(db, session, nouvelle_session, maintenant)

    return ResultatConnexion(
        user_id=user.id,
        access_token=nouvel_access,
        refresh_token=nouveau_refresh,
        # Sans rapport avec le mot de passe, non évalué ici : toujours False au refresh.
        rehash_recommande=False,
    )
