"""Endpoints HTTP d'authentification (bloc 4) — tests d'intégration via TestClient.

Le endpoint et le test partagent une session en isolation SAVEPOINT, injectée par override
de la dépendance get_db : le service committe (compteur, session, audit), l'override empêche
que ça touche la base pour de vrai, et le test inspecte la même session.

Transport hybride : l'access token est dans le CORPS, le refresh dans un cookie httpOnly.
Le TestClient (httpx) tient un pot à cookies, donc le refresh est renvoyé automatiquement à
/auth/refresh et /auth/logout, comme un vrai navigateur.

AUCUN SECRET EN DUR : mots de passe fabriqués par générateur (secrets).
"""

import secrets
import string
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.parameters.models import Agency
from app.modules.security.auth import _hash_refresh
from app.modules.security.jwt import creer_access_token, decoder_refresh_token
from app.modules.security.models import Role, User, UserRole, UserSession
from app.modules.security.password import hasher_mot_de_passe

pytestmark = pytest.mark.integration

COOKIE = "refresh_token"
# Domaine que Starlette TestClient attribue aux cookies (base_url http://testserver).
# Nécessaire pour poser un cookie sur le client qui écrase celui du serveur au lieu d'en
# créer un doublon (le jar httpx distingue les cookies par nom + domaine + chemin).
DOMAINE_TEST = "testserver.local"


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
def client(db: Session) -> Generator[TestClient, None, None]:
    """TestClient dont le get_db est branché sur la session savepoint du test."""
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def mot_de_passe() -> str:
    return _mot_de_passe_conforme()


@pytest.fixture
def utilisateur(db: Session, mot_de_passe: str) -> User:
    role = db.execute(select(Role).where(Role.code == "CAISSIER")).scalar_one()
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


def _login(client: TestClient, identifiant: str, mot_de_passe: str):  # type: ignore[no-untyped-def]
    return client.post(
        "/auth/login", json={"identifiant": identifiant, "mot_de_passe": mot_de_passe}
    )


# --- login ---------------------------------------------------------------------------


def test_login_bons_identifiants_renvoie_200_et_les_jetons(
    client: TestClient, utilisateur: User, mot_de_passe: str
) -> None:
    reponse = _login(client, utilisateur.username, mot_de_passe)

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["token_type"] == "bearer"
    assert corps["expires_in"] == 900
    assert corps["access_token"]
    # Le refresh N'EST PAS dans le corps : il est dans le cookie httpOnly.
    assert "refresh_token" not in corps
    assert COOKIE in client.cookies
    entete_cookie = reponse.headers["set-cookie"].lower()
    assert "httponly" in entete_cookie
    assert "samesite=strict" in entete_cookie
    assert "path=/auth" in entete_cookie


def test_login_par_email_insensible_a_la_casse(
    client: TestClient, utilisateur: User, mot_de_passe: str
) -> None:
    reponse = _login(client, utilisateur.email.upper(), mot_de_passe)
    assert reponse.status_code == 200


def test_login_mauvais_mot_de_passe_renvoie_401_generique(
    client: TestClient, utilisateur: User
) -> None:
    reponse = _login(client, utilisateur.username, _mot_de_passe_conforme())
    assert reponse.status_code == 401
    assert reponse.json()["detail"] == "Identifiant ou mot de passe incorrect."
    assert COOKIE not in client.cookies


def test_login_compte_inexistant_meme_message(
    client: TestClient, utilisateur: User, mot_de_passe: str
) -> None:
    mauvais = _login(client, utilisateur.username, _mot_de_passe_conforme())
    inexistant = _login(client, f"fantome_{uuid.uuid4().hex[:8]}", mot_de_passe)
    assert inexistant.status_code == 401
    # Message strictement identique : la réponse ne dit pas si le compte existe.
    assert inexistant.json()["detail"] == mauvais.json()["detail"]


def test_login_champ_manquant_renvoie_422(client: TestClient) -> None:
    reponse = client.post("/auth/login", json={"identifiant": "x"})  # mot_de_passe manquant
    assert reponse.status_code == 422


# --- verrouillage : révélé au seul titulaire -----------------------------------------


def test_login_compte_verrouille_bon_mot_de_passe_renvoie_423(
    client: TestClient, db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    """Le patron décidé : verrou + mot de passe CORRECT → 423 avec l'échéance."""
    jusqua = datetime.now(UTC) + timedelta(minutes=15)
    utilisateur.is_locked = True
    utilisateur.locked_until = jusqua
    db.flush()

    reponse = _login(client, utilisateur.username, mot_de_passe)
    assert reponse.status_code == 423
    detail = reponse.json()["detail"]
    assert "verrou_jusqua" in detail  # l'échéance est communiquée au titulaire


def test_login_compte_verrouille_mauvais_mot_de_passe_reste_401(
    client: TestClient, db: Session, utilisateur: User
) -> None:
    """Verrou + mot de passe FAUX → 401 générique : un attaquant n'apprend rien."""
    utilisateur.is_locked = True
    utilisateur.locked_until = datetime.now(UTC) + timedelta(minutes=15)
    db.flush()

    reponse = _login(client, utilisateur.username, _mot_de_passe_conforme())
    assert reponse.status_code == 401
    assert reponse.json()["detail"] == "Identifiant ou mot de passe incorrect."


# --- refresh -------------------------------------------------------------------------


def test_refresh_valide_renvoie_un_nouveau_couple(
    client: TestClient, utilisateur: User, mot_de_passe: str
) -> None:
    connexion = _login(client, utilisateur.username, mot_de_passe)
    ancien_access = connexion.json()["access_token"]
    ancien_cookie = client.cookies[COOKIE]

    reponse = client.post("/auth/refresh")
    assert reponse.status_code == 200
    assert reponse.json()["access_token"] != ancien_access
    # Le cookie de refresh a tourné aussi.
    assert client.cookies[COOKIE] != ancien_cookie


def test_refresh_sans_cookie_renvoie_401(client: TestClient) -> None:
    reponse = client.post("/auth/refresh")
    assert reponse.status_code == 401


def test_refresh_avec_un_access_token_renvoie_401(client: TestClient, utilisateur: User) -> None:
    # Un access token présenté comme refresh : le service rejette (type invalide) → 401.
    access = creer_access_token(utilisateur.id, ["CAISSIER"])
    client.cookies.set(COOKIE, access, domain=DOMAINE_TEST, path="/auth")
    reponse = client.post("/auth/refresh")
    assert reponse.status_code == 401


def test_reutilisation_dun_refresh_consomme_renvoie_401_et_revoque_tout(
    client: TestClient, db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    _login(client, utilisateur.username, mot_de_passe)
    ancien_cookie = client.cookies[COOKIE]

    client.post("/auth/refresh")  # 1re rotation : l'ancien cookie est consommé

    # Rejeu de l'ancien refresh → détection de vol. On vide le jar (retire le cookie tourné)
    # puis on repose l'ancien, sinon deux cookies homonymes coexisteraient.
    client.cookies.clear()
    client.cookies.set(COOKIE, ancien_cookie, domain=DOMAINE_TEST, path="/auth")
    reponse = client.post("/auth/refresh")
    assert reponse.status_code == 401

    # Toutes les sessions de l'utilisateur sont révoquées (déconnexion totale).
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


# --- logout --------------------------------------------------------------------------


def test_logout_renvoie_204_et_revoque_la_session(
    client: TestClient, db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    _login(client, utilisateur.username, mot_de_passe)
    jti = decoder_refresh_token(client.cookies[COOKIE]).jti

    reponse = client.post("/auth/logout")
    assert reponse.status_code == 204

    session = db.get(UserSession, jti)
    assert session is not None
    assert session.revoked_at is not None


def test_logout_sans_cookie_reste_204(client: TestClient) -> None:
    # Idempotent : rien à révoquer, mais on ne renvoie jamais d'erreur.
    reponse = client.post("/auth/logout")
    assert reponse.status_code == 204


def test_logout_all_revoque_toutes_les_sessions(
    client: TestClient, db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    # Deux connexions (deux appareils), puis logout-all.
    _login(client, utilisateur.username, mot_de_passe)
    autre = TestClient(app)
    autre.post(
        "/auth/login", json={"identifiant": utilisateur.username, "mot_de_passe": mot_de_passe}
    )

    client.post("/auth/logout-all")

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


# --- non-exposition ------------------------------------------------------------------


def test_aucune_reponse_ne_contient_de_hash(
    client: TestClient, utilisateur: User, mot_de_passe: str
) -> None:
    """Ni password_hash ni refresh_token_hash ne doivent apparaître dans une réponse."""
    hash_mdp = utilisateur.password_hash
    connexion = _login(client, utilisateur.username, mot_de_passe)
    # Le hash SHA-256 du refresh, tel que stocké en base.
    hash_refresh = _hash_refresh(client.cookies[COOKIE])

    empreinte = connexion.text + str(dict(connexion.headers))
    assert hash_mdp not in empreinte
    assert hash_refresh not in empreinte
