"""Service d'authentification — connexion, verrouillage, sessions, audit (§6). Sous-blocs 3a→3d.

Périmètre : identifiant + mot de passe → jetons (3a) ; compteur d'échecs et verrouillage
progressif C7 (3b) ; sessions en base et rotation des refresh tokens avec détection de vol
(3c) ; audit transactionnel C5 et choix d'agence courante multi-agences C6 (3d).

Hors périmètre, volontairement absent :
  - Bloc 4 : les endpoints API. Ici on fait la LOGIQUE, pas l'API.
  - La purge des sessions révoquées (job de nettoyage différé) : à écrire plus tard.

AUDIT (C5). Les événements SIGNIFICATIFS sont écrits dans audit.audit_logs, dans la MÊME
transaction que l'écriture métier (pas de trace, pas d'opération). Sont audités : la
connexion réussie, la pose d'un verrou (le 5e échec, pas chaque échec), la détection de
vol de jeton, le refus de refresh pour compte devenu indisponible, et la tentative de
connexion sur une agence non autorisée. NE SONT PAS audités : chaque échec de mot de passe
isolé (résumé par failed_attempts et par l'événement de verrouillage), les tentatives sur
compte inexistant/désactivé (flooding d'un journal indélébile 5 ans + anti-énumération),
ni le rafraîchissement RÉUSSI (bruit routinier). C5 vise les opérations métier, pas chaque
sollicitation réseau.

L'audit s'insère juste avant chaque commit, après les FOR UPDATE déjà tenus : l'ordre des
verrous reste constant — ligne user/session puis verrou consultatif du chaînage d'audit —
donc pas de deadlock avec le trigger de chaînage. L'insertion passe par du SQL paramétré,
jamais par l'ORM (le modèle AuditLog lève à l'insertion, par construction).

SECRETS À NE JAMAIS LAISSER SORTIR :

  1. password_hash et le refresh token EN CLAIR ne quittent jamais ce module. En base, le
     refresh n'existe que sous forme de hash SHA-256 ; aucun secret ne figure dans l'audit
     (old_values / new_values ne portent que ce qu'on y met explicitement).
  2. La RAISON d'un refus ne sort jamais non plus. À l'extérieur, toutes les causes donnent
     le MÊME message générique — au login pour ne pas révéler l'existence/l'état d'un
     compte, au refresh pour ne pas dire à un attaquant « vol détecté ». En interne, la
     cause est distinguée (hors de args) pour le compteur (3b) et l'audit (3d).

PIÈGES, détaillés à leur emplacement :
  - l'ordre des contrôles au login est un oracle de timing (voir authentifier) ;
  - le commit du compteur doit précéder le raise, sinon un échec perd l'incrément ;
  - la rotation d'un refresh est atomique sous FOR UPDATE (voir rafraichir) ;
  - l'audit s'écrit en SQL paramétré, jamais via l'ORM gardé (voir _ecrire_audit).
"""

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import Select, select, update
from sqlalchemy.orm import Session, lazyload

from app.modules.audit.service import ContexteRequete, ecrire_audit
from app.modules.security.jwt import (
    JetonError,
    JetonExpireError,
    JetonInvalideError,
    TypeDeJetonInvalideError,
    creer_access_token,
    creer_refresh_token,
    decoder_refresh_token,
)
from app.modules.security.models import User, UserAgency, UserSession
from app.modules.security.password import (
    HASH_LEURRE,
    rehachage_necessaire,
    verifier_mot_de_passe,
)

# Message unique renvoyé à l'extérieur pour TOUT échec. Un seul littéral, partagé, pour
# qu'aucune divergence ne se glisse entre les cas. Ne jamais y ajouter la cause.
MESSAGE_ECHEC_GENERIQUE = "Identifiant ou mot de passe incorrect."


# --- audit (C5, 3d) ----------------------------------------------------------------


class ActionAudit(StrEnum):
    """Les cinq événements d'auth jugés significatifs (§6). Format module.action.

    Ne PAS ajouter ici l'échec de mot de passe isolé ni la tentative sur compte inconnu :
    ils rempliraient un journal indélébile de 5 ans et rouvriraient l'écart de timing.
    Ni le rafraîchissement réussi : bruit routinier (un client rafraîchit toutes les 15 min).
    """

    LOGIN_SUCCESS = "auth.login.success"
    ACCOUNT_LOCKED = "auth.account.locked"
    TOKEN_REUSE_DETECTED = "auth.token.reuse_detected"
    REFRESH_DENIED_ACCOUNT_UNAVAILABLE = "auth.token.refresh_denied_account_unavailable"
    LOGIN_AGENCY_DENIED = "auth.login.agency_denied"


# ContexteRequete et CONTEXTE_VIDE vivent désormais dans app.modules.audit.service : ils
# servent à TOUS les modules qui auditent, pas seulement à l'authentification. Les
# appelants (router.py, tests) les importent de là, pas d'ici — un module ne doit pas
# devenir le point d'entrée d'un type qui ne lui appartient plus.


def _ecrire_audit(
    db: Session,
    *,
    action: ActionAudit,
    contexte: ContexteRequete,
    user_id: uuid.UUID | None,
    agency_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Adaptateur vers le service d'audit partagé, pour les événements d'AUTHENTIFICATION.

    Ici l'acteur EST le sujet : c'est le titulaire du compte qui se connecte, échoue ou se
    fait voler un jeton. resource_id reste donc vide — il n'y a pas de « cible » distincte
    de l'auteur. Les écritures administratives (4c), elles, appellent ecrire_audit
    directement pour renseigner les deux.

    À appeler le plus tard possible dans la transaction (verrou consultatif du chaînage).
    """
    ecrire_audit(
        db,
        action=action.value,
        contexte=contexte,
        acteur_id=user_id,
        agency_id=agency_id,
        new_values=details,
    )


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
    # Mot de passe correct mais agence courante demandée non habilitée (C6). Refus
    # d'autorisation, pas d'authentification : le compteur d'échecs n'est pas touché.
    AGENCE_NON_AUTORISEE = "agence_non_autorisee"


class EchecAuthentificationError(Exception):
    """Échec de connexion. Message générique dehors, cause distincte dedans.

    Le message (args[0]) est toujours MESSAGE_ECHEC_GENERIQUE : c'est lui, et lui seul,
    que la couche API renverra. cause et user_id sont des attributs hors de args, donc
    absents du repr par défaut — ils servent le compteur (3b) et l'audit (3d), jamais la
    réponse HTTP. user_id est None quand le compte est introuvable.

    verrou_jusqua : renseigné UNIQUEMENT sur COMPTE_VERROUILLE ET quand le mot de passe
    fourni était correct — donc quand le demandeur est le vrai titulaire. C'est le seul
    canal par lequel l'API peut révéler le verrou (et son échéance) à qui connaît le mot
    de passe, sans jamais l'apprendre à un attaquant qui l'ignore. Voir authentifier().
    """

    def __init__(
        self,
        cause: CauseEchec,
        user_id: uuid.UUID | None = None,
        verrou_jusqua: datetime | None = None,
    ) -> None:
        super().__init__(MESSAGE_ECHEC_GENERIQUE)
        self.cause = cause
        self.user_id = user_id
        self.verrou_jusqua = verrou_jusqua


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
    # §6 — le jeton émis n'ouvre rien tant que c'est vrai (cf. exige()). Exposé ici pour
    # que l'API le dise au client, qui doit alors présenter l'écran de changement.
    doit_changer_mot_de_passe: bool = False


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


def revoquer_les_sessions(db: Session, user_id: uuid.UUID) -> int:
    """Révoque TOUTES les sessions d'un utilisateur, sur ordre d'un ADMINISTRATEUR (4c).

    Distincte de deconnecter_tout, qui part d'un refresh token : là, c'est le titulaire qui
    se déconnecte lui-même. Ici, un tiers agit sur un compte dont il ne détient aucun jeton
    — désactivation, suppression, réinitialisation de mot de passe.

    C'est cette fonction qui referme la fenêtre laissée ouverte par la brique
    d'autorisation : celle-ci ne relit pas l'état du compte, si bien qu'un compte désactivé
    garderait 8 h d'accès par son refresh token. Désactiver sans révoquer ne désactive rien
    d'utile.

    Ne committe pas : l'appelant tient la transaction, pour que l'audit y entre aussi.
    Rend le nombre de sessions effectivement révoquées (0 si le compte n'en avait aucune).
    """
    # Les identifiants sont relus AVANT la mise à jour : Result.rowcount n'est pas typé
    # (ni garanti) sur un UPDATE ORM, et compter les lignes visées est plus honnête que
    # d'interroger le pilote sur ce qu'il a touché.
    identifiants = list(
        db.execute(
            select(UserSession.id).where(
                UserSession.user_id == user_id, UserSession.revoked_at.is_(None)
            )
        ).scalars()
    )
    if not identifiants:
        return 0
    db.execute(
        update(UserSession)
        .where(UserSession.id.in_(identifiants))
        .values(revoked_at=datetime.now(UTC))
        .execution_options(synchronize_session="fetch")
    )
    return len(identifiants)


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


def _enregistrer_echec(
    db: Session, user: User, maintenant: datetime, contexte: ContexteRequete
) -> None:
    """Compte l'échec, pose le verrou au seuil, puis COMMITTE — avant que l'appelant lève.

    Le commit précède le raise à dessein : un échec de connexion ne doit pas empêcher la
    persistance du compteur, sinon le verrouillage ne servirait à rien.

    AUDIT (C5) : SEULE la pose d'un verrou est auditée (l'événement significatif), pas
    chaque échec — sinon un pilonnage remplirait le journal indélébile. L'audit s'insère
    juste avant le commit ; l'ordre des verrous reste constant (ligne user FOR UPDATE puis
    verrou consultatif du chaînage), donc pas de deadlock avec le trigger de chaînage.
    """
    # Un verrou temporisé échu laisse is_locked à true : on le nettoie avant de compter,
    # pour repartir sur une série fraîche (failed_attempts a été remis à 0 à la pose).
    if user.is_locked and not _verrou_actif(user, maintenant):
        user.is_locked = False
        user.locked_until = None

    tentatives = user.failed_attempts + 1
    verrou_pose = tentatives >= SEUIL_VERROUILLAGE
    if verrou_pose:
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

    if verrou_pose:
        _ecrire_audit(
            db,
            action=ActionAudit.ACCOUNT_LOCKED,
            contexte=contexte,
            user_id=user.id,
            details={"lockout_count": user.lockout_count},
        )

    db.commit()


def _enregistrer_succes(
    db: Session,
    user: User,
    session: UserSession,
    maintenant: datetime,
    contexte: ContexteRequete,
    agency_id: uuid.UUID | None,
    roles: tuple[str, ...],
) -> None:
    """Remet le compteur à zéro, PERSISTE la nouvelle session, AUDITE, puis COMMITTE.

    Remise à zéro du compteur, création de session (3c) et audit de la connexion réussie
    (3d) sont dans la MÊME transaction : la connexion, sa session et sa trace naissent ou
    échouent ensemble (C5).

    L'audit (connexion réussie) est écrit juste avant le commit. roles y figure — ce n'est
    pas un secret — mais jamais le password_hash.
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
    _ecrire_audit(
        db,
        action=ActionAudit.LOGIN_SUCCESS,
        contexte=contexte,
        user_id=user.id,
        agency_id=agency_id,
        details={"roles": list(roles)},
    )
    db.commit()


def _resoudre_agence(
    db: Session,
    user: User,
    agence_demandee: uuid.UUID | None,
    maintenant: datetime,
    contexte: ContexteRequete,
) -> uuid.UUID | None:
    """Résout l'agence courante de la session (C6). Refuse et audite si non habilitée.

    - Rien de demandé → l'agence de rattachement (primary_agency_id), cas du mono-agent.
    - Demandée et habilitée (== agence de rattachement OU ligne dans user_agencies) → elle.
    - Demandée et NON habilitée → audit puis refus. Jamais de repli silencieux sur l'agence
      principale : ça masquerait une tentative d'accès à un périmètre non autorisé.

    L'audit + commit précèdent le raise, pour que la trace survive au refus (C5).
    """
    if agence_demandee is None:
        return user.primary_agency_id

    habilitee = (
        agence_demandee == user.primary_agency_id
        or db.execute(
            select(UserAgency.agency_id).where(
                UserAgency.user_id == user.id,
                UserAgency.agency_id == agence_demandee,
            )
        ).first()
        is not None
    )

    if habilitee:
        return agence_demandee

    _ecrire_audit(
        db,
        action=ActionAudit.LOGIN_AGENCY_DENIED,
        contexte=contexte,
        user_id=user.id,
        details={"agence_demandee": str(agence_demandee)},
    )
    db.commit()
    raise EchecAuthentificationError(CauseEchec.AGENCE_NON_AUTORISEE, user.id)


def authentifier(
    db: Session,
    identifiant: str,
    mot_de_passe: str,
    *,
    agence_demandee: uuid.UUID | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    request_id: uuid.UUID | None = None,
) -> ResultatConnexion:
    """Authentifie par (username OU email) + mot de passe. Émet access + refresh.

    identifiant : username (sensible à la casse, VARCHAR) ou email (insensible, CITEXT).
    Une seule requête couvre les deux ; la casse de l'email est gérée par le type CITEXT.

    agence_demandee (C6) : agence courante voulue pour la session. Omise → l'agence de
    rattachement (primary_agency_id). Fournie, elle doit être habilitée (== l'agence de
    rattachement OU présente dans user_agencies) ; sinon la connexion est REFUSÉE et
    auditée, jamais rabattue en silence sur l'agence principale.

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
    contexte = ContexteRequete(ip=ip, user_agent=user_agent, request_id=request_id)
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
        # locked_until n'est joint QUE si le mot de passe est bon (déjà calculé, aucun
        # Argon2 en plus, aucun changement d'ordre) : l'API ne révélera le verrou qu'au
        # vrai titulaire. Pour un mot de passe faux, verrou_jusqua reste None → 401 générique.
        verrou_jusqua = user.locked_until if mot_de_passe_ok else None
        raise EchecAuthentificationError(
            CauseEchec.COMPTE_VERROUILLE, user.id, verrou_jusqua=verrou_jusqua
        )
    if not mot_de_passe_ok:
        _enregistrer_echec(db, user, maintenant, contexte)  # committe AVANT de lever
        raise EchecAuthentificationError(CauseEchec.MOT_DE_PASSE_INVALIDE, user.id)

    # Le mot de passe est bon : l'identité est prouvée. Reste l'AUTORISATION d'agence (C6).
    # Un refus ici n'est pas un échec d'authentification, donc le compteur d'échecs n'est
    # pas touché ; mais la tentative sur une agence non habilitée est auditée.
    agency_id = _resoudre_agence(db, user, agence_demandee, maintenant, contexte)

    # Succès : émission des jetons, création de la session, remise à zéro du compteur,
    # audit — le tout dans une seule transaction (cf. _enregistrer_succes). roles vient de
    # la relation viewonly User.roles.
    roles = tuple(role.code for role in user.roles)
    access_token = creer_access_token(
        user_id=user.id,
        roles=roles,
        primary_agency_id=user.primary_agency_id,
        agency_id=agency_id,
        must_change_password=user.must_change_password,
    )
    refresh_token = creer_refresh_token(user_id=user.id)
    session = _creer_session(user.id, refresh_token, ip, user_agent)

    _enregistrer_succes(db, user, session, maintenant, contexte, agency_id, roles)

    return ResultatConnexion(
        user_id=user.id,
        doit_changer_mot_de_passe=user.must_change_password,
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
    request_id: uuid.UUID | None = None,
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
    contexte = ContexteRequete(ip=ip, user_agent=user_agent, request_id=request_id)

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
        _ecrire_audit(
            db,
            action=ActionAudit.TOKEN_REUSE_DETECTED,
            contexte=contexte,
            user_id=session.user_id,
            details={"session_reutilisee": str(session.id)},
        )
        db.commit()
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
    # le verrouillage (3b) et la désactivation contournables via le refresh. Ce refus est
    # audité (événement significatif) puis committé avant le raise, pour que la trace tienne.
    user = db.get(User, session.user_id)
    compte_indisponible = user is None or user.deleted_at is not None or not user.is_active
    if not compte_indisponible:
        assert user is not None  # pour mypy : compte_indisponible garantit user non None
        compte_indisponible = _verrou_actif(user, maintenant)
    if compte_indisponible:
        _ecrire_audit(
            db,
            action=ActionAudit.REFRESH_DENIED_ACCOUNT_UNAVAILABLE,
            contexte=contexte,
            user_id=session.user_id,
            details={"cause": CauseRefresh.COMPTE_INDISPONIBLE.value},
        )
        db.commit()
        raise RafraichissementError(CauseRefresh.COMPTE_INDISPONIBLE, session.user_id)
    assert user is not None  # les cas None sont couverts par compte_indisponible ci-dessus

    # Rotation. Les rôles sont RELUS en base ici — c'est précisément pourquoi le refresh
    # n'en portait pas (bloc 2) : une habilitation révoquée entre-temps ne survit pas.
    roles = tuple(role.code for role in user.roles)
    nouvel_access = creer_access_token(
        user_id=user.id,
        roles=roles,
        primary_agency_id=user.primary_agency_id,
        agency_id=user.primary_agency_id,
        # RELU en base à chaque rotation, comme les rôles : un mot de passe réinitialisé
        # entre-temps doit refermer l'accès, pas attendre la prochaine connexion.
        must_change_password=user.must_change_password,
    )
    nouveau_refresh = creer_refresh_token(user_id=user.id)
    nouvelle_session = _creer_session(user.id, nouveau_refresh, ip, user_agent)

    _appliquer_rotation(db, session, nouvelle_session, maintenant)

    return ResultatConnexion(
        user_id=user.id,
        doit_changer_mot_de_passe=user.must_change_password,
        access_token=nouvel_access,
        refresh_token=nouveau_refresh,
        # Sans rapport avec le mot de passe, non évalué ici : toujours False au refresh.
        rehash_recommande=False,
    )


# --- déconnexion (bloc 4) ----------------------------------------------------------


def _revoquer_session_courante(db: Session, refresh_token: str, maintenant: datetime) -> None:
    """Révoque la SEULE session portée par le refresh token présenté (le jti = l'id).

    Best-effort et idempotent : un token illisible, expiré, sans session, ou déjà révoqué
    ne fait rien. La déconnexion est une intention bénigne — on ne lève jamais et on ne
    révèle rien (pas de détection de vol ici : rejouer son propre token pour se déconnecter
    est légitime).
    """
    try:
        claims = decoder_refresh_token(refresh_token)
    except JetonError:
        return

    session = db.execute(
        select(UserSession).where(UserSession.id == claims.jti).with_for_update()
    ).scalar_one_or_none()
    if session is None or session.revoked_at is not None:
        return

    session.revoked_at = maintenant
    db.commit()


def deconnecter(db: Session, refresh_token: str) -> None:
    """Déconnexion simple : révoque la session courante. Idempotent, ne lève jamais."""
    _revoquer_session_courante(db, refresh_token, datetime.now(UTC))


def deconnecter_tout(db: Session, refresh_token: str) -> None:
    """Déconnexion totale : révoque TOUTES les sessions de l'utilisateur (tous appareils).

    Exige que le token présenté corresponde à une session ENCORE ACTIVE : un token dont la
    session est révoquée ou absente ne déclenche rien (idempotent, et un token périmé ne
    doit pas pouvoir déconnecter un utilisateur à distance). Ne lève jamais.
    """
    maintenant = datetime.now(UTC)
    try:
        claims = decoder_refresh_token(refresh_token)
    except JetonError:
        return

    session = db.execute(
        select(UserSession).where(UserSession.id == claims.jti).with_for_update()
    ).scalar_one_or_none()
    if session is None or session.revoked_at is not None:
        return

    _revoquer_toutes_les_sessions(db, session.user_id, maintenant)
    db.commit()
