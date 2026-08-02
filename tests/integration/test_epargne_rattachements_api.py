"""API rattachements comptables des produits d'épargne (Bloc 5 du paramétrage comptable).

  - lecture : les 3 comptes résolus en numéro+libellé, jamais un UUID ;
  - écriture : motif obligatoire, tracé avant/après ; vider un rattachement est LÉGITIME ;
  - GARDE-FOU DOUBLE : le sélecteur ne montre que des comptes de saisie actifs (testé à part,
    test_comptabilite_selecteur_rattachement.py) ET compte_saisie_actif REFUSE ENCORE si un
    numéro de compte de regroupement ou désactivé est soumis DIRECTEMENT à l'API, en
    contournant le sélecteur — c'est ce deuxième filet qu'on prouve ici, sur le vrai endpoint
    HTTP, pas seulement sur la fonction en isolation.
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.comptabilite.models import Account
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


def _compte(db: Session, numero: str, **overrides: object) -> Account:
    valeurs = {
        "account_number": numero,
        "name": f"Compte {numero}",
        "account_class": int(numero[0]),
        "normal_side": "D",
        "is_posting": True,
        "is_system": False,
        **overrides,
    }
    compte = Account(**valeurs)
    db.add(compte)
    db.flush()
    return compte


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


# --- Lecture ---------------------------------------------------------------------------


def test_lecture_resout_les_comptes_en_numero_jamais_uuid(client: TestClient, db: Session) -> None:
    epargne = _compte(db, "6T9E1", normal_side="C")
    _produit(db, code="PT-LEC", compte_epargne_id=epargne.id)
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.get("/epargne/produits/rattachements", headers=comptable)

    assert reponse.status_code == 200
    ligne = next(p for p in reponse.json() if p["code"] == "PT-LEC")
    assert ligne["compte_epargne"] == {"account_number": "6T9E1", "name": "Compte 6T9E1"}
    assert ligne["compte_epargne_client"] is None


def test_lecture_sans_permission_403(client: TestClient, db: Session) -> None:
    caissier = _entete_auth(db, "CAISSIER")
    reponse = client.get("/epargne/produits/rattachements", headers=caissier)
    assert reponse.status_code == 403


# --- Écriture : succès, motif, vider un rattachement -------------------------------------


def test_modification_reussie_avec_motif_trace(client: TestClient, db: Session) -> None:
    ancien = _compte(db, "6T9E2", normal_side="C")
    _compte(db, "6T9E3", normal_side="C")
    produit = _produit(db, code="PT-MOD", compte_epargne_id=ancien.id)
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.patch(
        f"/epargne/produits/{produit.id}/rattachements",
        json={
            "compte_epargne": "6T9E3",
            "compte_epargne_client": None,
            "compte_charge_interet": None,
            "motif": "Correction du rattachement épargne",
        },
        headers=comptable,
    )

    assert reponse.status_code == 200
    assert reponse.json()["compte_epargne"]["account_number"] == "6T9E3"

    ligne = db.execute(
        text(
            "SELECT old_values, new_values FROM audit.audit_logs "
            "WHERE action = 'epargne.product.comptes_updated' AND resource_id = :r"
        ),
        {"r": produit.id},
    ).one()
    assert ligne.old_values["compte_epargne"] == "6T9E2"
    assert ligne.new_values["compte_epargne"] == "6T9E3"
    assert ligne.new_values["motif"] == "Correction du rattachement épargne"


def test_vider_un_rattachement_est_legitime(client: TestClient, db: Session) -> None:
    compte = _compte(db, "6T9E4", normal_side="C")
    produit = _produit(db, code="PT-VIDE", compte_epargne_client_id=compte.id)
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.patch(
        f"/epargne/produits/{produit.id}/rattachements",
        json={
            "compte_epargne": None,
            "compte_epargne_client": None,
            "compte_charge_interet": None,
            "motif": "Retrait du rattachement client, pas encore décidé",
        },
        headers=comptable,
    )

    assert reponse.status_code == 200
    assert reponse.json()["compte_epargne_client"] is None


def test_modification_motif_absent_refusee(client: TestClient, db: Session) -> None:
    produit = _produit(db, code="PT-NOMOTIF")
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.patch(
        f"/epargne/produits/{produit.id}/rattachements",
        json={
            "compte_epargne": None,
            "compte_epargne_client": None,
            "compte_charge_interet": None,
            "motif": "",
        },
        headers=comptable,
    )
    assert reponse.status_code == 422


def test_produit_introuvable_404(client: TestClient, db: Session) -> None:
    comptable = _entete_auth(db, "COMPTABLE")
    reponse = client.patch(
        f"/epargne/produits/{uuid.uuid4()}/rattachements",
        json={
            "compte_epargne": None,
            "compte_epargne_client": None,
            "compte_charge_interet": None,
            "motif": "Tentative",
        },
        headers=comptable,
    )
    assert reponse.status_code == 404


def test_modification_sans_permission_403(client: TestClient, db: Session) -> None:
    produit = _produit(db, code="PT-403")
    caissier = _entete_auth(db, "CAISSIER")
    reponse = client.patch(
        f"/epargne/produits/{produit.id}/rattachements",
        json={
            "compte_epargne": None,
            "compte_epargne_client": None,
            "compte_charge_interet": None,
            "motif": "Tentative",
        },
        headers=caissier,
    )
    assert reponse.status_code == 403


# --- LE double garde-fou : contourner le sélecteur, prouver que ça mord quand même --------


def test_compte_de_regroupement_soumis_directement_est_refuse(
    client: TestClient, db: Session
) -> None:
    """Le sélecteur ne PROPOSERAIT jamais ce compte (is_posting=False) — on le soumet quand
    même, à la main, directement au corps de la requête HTTP, pour prouver que le refus vient
    d'une VRAIE revérification côté serveur, pas seulement d'un filtre à l'affichage."""
    regroupement = _compte(db, "6T9E5", is_posting=False, normal_side="C")
    produit = _produit(db, code="PT-GROUP")
    db.commit()  # checkpoint : le rollback de la requête (422) ne doit pas emporter ce setup.
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.patch(
        f"/epargne/produits/{produit.id}/rattachements",
        json={
            "compte_epargne": regroupement.account_number,
            "compte_epargne_client": None,
            "compte_charge_interet": None,
            "motif": "Tentative de contournement du sélecteur",
        },
        headers=comptable,
    )

    assert reponse.status_code == 422
    assert "regroupement" in reponse.json()["detail"].lower()
    # RIEN écrit : le produit garde son rattachement d'origine (absent).
    assert (
        db.execute(
            select(Product.compte_epargne_id).where(Product.id == produit.id)
        ).scalar_one()
        is None
    )


def test_compte_desactive_soumis_directement_est_refuse(client: TestClient, db: Session) -> None:
    """Même principe, sur un compte désactivé plutôt qu'un compte de regroupement."""
    desactive = _compte(db, "6T9E6", is_active=False, normal_side="C")
    produit = _produit(db, code="PT-INACTIF")
    db.commit()  # checkpoint : le rollback de la requête (422) ne doit pas emporter ce setup.
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.patch(
        f"/epargne/produits/{produit.id}/rattachements",
        json={
            "compte_epargne": desactive.account_number,
            "compte_epargne_client": None,
            "compte_charge_interet": None,
            "motif": "Tentative de contournement du sélecteur",
        },
        headers=comptable,
    )

    assert reponse.status_code == 422
    assert (
        db.execute(
            select(Product.compte_epargne_id).where(Product.id == produit.id)
        ).scalar_one()
        is None
    )
