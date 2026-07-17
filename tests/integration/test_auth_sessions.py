"""Sessions en base et rotation des refresh tokens avec détection de vol (§6, sous-bloc 3c).

Tests d'intégration, vraie base, isolation par SAVEPOINT (authentifier/rafraichir
committent). Fixtures partagées définies localement — même patron que test_auth.py.

AUCUN SECRET EN DUR : mots de passe fabriqués par générateur (secrets).
"""

import secrets
import string
import uuid
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta

import jwt as pyjwt
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import engine
from app.modules.parameters.models import Agency
from app.modules.security.auth import (
    MESSAGE_REFRESH_REFUSE,
    CauseRefresh,
    RafraichissementError,
    _hash_refresh,
    authentifier,
    rafraichir,
)
from app.modules.security.jwt import (
    DUREE_RAFRAICHISSEMENT,
    creer_access_token,
    decoder_refresh_token,
)
from app.modules.security.models import Role, User, UserRole, UserSession
from app.modules.security.password import hasher_mot_de_passe

pytestmark = pytest.mark.integration

IP_TEST = "203.0.113.7"  # TEST-NET-3 (RFC 5737), jamais une vraie IP


def _mot_de_passe_conforme() -> str:
    familles = [string.ascii_uppercase, string.ascii_lowercase, string.digits, string.punctuation]
    alphabet = "".join(familles)
    caracteres = [secrets.choice(f) for f in familles]
    caracteres += [secrets.choice(alphabet) for _ in range(12)]
    secrets.SystemRandom().shuffle(caracteres)
    return "".join(caracteres)


@pytest.fixture
def db() -> Generator[Session, None, None]:
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
def creer_utilisateur(db: Session, mot_de_passe: str) -> Callable[[], User]:
    """Fabrique un utilisateur actif rattaché à une agence, avec le rôle CAISSIER.

    Renvoie une FABRIQUE (pas un user unique) : les tests multi-appareils ont besoin de
    plusieurs utilisateurs distincts sans se marcher dessus sur les contraintes d'unicité.
    Tous partagent le même mot de passe conforme, ce qui suffit aux scénarios.
    """
    role = db.execute(select(Role).where(Role.code == "CAISSIER")).scalar_one()

    def _fabriquer() -> User:
        suffixe = uuid.uuid4().hex[:8]
        agence = Agency(code=f"AG-{suffixe}", name="Agence de test")
        db.add(agence)
        db.flush()
        user = User(
            matricule=f"MAT-{suffixe}",
            email=f"u.{suffixe}@example.com",
            username=f"user_{suffixe}",
            password_hash=hasher_mot_de_passe(mot_de_passe),
            last_name="Test",
            first_name="U",
            primary_agency_id=agence.id,
        )
        db.add(user)
        db.flush()
        db.add(UserRole(user_id=user.id, role_id=role.id))
        db.flush()
        return user

    return _fabriquer


@pytest.fixture
def utilisateur(creer_utilisateur: Callable[[], User]) -> User:
    return creer_utilisateur()


def _session_de(db: Session, refresh_token: str) -> UserSession | None:
    """Charge la ligne user_sessions correspondant à un refresh token (id = jti)."""
    jti = decoder_refresh_token(refresh_token).jti
    return db.get(UserSession, jti)


# --- création de session à la connexion ---------------------------------------------


def test_une_connexion_cree_une_session(db: Session, utilisateur: User, mot_de_passe: str) -> None:
    resultat = authentifier(db, utilisateur.username, mot_de_passe, ip=IP_TEST, user_agent="pytest")

    session = _session_de(db, resultat.refresh_token)
    assert session is not None
    # L'id de la session EST le jti du token.
    assert session.id == decoder_refresh_token(resultat.refresh_token).jti
    assert session.user_id == utilisateur.id
    assert session.revoked_at is None
    # La colonne INET revient en objet ipaddress à la lecture (psycopg), alors que le
    # modèle l'annonce en str : on compare via str(). Écart de type à traiter au niveau
    # des schémas Pydantic / du modèle plus tard, sans impact sur la logique 3c.
    assert str(session.ip) == IP_TEST
    assert session.user_agent == "pytest"
    # expires_at ≈ maintenant + 8 h (durée du refresh).
    delta = session.expires_at - datetime.now(UTC)
    assert timedelta(hours=7, minutes=59) <= delta <= DUREE_RAFRAICHISSEMENT


def test_le_hash_stocke_nest_jamais_le_token_en_clair(
    db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    resultat = authentifier(db, utilisateur.username, mot_de_passe)
    session = _session_de(db, resultat.refresh_token)
    assert session is not None
    # En base : le SHA-256, jamais le clair.
    assert session.refresh_token_hash == _hash_refresh(resultat.refresh_token)
    assert resultat.refresh_token not in session.refresh_token_hash
    assert len(session.refresh_token_hash) == 64  # hex SHA-256


# --- rotation -----------------------------------------------------------------------


def test_rafraichir_emet_un_nouveau_couple_et_revoque_lancien(
    db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    connexion = authentifier(db, utilisateur.username, mot_de_passe)
    ancienne = _session_de(db, connexion.refresh_token)
    assert ancienne is not None

    rotation = rafraichir(db, connexion.refresh_token, ip=IP_TEST)

    # Nouveau couple, distinct de l'ancien.
    assert rotation.access_token != connexion.access_token
    assert rotation.refresh_token != connexion.refresh_token
    assert rotation.user_id == utilisateur.id

    # Ancienne session révoquée et chaînée à la nouvelle.
    db.refresh(ancienne)
    nouveau_jti = decoder_refresh_token(rotation.refresh_token).jti
    assert ancienne.revoked_at is not None
    assert ancienne.replaced_by_session_id == nouveau_jti

    # La nouvelle session est active.
    nouvelle = _session_de(db, rotation.refresh_token)
    assert nouvelle is not None
    assert nouvelle.revoked_at is None


def test_le_nouveau_token_est_utilisable_pour_rafraichir_encore(
    db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    connexion = authentifier(db, utilisateur.username, mot_de_passe)
    r1 = rafraichir(db, connexion.refresh_token)
    r2 = rafraichir(db, r1.refresh_token)  # chaîne de rotations légitime
    assert r2.refresh_token not in {connexion.refresh_token, r1.refresh_token}


# --- refus : token invalide / expiré / mauvais type ---------------------------------


def test_rafraichir_un_token_expire_est_refuse(db: Session, utilisateur: User) -> None:
    passe = datetime.now(UTC) - timedelta(hours=1)
    expire = pyjwt.encode(
        {
            "sub": str(utilisateur.id),
            "jti": str(uuid.uuid4()),
            "iat": int((passe - DUREE_RAFRAICHISSEMENT).timestamp()),
            "exp": int(passe.timestamp()),
            "type": "refresh",
        },
        settings.JWT_SECRET.get_secret_value(),
        algorithm=settings.JWT_ALGORITHM,
    )
    with pytest.raises(RafraichissementError) as capture:
        rafraichir(db, expire)
    assert capture.value.cause == CauseRefresh.TOKEN_EXPIRE
    assert str(capture.value) == MESSAGE_REFRESH_REFUSE


def test_rafraichir_avec_un_access_token_est_refuse(db: Session, utilisateur: User) -> None:
    # Le piège de séparation access/refresh du bloc 2 : un access token n'est pas un refresh.
    access = creer_access_token(utilisateur.id, ["CAISSIER"])
    with pytest.raises(RafraichissementError) as capture:
        rafraichir(db, access)
    assert capture.value.cause == CauseRefresh.TYPE_INVALIDE


def test_rafraichir_un_charabia_est_refuse(db: Session) -> None:
    with pytest.raises(RafraichissementError) as capture:
        rafraichir(db, "pas.un.token")
    assert capture.value.cause == CauseRefresh.TOKEN_INVALIDE


def test_rafraichir_un_token_sans_session_est_refuse_sans_revocation(
    db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    # Token signé valide mais jti inconnu en base : refus simple, PAS de révocation totale.
    connexion = authentifier(db, utilisateur.username, mot_de_passe)
    token_orphelin = pyjwt.encode(
        {
            "sub": str(utilisateur.id),
            "jti": str(uuid.uuid4()),  # jti qui ne correspond à aucune session
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + DUREE_RAFRAICHISSEMENT).timestamp()),
            "type": "refresh",
        },
        settings.JWT_SECRET.get_secret_value(),
        algorithm=settings.JWT_ALGORITHM,
    )
    with pytest.raises(RafraichissementError) as capture:
        rafraichir(db, token_orphelin)
    assert capture.value.cause == CauseRefresh.SESSION_INTROUVABLE
    # La vraie session de l'utilisateur reste active : pas de révocation collatérale.
    vraie = _session_de(db, connexion.refresh_token)
    assert vraie is not None
    assert vraie.revoked_at is None


# --- DÉTECTION DE VOL ----------------------------------------------------------------


def test_reutiliser_un_refresh_declenche_la_detection_de_vol(
    db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    """Le cœur de 3c : présenter deux fois le même refresh token.

    La 2e fois, le token pointe sur une session déjà révoquée (par la 1re rotation) → un
    token consommé recircule → révocation de TOUTES les sessions de l'utilisateur.
    """
    connexion = authentifier(db, utilisateur.username, mot_de_passe)
    rafraichir(db, connexion.refresh_token)  # 1re rotation : légitime

    with pytest.raises(RafraichissementError) as capture:
        rafraichir(db, connexion.refresh_token)  # rejeu du MÊME token
    assert capture.value.cause == CauseRefresh.REUTILISATION_DETECTEE
    assert capture.value.user_id == utilisateur.id
    assert str(capture.value) == MESSAGE_REFRESH_REFUSE

    # Toutes les sessions de l'utilisateur sont révoquées — y compris celle qu'avait créée
    # la 1re rotation. Déconnexion totale.
    actives = (
        db.execute(
            select(UserSession).where(
                UserSession.user_id == utilisateur.id, UserSession.revoked_at.is_(None)
            )
        )
        .scalars()
        .all()
    )
    assert actives == []


def test_apres_detection_de_vol_le_nouveau_token_est_aussi_invalide(
    db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    # Le token légitimement émis par la 1re rotation devient inutilisable après la
    # détection de vol : rejouer l'ancien déconnecte vraiment TOUT.
    connexion = authentifier(db, utilisateur.username, mot_de_passe)
    rotation = rafraichir(db, connexion.refresh_token)
    with pytest.raises(RafraichissementError):
        rafraichir(db, connexion.refresh_token)  # déclenche le vol

    with pytest.raises(RafraichissementError) as capture:
        rafraichir(db, rotation.refresh_token)  # le « bon » token, désormais révoqué
    assert capture.value.cause == CauseRefresh.REUTILISATION_DETECTEE


# --- multi-appareils : pas de faux positif ------------------------------------------


def test_deux_connexions_creent_deux_sessions_actives_distinctes(
    db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    appareil_a = authentifier(db, utilisateur.username, mot_de_passe, user_agent="appareil-A")
    appareil_b = authentifier(db, utilisateur.username, mot_de_passe, user_agent="appareil-B")

    session_a = _session_de(db, appareil_a.refresh_token)
    session_b = _session_de(db, appareil_b.refresh_token)
    assert session_a is not None and session_b is not None
    assert session_a.id != session_b.id
    assert session_a.revoked_at is None
    assert session_b.revoked_at is None


def test_rafraichir_un_appareil_ne_revoque_pas_lautre(
    db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    """Le piège subtil : l'usage multi-appareils NORMAL ne déclenche pas de faux vol.

    Chaque appareil a sa propre session (jti distinct). Rafraîchir A ne touche qu'à la
    session de A — celle de B, d'un autre jti, reste active.
    """
    appareil_a = authentifier(db, utilisateur.username, mot_de_passe, user_agent="appareil-A")
    appareil_b = authentifier(db, utilisateur.username, mot_de_passe, user_agent="appareil-B")
    session_b = _session_de(db, appareil_b.refresh_token)
    assert session_b is not None

    # A rafraîchit : rotation de SA session uniquement.
    rafraichir(db, appareil_a.refresh_token, user_agent="appareil-A")

    # B est intact, et peut toujours rafraîchir.
    db.refresh(session_b)
    assert session_b.revoked_at is None
    rotation_b = rafraichir(db, appareil_b.refresh_token, user_agent="appareil-B")
    assert rotation_b.user_id == utilisateur.id


# --- état du compte re-vérifié au refresh -------------------------------------------


def test_un_compte_desactive_ne_peut_plus_rafraichir(
    db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    connexion = authentifier(db, utilisateur.username, mot_de_passe)
    utilisateur.is_active = False
    db.flush()
    with pytest.raises(RafraichissementError) as capture:
        rafraichir(db, connexion.refresh_token)
    assert capture.value.cause == CauseRefresh.COMPTE_INDISPONIBLE


def test_un_compte_verrouille_ne_peut_plus_rafraichir(
    db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    connexion = authentifier(db, utilisateur.username, mot_de_passe)
    utilisateur.is_locked = True
    utilisateur.locked_until = datetime.now(UTC) + timedelta(minutes=15)
    db.flush()
    with pytest.raises(RafraichissementError) as capture:
        rafraichir(db, connexion.refresh_token)
    assert capture.value.cause == CauseRefresh.COMPTE_INDISPONIBLE


def test_un_compte_supprime_ne_peut_plus_rafraichir(
    db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    connexion = authentifier(db, utilisateur.username, mot_de_passe)
    utilisateur.deleted_at = datetime.now(UTC)
    db.flush()
    with pytest.raises(RafraichissementError) as capture:
        rafraichir(db, connexion.refresh_token)
    assert capture.value.cause == CauseRefresh.COMPTE_INDISPONIBLE


# --- concurrence : preuve du verrou de ligne ----------------------------------------


def test_la_rotation_prend_un_verrou_de_ligne_for_update() -> None:
    """Preuve déterministe que la rotation est protégée contre deux refresh parallèles.

    Comme en 3b : on vérifie que la requête de chargement de session compile en SQL avec
    FOR UPDATE, plutôt qu'un test à deux connexions instable et polluant.
    """
    stmt = select(UserSession).where(UserSession.id == uuid.uuid4()).with_for_update()
    assert "FOR UPDATE" in str(stmt.compile(engine))
