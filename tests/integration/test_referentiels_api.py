"""GET /countries et GET /currencies — référentiels pour les formulaires tiers.

Alimentent les sélecteurs de nationalité (obligatoire pour une personne physique) et de devise
du capital (personne morale). Authentifié suffit ; la vraie protection reste sur POST /tiers.
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.security.jwt import creer_access_token
from app.modules.security.models import Role, User, UserRole
from app.modules.security.password import hasher_mot_de_passe

pytestmark = pytest.mark.integration


@pytest.fixture
def db() -> Generator[Session, None, None]:
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
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


def _entete(db: Session) -> dict[str, str]:
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
    return {"Authorization": f"Bearer {creer_access_token(user_id=user.id, roles=['CAISSIER'])}"}


def test_liste_les_pays_uemoa_en_tete(client: TestClient, db: Session) -> None:
    corps = client.get("/countries", headers=_entete(db)).json()

    codes = [item["code"] for item in corps]
    assert "SN" in codes  # Sénégal (seed T0)
    # Les 8 UEMOA (display_order 10-80) précèdent le reste.
    assert set(codes[:8]) == {"BJ", "BF", "CI", "GW", "ML", "NE", "SN", "TG"}
    assert set(corps[0]) == {"id", "code", "name"}


def test_liste_les_devises(client: TestClient, db: Session) -> None:
    corps = client.get("/currencies", headers=_entete(db)).json()

    devises = {item["code"]: item for item in corps}
    assert "XOF" in devises
    assert devises["XOF"]["decimal_places"] == 0  # le franc CFA n'a pas de décimale
    assert set(corps[0]) == {"id", "code", "name", "decimal_places"}


def test_les_referentiels_exigent_une_authentification(client: TestClient) -> None:
    assert client.get("/countries").status_code == 401
    assert client.get("/currencies").status_code == 401
