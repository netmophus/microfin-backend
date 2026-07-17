"""Service d'authentification, flux de connexion (§6, sous-bloc 3a). Tests d'intégration.

Vraie base. Comme il n'existe pas encore de service de création d'utilisateur (bloc
ultérieur), la fixture insère directement via l'ORM, en hachant le mot de passe avec
password.py — jamais de password_hash forgé à la main.

ISOLATION : chaque test s'exécute dans une session dont la transaction est TOUJOURS
annulée (rollback). Rien n'est jamais committé — ni l'agence, ni l'utilisateur, ni ses
rôles. Le service authentifier reçoit CETTE MÊME session : il voit donc les lignes
flushées (non committées), et le rollback final nettoie tout. Aucun résidu entre tests,
aucune dépendance à l'ordre d'exécution.

AUCUN MOT DE PASSE EN DUR : le mot de passe de test est fabriqué par un générateur qui
tire ses caractères avec secrets (même règle que test_password.py).
"""

import secrets
import string
import time
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import engine
from app.modules.parameters.models import Agency
from app.modules.security.auth import (
    MESSAGE_ECHEC_GENERIQUE,
    PLAFOND_LOCKOUT_COUNT,
    SEUIL_VERROUILLAGE,
    CauseEchec,
    EchecAuthentificationError,
    _selectionner_pour_maj,
    authentifier,
)
from app.modules.security.jwt import decoder_access_token, decoder_refresh_token
from app.modules.security.models import Role, User, UserRole
from app.modules.security.password import hasher_mot_de_passe

pytestmark = pytest.mark.integration


def _mot_de_passe_conforme() -> str:
    """Mot de passe conforme au §6, aléatoire — jamais un littéral."""
    familles = [string.ascii_uppercase, string.ascii_lowercase, string.digits, string.punctuation]
    alphabet = "".join(familles)
    caracteres = [secrets.choice(f) for f in familles]
    caracteres += [secrets.choice(alphabet) for _ in range(12)]
    secrets.SystemRandom().shuffle(caracteres)
    return "".join(caracteres)


@pytest.fixture
def db() -> Generator[Session, None, None]:
    """Session en isolation par SAVEPOINT.

    authentifier COMMITTE désormais (compteur, verrou) : une session rollback-only ne
    suffirait plus, un commit réel polluerait la base entre tests. Ici, la session est
    liée à une connexion dont la transaction externe est déjà ouverte ; join_transaction_
    mode="create_savepoint" fait que chaque db.commit() du code testé ne relâche qu'un
    savepoint. Le rollback de la transaction externe, au teardown, annule TOUT — y compris
    ce que le code a « committé ». Patron standard pour tester du code qui committe.
    """
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(
        bind=connection,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
    )
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def mot_de_passe() -> str:
    return _mot_de_passe_conforme()


@pytest.fixture
def agence(db: Session) -> Agency:
    # Suffixe aléatoire sur le code : deux exécutions concurrentes ne s'entrechoquent pas
    # sur la contrainte d'unicité, même si aucune n'est committée.
    agence = Agency(code=f"AG-{uuid.uuid4().hex[:8]}", name="Agence de test")
    db.add(agence)
    db.flush()
    return agence


@pytest.fixture
def utilisateur(db: Session, agence: Agency, mot_de_passe: str) -> User:
    """Utilisateur actif, non verrouillé, rattaché à une agence, portant le rôle CAISSIER.

    password_hash produit par password.py — c'est le vrai chemin de hachage, pas un
    hash fabriqué à la main qui pourrait diverger de ce que vérifie le service.
    """
    suffixe = uuid.uuid4().hex[:8]
    user = User(
        matricule=f"MAT-{suffixe}",
        email=f"Alice.{suffixe}@Example.COM",  # casse mixte : sert le test CITEXT
        username=f"alice_{suffixe}",
        password_hash=hasher_mot_de_passe(mot_de_passe),
        last_name="Test",
        first_name="Alice",
        primary_agency_id=agence.id,
    )
    db.add(user)
    db.flush()

    role = db.execute(select(Role).where(Role.code == "CAISSIER")).scalar_one()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.flush()
    return user


# --- succès -------------------------------------------------------------------------


def test_connexion_reussie_par_username(
    db: Session, utilisateur: User, mot_de_passe: str, agence: Agency
) -> None:
    resultat = authentifier(db, utilisateur.username, mot_de_passe)

    claims = decoder_access_token(resultat.access_token)
    assert claims.sub == utilisateur.id
    assert claims.roles == ("CAISSIER",)
    # 3a : l'agence courante du jeton EST l'agence de rattachement.
    assert claims.agency_id == agence.id
    assert claims.primary_agency_id == agence.id

    refresh = decoder_refresh_token(resultat.refresh_token)
    assert refresh.sub == utilisateur.id
    assert resultat.user_id == utilisateur.id


def test_connexion_reussie_par_email_insensible_a_la_casse(
    db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    """Prouve le CITEXT : l'email est stocké en casse mixte, on se connecte en minuscules."""
    email_saisi = utilisateur.email.lower()
    assert email_saisi != utilisateur.email  # la casse diffère réellement

    resultat = authentifier(db, email_saisi, mot_de_passe)
    assert decoder_access_token(resultat.access_token).sub == utilisateur.id


def test_un_access_et_un_refresh_sont_emis(
    db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    resultat = authentifier(db, utilisateur.username, mot_de_passe)
    # jti distincts : deux jetons de la même connexion ne sont pas le même objet.
    assert (
        decoder_access_token(resultat.access_token).jti
        != decoder_refresh_token(resultat.refresh_token).jti
    )


def test_le_signal_de_rehachage_est_expose(
    db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    # Hash produit avec les paramètres courants du §6 : aucun re-hachage attendu.
    resultat = authentifier(db, utilisateur.username, mot_de_passe)
    assert resultat.rehash_recommande is False


# --- échecs : message générique identique -------------------------------------------


def test_mauvais_mot_de_passe_echoue(db: Session, utilisateur: User) -> None:
    with pytest.raises(EchecAuthentificationError) as capture:
        authentifier(db, utilisateur.username, _mot_de_passe_conforme())
    assert capture.value.cause == CauseEchec.MOT_DE_PASSE_INVALIDE
    assert str(capture.value) == MESSAGE_ECHEC_GENERIQUE


def test_compte_inexistant_echoue(db: Session, mot_de_passe: str) -> None:
    with pytest.raises(EchecAuthentificationError) as capture:
        authentifier(db, f"fantome_{uuid.uuid4().hex[:8]}", mot_de_passe)
    assert capture.value.cause == CauseEchec.COMPTE_INEXISTANT
    assert capture.value.user_id is None  # personne à désigner
    assert str(capture.value) == MESSAGE_ECHEC_GENERIQUE


def test_compte_desactive_echoue(db: Session, utilisateur: User, mot_de_passe: str) -> None:
    utilisateur.is_active = False
    db.flush()
    # Mot de passe CORRECT : c'est bien l'état du compte qui bloque, pas les identifiants.
    with pytest.raises(EchecAuthentificationError) as capture:
        authentifier(db, utilisateur.username, mot_de_passe)
    assert capture.value.cause == CauseEchec.COMPTE_DESACTIVE
    assert str(capture.value) == MESSAGE_ECHEC_GENERIQUE


def test_compte_verrouille_par_locked_until_echoue(
    db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    utilisateur.locked_until = datetime.now(UTC) + timedelta(minutes=15)
    db.flush()
    with pytest.raises(EchecAuthentificationError) as capture:
        authentifier(db, utilisateur.username, mot_de_passe)
    assert capture.value.cause == CauseEchec.COMPTE_VERROUILLE
    assert str(capture.value) == MESSAGE_ECHEC_GENERIQUE


def test_compte_verrouille_par_is_locked_echoue(
    db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    utilisateur.is_locked = True
    db.flush()
    with pytest.raises(EchecAuthentificationError) as capture:
        authentifier(db, utilisateur.username, mot_de_passe)
    assert capture.value.cause == CauseEchec.COMPTE_VERROUILLE


def test_un_verrou_echu_ne_bloque_plus(db: Session, utilisateur: User, mot_de_passe: str) -> None:
    # locked_until dans le passé : la fenêtre est refermée, la connexion doit passer.
    utilisateur.locked_until = datetime.now(UTC) - timedelta(minutes=1)
    db.flush()
    resultat = authentifier(db, utilisateur.username, mot_de_passe)
    assert resultat.user_id == utilisateur.id


def test_un_compte_supprime_est_traite_comme_inexistant(
    db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    utilisateur.deleted_at = datetime.now(UTC)
    db.flush()
    with pytest.raises(EchecAuthentificationError) as capture:
        authentifier(db, utilisateur.username, mot_de_passe)
    # Inexistant, PAS désactivé : le service ne doit pas révéler que le compte a existé.
    assert capture.value.cause == CauseEchec.COMPTE_INEXISTANT
    assert capture.value.user_id is None


def test_les_quatre_echecs_donnent_le_meme_message(
    db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    """Le cœur de l'anti-fuite : dehors, les quatre causes sont un seul et même message."""
    messages: set[str] = set()

    # 1. mot de passe faux
    with pytest.raises(EchecAuthentificationError) as c1:
        authentifier(db, utilisateur.username, _mot_de_passe_conforme())
    messages.add(str(c1.value))

    # 2. compte inexistant
    with pytest.raises(EchecAuthentificationError) as c2:
        authentifier(db, f"fantome_{uuid.uuid4().hex[:8]}", mot_de_passe)
    messages.add(str(c2.value))

    # 3. compte désactivé
    utilisateur.is_active = False
    db.flush()
    with pytest.raises(EchecAuthentificationError) as c3:
        authentifier(db, utilisateur.username, mot_de_passe)
    messages.add(str(c3.value))

    # 4. compte verrouillé
    utilisateur.is_active = True
    utilisateur.is_locked = True
    db.flush()
    with pytest.raises(EchecAuthentificationError) as c4:
        authentifier(db, utilisateur.username, mot_de_passe)
    messages.add(str(c4.value))

    assert messages == {MESSAGE_ECHEC_GENERIQUE}


# --- anti-énumération par le timing -------------------------------------------------


def _mediane(mesures: list[float]) -> float:
    mesures.sort()
    return mesures[len(mesures) // 2]


def test_le_compte_inexistant_a_un_timing_comparable_a_un_compte_existant(
    db: Session, utilisateur: User
) -> None:
    """Un compte absent doit coûter le même Argon2 qu'un compte présent au mot de passe faux.

    Sinon le temps de réponse (~0 ms contre ~22 ms) trahirait l'existence du compte. On
    compare un ratio plutôt qu'un seuil absolu, pour rester robuste à la vitesse machine :
    un court-circuit sans Argon2 donnerait un ratio proche de zéro. Le compteur est remis
    à zéro entre les itérations pour que chacune mesure le chemin d'écriture complet
    (UPDATE + COMMIT) sans jamais déclencher le verrou.
    """

    def mesurer_existant() -> float:
        mesures: list[float] = []
        for _ in range(5):
            utilisateur.failed_attempts = 0
            db.flush()
            debut = time.perf_counter()
            with pytest.raises(EchecAuthentificationError):
                authentifier(db, utilisateur.username, _mot_de_passe_conforme())
            mesures.append(time.perf_counter() - debut)
        return _mediane(mesures)

    def mesurer_inexistant() -> float:
        mesures: list[float] = []
        for _ in range(5):
            debut = time.perf_counter()
            with pytest.raises(EchecAuthentificationError):
                authentifier(db, f"fantome_{uuid.uuid4().hex[:8]}", _mot_de_passe_conforme())
            mesures.append(time.perf_counter() - debut)
        return _mediane(mesures)

    temps_existant = mesurer_existant()
    temps_inexistant = mesurer_inexistant()

    # Le compte absent ne doit pas répondre nettement plus vite : preuve que HASH_LEURRE
    # a bien fait travailler Argon2. Le chemin existant fait EN PLUS un UPDATE + COMMIT
    # (~quelques ms), donc temps_inexistant est un peu plus court — mais du même ordre.
    assert temps_inexistant >= temps_existant * 0.5


def test_un_compte_verrouille_execute_quand_meme_argon2(
    db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    """Le piège de timing signalé dans authentifier : un compte bloqué ne doit pas
    court-circuiter Argon2, sinon sa vitesse de réponse le désignerait comme existant.

    Comparaison à un mot de passe faux sur compte actif (qui exécute forcément Argon2) :
    les deux doivent être du même ordre de grandeur.
    """

    def mesurer_verrouille() -> float:
        mesures: list[float] = []
        for _ in range(5):
            # Verrou frais à chaque itération : la tentative est rejetée sans écriture.
            utilisateur.is_locked = True
            utilisateur.locked_until = datetime.now(UTC) + timedelta(minutes=15)
            db.flush()
            debut = time.perf_counter()
            with pytest.raises(EchecAuthentificationError):
                authentifier(db, utilisateur.username, mot_de_passe)
            mesures.append(time.perf_counter() - debut)
        return _mediane(mesures)

    def mesurer_reference() -> float:
        mesures: list[float] = []
        for _ in range(5):
            # Compte actif, non verrouillé, compteur remis à zéro : mot de passe faux →
            # Argon2 + écriture, sans jamais atteindre le seuil de verrouillage.
            utilisateur.is_active = True
            utilisateur.is_locked = False
            utilisateur.locked_until = None
            utilisateur.failed_attempts = 0
            db.flush()
            debut = time.perf_counter()
            with pytest.raises(EchecAuthentificationError):
                authentifier(db, utilisateur.username, _mot_de_passe_conforme())
            mesures.append(time.perf_counter() - debut)
        return _mediane(mesures)

    assert mesurer_verrouille() >= mesurer_reference() * 0.5


# --- non-exposition -----------------------------------------------------------------


def test_aucune_exception_ne_contient_le_hash(db: Session, utilisateur: User) -> None:
    hash_stocke = utilisateur.password_hash
    with pytest.raises(EchecAuthentificationError) as capture:
        authentifier(db, utilisateur.username, _mot_de_passe_conforme())
    assert hash_stocke not in str(capture.value)
    assert hash_stocke not in repr(capture.value)


def test_la_cause_nest_pas_dans_le_repr_de_lexception(db: Session, utilisateur: User) -> None:
    # cause et user_id sont des attributs hors de args : le repr par défaut ne les montre
    # pas, donc un log qui imprimerait l'exception ne fuiterait pas la raison.
    with pytest.raises(EchecAuthentificationError) as capture:
        authentifier(db, utilisateur.username, _mot_de_passe_conforme())
    assert "mot_de_passe" not in repr(capture.value)


# --- verrouillage progressif C7 (3b) ------------------------------------------------


def _echouer(db: Session, user: User, fois: int) -> None:
    """Enchaîne `fois` tentatives à mot de passe faux (chacune committe son incrément)."""
    for _ in range(fois):
        with pytest.raises(EchecAuthentificationError):
            authentifier(db, user.username, _mot_de_passe_conforme())


def test_quatre_echecs_ne_verrouillent_pas(db: Session, utilisateur: User) -> None:
    _echouer(db, utilisateur, 4)
    assert utilisateur.failed_attempts == 4
    assert utilisateur.is_locked is False
    assert utilisateur.locked_until is None


def test_le_compteur_est_persiste_entre_les_tentatives(db: Session, utilisateur: User) -> None:
    # Preuve que l'incrément est bien committé et non perdu au raise : on recharge la
    # ligne depuis la base (populate_existing force la relecture, pas l'objet en mémoire).
    _echouer(db, utilisateur, 3)
    recharge = db.execute(
        select(User).where(User.id == utilisateur.id).execution_options(populate_existing=True)
    ).scalar_one()
    assert recharge.failed_attempts == 3


def test_le_cinquieme_echec_pose_un_verrou_de_quinze_minutes(
    db: Session, utilisateur: User
) -> None:
    _echouer(db, utilisateur, SEUIL_VERROUILLAGE)
    assert utilisateur.is_locked is True
    assert utilisateur.lockout_count == 1
    assert utilisateur.last_lockout_at is not None
    # locked_until ≈ now + 15 min (un peu moins : du temps s'est écoulé depuis la pose).
    assert utilisateur.locked_until is not None
    delta = utilisateur.locked_until - datetime.now(UTC)
    assert timedelta(minutes=14) <= delta <= timedelta(minutes=15)
    # Les échecs sont consommés dans le verrou.
    assert utilisateur.failed_attempts == 0


def test_une_connexion_reussie_remet_le_compteur_a_zero(
    db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    _echouer(db, utilisateur, 3)
    assert utilisateur.failed_attempts == 3
    authentifier(db, utilisateur.username, mot_de_passe)
    assert utilisateur.failed_attempts == 0
    assert utilisateur.is_locked is False


def test_le_deuxieme_cycle_de_verrouillage_dure_trente_minutes(
    db: Session, utilisateur: User
) -> None:
    # 1er verrou : 15 min, lockout_count = 1.
    _echouer(db, utilisateur, SEUIL_VERROUILLAGE)
    assert utilisateur.lockout_count == 1

    # On simule l'expiration de la fenêtre (locked_until dans le passé), en gardant
    # last_lockout_at récent : l'escalade ne doit PAS être réinitialisée.
    utilisateur.locked_until = datetime.now(UTC) - timedelta(minutes=1)
    db.flush()

    # 2e cycle : 5 échecs de plus → verrou de 30 min, lockout_count = 2.
    _echouer(db, utilisateur, SEUIL_VERROUILLAGE)
    assert utilisateur.lockout_count == 2
    assert utilisateur.locked_until is not None
    delta = utilisateur.locked_until - datetime.now(UTC)
    assert timedelta(minutes=29) <= delta <= timedelta(minutes=30)


def test_lockout_count_est_reinitialise_apres_vingt_quatre_heures(
    db: Session, utilisateur: User
) -> None:
    _echouer(db, utilisateur, SEUIL_VERROUILLAGE)
    assert utilisateur.lockout_count == 1

    # On simule 25 h écoulées depuis le dernier verrou, fenêtre expirée : la progression
    # doit repartir de 15 min et lockout_count de 1.
    utilisateur.last_lockout_at = datetime.now(UTC) - timedelta(hours=25)
    utilisateur.locked_until = datetime.now(UTC) - timedelta(hours=25)
    db.flush()

    _echouer(db, utilisateur, SEUIL_VERROUILLAGE)
    assert utilisateur.lockout_count == 1  # reparti de zéro, +1
    assert utilisateur.locked_until is not None
    delta = utilisateur.locked_until - datetime.now(UTC)
    assert timedelta(minutes=14) <= delta <= timedelta(minutes=15)


def test_une_tentative_sur_compte_verrouille_ne_prolonge_pas_le_verrou(
    db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    _echouer(db, utilisateur, SEUIL_VERROUILLAGE)
    verrou_initial = utilisateur.locked_until
    compteur_initial = utilisateur.lockout_count

    # Plusieurs tentatives PENDANT le verrou (mot de passe correct ou non) : aucune ne
    # doit prolonger locked_until ni réincrémenter lockout_count.
    for _ in range(3):
        with pytest.raises(EchecAuthentificationError) as capture:
            authentifier(db, utilisateur.username, mot_de_passe)
        assert capture.value.cause == CauseEchec.COMPTE_VERROUILLE

    assert utilisateur.locked_until == verrou_initial
    assert utilisateur.lockout_count == compteur_initial
    assert utilisateur.failed_attempts == 0


def test_un_verrou_expire_repart_sur_un_compteur_neuf(db: Session, utilisateur: User) -> None:
    # Le cas limite vicieux : après expiration du verrou, un seul mauvais mot de passe ne
    # doit PAS re-verrouiller aussitôt — le compte repart sur SEUIL tentatives.
    _echouer(db, utilisateur, SEUIL_VERROUILLAGE)
    utilisateur.locked_until = datetime.now(UTC) - timedelta(minutes=1)
    db.flush()

    _echouer(db, utilisateur, 1)  # une seule tentative après expiration
    assert utilisateur.is_locked is False
    assert utilisateur.failed_attempts == 1  # compteur neuf, pas 6


def test_la_duree_de_verrou_plafonne_a_cent_vingt_minutes(db: Session, utilisateur: User) -> None:
    # Au-delà du palier max, la durée ne double plus : elle reste à 120 min.
    utilisateur.lockout_count = PLAFOND_LOCKOUT_COUNT
    utilisateur.last_lockout_at = datetime.now(UTC)
    db.flush()

    _echouer(db, utilisateur, SEUIL_VERROUILLAGE)
    assert utilisateur.locked_until is not None
    delta = utilisateur.locked_until - datetime.now(UTC)
    assert timedelta(minutes=119) <= delta <= timedelta(minutes=120)


# --- concurrence : preuve du verrou de ligne ----------------------------------------


def test_le_chargement_prend_un_verrou_de_ligne_for_update() -> None:
    """Preuve déterministe que la lecture-modification du compteur est protégée.

    Un vrai test à deux connexions serait instable (ordonnancement des threads) et
    polluerait la base (la ligne de test n'est pas committée, donc invisible d'une autre
    connexion sous l'isolation par savepoint). On vérifie plutôt que la requête compile
    en SQL avec FOR UPDATE : le mécanisme de sérialisation est en place.
    """
    # Compilé avec le dialecte du vrai engine PostgreSQL : le rendu FOR UPDATE est
    # celui qui partira en base.
    sql = str(_selectionner_pour_maj("peu-importe").compile(engine))
    assert "FOR UPDATE" in sql
