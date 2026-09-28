"""API paramètres d'intérêt des produits d'épargne (écran de paramétrage, Bloc 5 comptable).

  - lecture/écriture : même paire de permissions que les rattachements (compta.plan.read /
    compta.plan.manage), motif obligatoire, tracé avant/après ;
  - bornes reproduisant les CHECK constraints SQL (migration 0023) et le précédent
    `credit.taux_provision_bp` (taux 0-10000 bp) — revérifiées côté serveur, pas seulement
    proposées par un sélecteur d'écran ;
  - GARDE-FOU is_provisional : régler le taux ne lève PAS le provisoire.
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


def _agence_id(db: Session) -> uuid.UUID:
    return db.execute(text("SELECT id FROM parameters.agencies LIMIT 1")).scalar_one()


def _entete_auth(db: Session, role_code: str) -> dict[str, str]:
    role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
    agence_id = _agence_id(db)
    s = uuid.uuid4().hex[:8]
    user = User(
        matricule=f"MAT-{s}", email=f"{s}@ex.com", username=f"u{s}",
        password_hash=hasher_mot_de_passe("Motdepasse!123"), last_name="T", first_name="A",
        primary_agency_id=agence_id,
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.flush()
    jeton = creer_access_token(
        user_id=user.id, roles=[role_code], primary_agency_id=agence_id, agency_id=agence_id
    )
    return {"Authorization": f"Bearer {jeton}"}


def _produit(db: Session, **overrides: object) -> Product:
    valeurs = {
        "code": f"PT{uuid.uuid4().hex[:6]}",
        "name": "Produit de test",
        "type": "a_vue",
        **overrides,
    }
    produit = Product(**valeurs)
    db.add(produit)
    db.flush()
    return produit


CORPS_VALIDE = {
    "taux_bp": 350,
    "methode_calcul_solde": "fin_periode",
    "base_jours": 360,
    "regle_arrondi": "plus_proche",
    "solde_minimum_remunere": 0,
    "motif": "Fixation du taux 2026 par le comité",
}


# --- Lecture ---------------------------------------------------------------------------


def test_lecture_expose_les_parametres(client: TestClient, db: Session) -> None:
    _produit(db, code="PT-LEC-INT", taux_bp=200)
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.get("/epargne/produits/parametres-interet", headers=comptable)

    assert reponse.status_code == 200
    ligne = next(p for p in reponse.json() if p["code"] == "PT-LEC-INT")
    assert ligne["taux_bp"] == 200
    assert ligne["is_provisional"] is True
    assert "periodicite" not in ligne


def test_lecture_sans_permission_403(client: TestClient, db: Session) -> None:
    caissier = _entete_auth(db, "CAISSIER")
    reponse = client.get("/epargne/produits/parametres-interet", headers=caissier)
    assert reponse.status_code == 403


# --- Écriture : succès, motif, audit ------------------------------------------------------


def test_modification_reussie_avec_motif_trace(client: TestClient, db: Session) -> None:
    produit = _produit(db, code="PT-MOD-INT", taux_bp=0)
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.patch(
        f"/epargne/produits/{produit.id}/parametres-interet",
        json=CORPS_VALIDE,
        headers=comptable,
    )

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["taux_bp"] == 350
    assert corps["methode_calcul_solde"] == "fin_periode"

    ligne = db.execute(
        text(
            "SELECT old_values, new_values FROM audit.audit_logs "
            "WHERE action = 'epargne.product.interets_updated' AND resource_id = :r"
        ),
        {"r": produit.id},
    ).one()
    assert ligne.old_values["taux_bp"] == 0
    assert ligne.new_values["taux_bp"] == 350
    assert ligne.new_values["motif"] == CORPS_VALIDE["motif"]


def test_regler_le_taux_ne_leve_pas_le_provisoire(client: TestClient, db: Session) -> None:
    produit = _produit(db, code="PT-PROV", taux_bp=0)
    assert produit.is_provisional is True
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.patch(
        f"/epargne/produits/{produit.id}/parametres-interet",
        json=CORPS_VALIDE,
        headers=comptable,
    )

    assert reponse.status_code == 200
    assert reponse.json()["is_provisional"] is True
    db.refresh(produit)
    assert produit.is_provisional is True


def test_modification_motif_absent_refusee(client: TestClient, db: Session) -> None:
    produit = _produit(db, code="PT-NOMOTIF-INT")
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.patch(
        f"/epargne/produits/{produit.id}/parametres-interet",
        json={**CORPS_VALIDE, "motif": ""},
        headers=comptable,
    )
    assert reponse.status_code == 422


def test_produit_introuvable_404(client: TestClient, db: Session) -> None:
    comptable = _entete_auth(db, "COMPTABLE")
    reponse = client.patch(
        f"/epargne/produits/{uuid.uuid4()}/parametres-interet",
        json=CORPS_VALIDE,
        headers=comptable,
    )
    assert reponse.status_code == 404


def test_modification_sans_permission_403(client: TestClient, db: Session) -> None:
    produit = _produit(db, code="PT-403-INT")
    caissier = _entete_auth(db, "CAISSIER")
    reponse = client.patch(
        f"/epargne/produits/{produit.id}/parametres-interet",
        json=CORPS_VALIDE,
        headers=caissier,
    )
    assert reponse.status_code == 403


# --- Bornes : revérifiées côté serveur, pas seulement proposées à l'écran ------------------


@pytest.mark.parametrize(
    "champ,valeur",
    [
        ("taux_bp", -1),
        ("taux_bp", 10001),
        ("methode_calcul_solde", "moyenne_mensuelle"),
        ("base_jours", 365.5),
        ("base_jours", 366),
        ("regle_arrondi", "superieur"),
        ("solde_minimum_remunere", -1),
    ],
)
def test_valeur_hors_borne_refusee(
    client: TestClient, db: Session, champ: str, valeur: object
) -> None:
    produit = _produit(db, code=f"PT-BORNE-{champ}"[:20])
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.patch(
        f"/epargne/produits/{produit.id}/parametres-interet",
        json={**CORPS_VALIDE, champ: valeur},
        headers=comptable,
    )
    assert reponse.status_code == 422
