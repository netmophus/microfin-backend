"""API Épargne F1 — consultation + ouverture, avec cloisonnement et gate KYC.

  - ouverture réservée (epargne.account.open) et SEULEMENT sur un membre actif (prospect -> 422) ;
  - un rôle sans la permission -> 403 ;
  - cloisonnement : un acteur d'une autre agence ne voit pas le membre (404) ;
  - le compte ouvert apparaît dans la liste, avec son solde en francs.
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.epargne.models import Product
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


def _agence(db: Session, code: str) -> Agency:
    agence = Agency(code=code, name=f"Agence {code}")
    db.add(agence)
    db.flush()
    return agence


def _produit(db: Session) -> Product:
    produit = Product(
        code=f"PA{uuid.uuid4().hex[:4]}", name="Épargne à vue", type="a_vue",
        compte_epargne_id=db.execute(
            text("SELECT id FROM comptabilite.accounts WHERE account_number='251111'")
        ).scalar_one(),
    )
    db.add(produit)
    db.flush()
    return produit


def _membre(db: Session, agence: Agency, statut: str) -> uuid.UUID:
    return db.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES (:n, 'individual', :a, :s) RETURNING id"
        ),
        {"n": f"M-API-{uuid.uuid4().hex[:6]}", "a": agence.id, "s": statut},
    ).scalar_one()


def _entete(db: Session, agence: Agency, role_code: str) -> dict[str, str]:
    role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
    suffixe = uuid.uuid4().hex[:8]
    user = User(
        matricule=f"MAT-{suffixe}", email=f"{suffixe}@ex.com", username=f"u{suffixe}",
        password_hash=hasher_mot_de_passe("Motdepasse!123"), last_name="T", first_name="A",
        primary_agency_id=agence.id,
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.flush()
    jeton = creer_access_token(
        user_id=user.id, roles=[role_code], primary_agency_id=agence.id, agency_id=agence.id
    )
    return {"Authorization": f"Bearer {jeton}"}


def test_ouvrir_un_compte_sur_membre_actif_puis_le_lister(
    client: TestClient, db: Session
) -> None:
    agence = _agence(db, "AG-EP1")
    produit = _produit(db)
    membre = _membre(db, agence, "actif")
    entete = _entete(db, agence, "CHARGE_CLIENTELE")

    creation = client.post(
        f"/tiers/{membre}/comptes-epargne", json={"product_id": str(produit.id)}, headers=entete
    )
    assert creation.status_code == 201
    corps = creation.json()
    assert corps["balance"] == 0
    assert corps["status"] == "actif"
    assert corps["account_number"].startswith("EP-")

    liste = client.get(f"/tiers/{membre}/comptes-epargne", headers=entete)
    assert liste.status_code == 200
    assert len(liste.json()) == 1
    assert liste.json()[0]["is_provisional"] is True  # produit provisoire, visible


def test_ouvrir_sur_prospect_refuse_422_message(client: TestClient, db: Session) -> None:
    agence = _agence(db, "AG-EP2")
    produit = _produit(db)
    prospect = _membre(db, agence, "prospect")
    entete = _entete(db, agence, "CHARGE_CLIENTELE")

    reponse = client.post(
        f"/tiers/{prospect}/comptes-epargne", json={"product_id": str(produit.id)}, headers=entete
    )
    assert reponse.status_code == 422
    assert "actif" in reponse.json()["detail"].lower()  # message qui dit POURQUOI


def test_ouvrir_sans_permission_403(client: TestClient, db: Session) -> None:
    agence = _agence(db, "AG-EP3")
    produit = _produit(db)
    membre = _membre(db, agence, "actif")
    # MEMBRE_COMITE_CREDIT n'a aucune permission épargne.
    entete = _entete(db, agence, "MEMBRE_COMITE_CREDIT")

    reponse = client.post(
        f"/tiers/{membre}/comptes-epargne", json={"product_id": str(produit.id)}, headers=entete
    )
    assert reponse.status_code == 403


def test_membre_hors_agence_est_introuvable_404(client: TestClient, db: Session) -> None:
    agence_a = _agence(db, "AG-EPA")
    agence_b = _agence(db, "AG-EPB")
    produit = _produit(db)
    membre_a = _membre(db, agence_a, "actif")
    # Chargé cloisonné à l'agence B tente d'ouvrir pour un membre de l'agence A.
    entete_b = _entete(db, agence_b, "CHARGE_CLIENTELE")

    reponse = client.post(
        f"/tiers/{membre_a}/comptes-epargne", json={"product_id": str(produit.id)}, headers=entete_b
    )
    assert reponse.status_code == 404


def test_lister_les_produits(client: TestClient, db: Session) -> None:
    agence = _agence(db, "AG-EP4")
    entete = _entete(db, agence, "CHARGE_CLIENTELE")
    reponse = client.get("/epargne/produits", headers=entete)
    assert reponse.status_code == 200
    assert any(p["code"] == "EAV" for p in reponse.json())
