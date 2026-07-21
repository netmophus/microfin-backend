"""GET /agencies — premier morceau du module Paramétrage.

Alimente le sélecteur d'agences du formulaire de création. Authentifié suffit ; la structure
d'agences n'est pas confidentielle, et la vraie protection reste sur POST /users.
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.parameters.models import Agency
from app.modules.security.jwt import creer_access_token
from app.modules.security.models import Role, User, UserRole
from app.modules.security.password import hasher_mot_de_passe

pytestmark = pytest.mark.integration


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
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _entete_utilisateur(db: Session) -> dict[str, str]:
    """Un jeton d'un compte quelconque (CAISSIER) : authentifié, sans permission spéciale."""
    role = db.execute(select(Role).where(Role.code == "CAISSIER")).scalar_one()
    suffixe = uuid.uuid4().hex[:8]
    user = User(
        matricule=f"MAT-{suffixe}",
        email=f"{suffixe}@example.com",
        username=f"u{suffixe}",
        password_hash=hasher_mot_de_passe("Motdepasse!123"),
        last_name="Test",
        first_name="U",
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.flush()
    jeton = creer_access_token(user_id=user.id, roles=["CAISSIER"])
    return {"Authorization": f"Bearer {jeton}"}


def _agence(db: Session, nom: str, active: bool = True) -> Agency:
    agence = Agency(code=f"AG-{uuid.uuid4().hex[:6]}", name=nom, is_active=active)
    db.add(agence)
    db.flush()
    return agence


def test_liste_les_agences_pour_un_compte_authentifie(client: TestClient, db: Session) -> None:
    """Authentifié suffit — même un CAISSIER, sans permission du périmètre Sécurité."""
    agence = _agence(db, "Agence de Niamey")
    entete = _entete_utilisateur(db)

    reponse = client.get("/agencies", headers=entete)

    assert reponse.status_code == 200
    codes = {item["code"] for item in reponse.json()}
    assert agence.code in codes


def test_exige_une_authentification(client: TestClient) -> None:
    assert client.get("/agencies").status_code == 401


def test_n_expose_que_id_code_nom(client: TestClient, db: Session) -> None:
    """Sortie explicite : pas de created_by, is_active, ni autre champ interne."""
    _agence(db, "Agence de test")
    entete = _entete_utilisateur(db)

    corps = client.get("/agencies", headers=entete).json()

    assert corps, "au moins une agence attendue"
    assert set(corps[0]) == {"id", "code", "name"}


def test_les_agences_inactives_sont_exclues(client: TestClient, db: Session) -> None:
    active = _agence(db, "Active")
    inactive = _agence(db, "Fermée", active=False)
    entete = _entete_utilisateur(db)

    codes = {item["code"] for item in client.get("/agencies", headers=entete).json()}

    assert active.code in codes
    assert inactive.code not in codes
