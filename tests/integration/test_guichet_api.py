"""API Guichet (F2a) — dépôt / retrait, et surtout les REFUS parlants.

  - recherche par numéro : renvoie le NOM du membre (vérification humaine contre une faute de
    frappe dans le numéro) ;
  - dépôt : crédite ; retrait > solde -> 422 avec « disponible » ; membre suspendu -> 422 (gel) ;
    compte fermé -> 422 ; montant <= 0 -> 422 ; sans permission -> 403 ; hors agence -> 404.
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.epargne import service
from app.modules.epargne.models import Product, SavingsAccount
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


def _cid(db: Session, numero: str) -> uuid.UUID:
    return db.execute(
        text("SELECT id FROM comptabilite.accounts WHERE account_number = :n"), {"n": numero}
    ).scalar_one()


def _agence(db: Session, code: str) -> Agency:
    agence = Agency(code=code, name=f"Agence {code}", compte_caisse_id=_cid(db, "5721"))
    db.add(agence)
    db.flush()
    return agence


def _membre_nomme(db: Session, agence: Agency, nom: str, prenom: str) -> uuid.UUID:
    tier_id = db.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES (:n, 'individual', :a, 'actif') RETURNING id"
        ),
        {"n": f"M-G-{uuid.uuid4().hex[:6]}", "a": agence.id},
    ).scalar_one()
    nat = db.execute(text("SELECT id FROM parameters.countries LIMIT 1")).scalar_one()
    db.execute(
        text(
            "INSERT INTO tiers.individual_profiles "
            "(tier_id, last_name, first_name, birth_date, gender, nationality_id) "
            "VALUES (:t, :n, :p, '1990-01-01', 'F', :nat)"
        ),
        {"t": tier_id, "n": nom, "p": prenom, "nat": nat},
    )
    return tier_id


def _compte(db: Session, agence: Agency) -> SavingsAccount:
    produit = Product(
        code=f"PG{uuid.uuid4().hex[:4]}", name="Épargne à vue", type="a_vue",
        compte_epargne_id=_cid(db, "3111"),
    )
    db.add(produit)
    db.flush()
    tier_id = _membre_nomme(db, agence, "Traoré", "Fatoumata")
    return service.ouvrir_compte(
        db, tier_id=tier_id, product_id=produit.id, agency_id=agence.id, par=None
    )


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


def test_recherche_par_numero_renvoie_le_nom_du_membre(client: TestClient, db: Session) -> None:
    agence = _agence(db, "AG-G1")
    compte = _compte(db, agence)
    entete = _entete(db, agence, "CAISSIER")

    reponse = client.get(
        "/epargne/recherche-compte", params={"numero": compte.account_number}, headers=entete
    )
    assert reponse.status_code == 200
    # Nom proéminent = vérification humaine contre une faute de frappe dans le numéro.
    assert reponse.json()["membre_nom"] == "Traoré Fatoumata"


def test_depot_puis_retrait(client: TestClient, db: Session) -> None:
    agence = _agence(db, "AG-G2")
    compte = _compte(db, agence)
    entete = _entete(db, agence, "CAISSIER")

    depot = client.post(
        f"/epargne/comptes/{compte.id}/depot", json={"montant": 10000}, headers=entete
    )
    assert depot.status_code == 200
    assert depot.json()["nouveau_solde"] == 10000

    retrait = client.post(
        f"/epargne/comptes/{compte.id}/retrait", json={"montant": 3000}, headers=entete
    )
    assert retrait.json()["nouveau_solde"] == 7000


def test_retrait_solde_insuffisant_422_avec_disponible(client: TestClient, db: Session) -> None:
    agence = _agence(db, "AG-G3")
    compte = _compte(db, agence)
    entete = _entete(db, agence, "CAISSIER")
    client.post(f"/epargne/comptes/{compte.id}/depot", json={"montant": 1000}, headers=entete)

    reponse = client.post(
        f"/epargne/comptes/{compte.id}/retrait", json={"montant": 5000}, headers=entete
    )
    assert reponse.status_code == 422
    assert "disponible" in reponse.json()["detail"].lower()


def test_operation_membre_suspendu_422(client: TestClient, db: Session) -> None:
    agence = _agence(db, "AG-G4")
    compte = _compte(db, agence)
    db.execute(
        text("UPDATE tiers.tiers SET status = 'suspendu_temporaire' WHERE id = :t"),
        {"t": compte.tier_id},
    )
    entete = _entete(db, agence, "CAISSIER")

    reponse = client.post(
        f"/epargne/comptes/{compte.id}/depot", json={"montant": 1000}, headers=entete
    )
    assert reponse.status_code == 422
    assert "gel" in reponse.json()["detail"].lower()


def test_operation_compte_ferme_422(client: TestClient, db: Session) -> None:
    agence = _agence(db, "AG-G5")
    compte = _compte(db, agence)
    db.execute(
        text("UPDATE epargne.accounts SET status = 'cloture' WHERE id = :a"), {"a": compte.id}
    )
    entete = _entete(db, agence, "CAISSIER")

    reponse = client.post(
        f"/epargne/comptes/{compte.id}/depot", json={"montant": 1000}, headers=entete
    )
    assert reponse.status_code == 422


def test_montant_invalide_422(client: TestClient, db: Session) -> None:
    agence = _agence(db, "AG-G6")
    compte = _compte(db, agence)
    entete = _entete(db, agence, "CAISSIER")

    reponse = client.post(
        f"/epargne/comptes/{compte.id}/depot", json={"montant": 0}, headers=entete
    )
    assert reponse.status_code == 422


def test_depot_sans_permission_403(client: TestClient, db: Session) -> None:
    agence = _agence(db, "AG-G7")
    compte = _compte(db, agence)
    # AUDITEUR_INTERNE lit mais n'opère pas au guichet.
    entete = _entete(db, agence, "AUDITEUR_INTERNE")

    reponse = client.post(
        f"/epargne/comptes/{compte.id}/depot", json={"montant": 1000}, headers=entete
    )
    assert reponse.status_code == 403


def test_compte_hors_agence_404(client: TestClient, db: Session) -> None:
    agence_a = _agence(db, "AG-GA")
    agence_b = _agence(db, "AG-GB")
    compte = _compte(db, agence_a)
    entete_b = _entete(db, agence_b, "CAISSIER")

    reponse = client.post(
        f"/epargne/comptes/{compte.id}/depot", json={"montant": 1000}, headers=entete_b
    )
    assert reponse.status_code == 404
