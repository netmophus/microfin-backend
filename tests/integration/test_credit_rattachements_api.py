"""API rattachements comptables des produits de crédit (lot 3b) — miroir de
`test_epargne_rattachements_api.py`, premier bloc « comptable » que le crédit n'avait pas
encore.

  - lecture : les 3 comptes résolus en numéro+libellé, jamais un UUID ;
  - écriture : motif obligatoire, tracé avant/après ; vider un rattachement est LÉGITIME ;
  - GARDE-FOU DOUBLE : `compte_saisie_actif` refuse un compte de regroupement ou désactivé
    même soumis directement à l'API, en contournant un sélecteur ;
  - cohérence avec `valider_produit` (lot 3a) : modifier les rattachements d'un produit DÉJÀ
    VALIDÉ reste possible (le comptable doit pouvoir corriger une erreur après coup) — seule
    la validation elle-même contrôle leur présence, jamais cet endpoint.
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
from app.modules.credit.models import Product
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
        "code": f"PC{uuid.uuid4().hex[:6]}",
        "name": "Produit de test",
        **overrides,
    }
    produit = Product(**valeurs)
    db.add(produit)
    db.flush()
    return produit


CORPS_VIDE = {
    "compte_credit_membre": None,
    "compte_credit_client": None,
    "compte_produits_interets": None,
    "motif": "Tentative",
}


# --- Lecture ---------------------------------------------------------------------------


def test_lecture_expose_les_3_comptes_resolus(client: TestClient, db: Session) -> None:
    membre = _compte(db, "461201")
    produit = _produit(db, code="PC-LEC", compte_credit_membre_id=membre.id)
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.get(f"/credit/produits/{produit.id}/rattachements", headers=comptable)

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["compte_credit_membre"] == {"account_number": "461201", "name": "Compte 461201"}
    assert corps["compte_credit_client"] is None
    assert corps["compte_produits_interets"] is None


def test_lecture_sans_permission_403(client: TestClient, db: Session) -> None:
    produit = _produit(db, code="PC-LEC403")
    caissier = _entete_auth(db, "CAISSIER")
    reponse = client.get(f"/credit/produits/{produit.id}/rattachements", headers=caissier)
    assert reponse.status_code == 403


def test_lecture_produit_introuvable_404(client: TestClient, db: Session) -> None:
    comptable = _entete_auth(db, "COMPTABLE")
    reponse = client.get(f"/credit/produits/{uuid.uuid4()}/rattachements", headers=comptable)
    assert reponse.status_code == 404


# --- Écriture : succès, motif, audit ------------------------------------------------------


def test_modification_reussie_des_3_comptes_avec_motif_trace(
    client: TestClient, db: Session
) -> None:
    membre = _compte(db, "461211")
    client_compte = _compte(db, "461221")
    interets = _compte(db, "752111", account_class=7, normal_side="C")
    produit = _produit(db, code="PC-MOD")
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.patch(
        f"/credit/produits/{produit.id}/rattachements",
        json={
            "compte_credit_membre": membre.account_number,
            "compte_credit_client": client_compte.account_number,
            "compte_produits_interets": interets.account_number,
            "motif": "Rattachement initial du produit",
        },
        headers=comptable,
    )

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["compte_credit_membre"]["account_number"] == "461211"
    assert corps["compte_credit_client"]["account_number"] == "461221"
    assert corps["compte_produits_interets"]["account_number"] == "752111"

    ligne = db.execute(
        text(
            "SELECT old_values, new_values FROM audit.audit_logs "
            "WHERE action = 'credit.product.comptes_updated' AND resource_id = :r"
        ),
        {"r": produit.id},
    ).one()
    assert ligne.old_values["compte_credit_membre"] is None
    assert ligne.new_values["motif"] == "Rattachement initial du produit"


def test_modification_motif_absent_refusee(client: TestClient, db: Session) -> None:
    produit = _produit(db, code="PC-NOMOTIF")
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.patch(
        f"/credit/produits/{produit.id}/rattachements",
        json={**CORPS_VIDE, "motif": ""},
        headers=comptable,
    )
    assert reponse.status_code == 422


def test_modification_sans_permission_403(client: TestClient, db: Session) -> None:
    produit = _produit(db, code="PC-403")
    caissier = _entete_auth(db, "CAISSIER")
    reponse = client.patch(
        f"/credit/produits/{produit.id}/rattachements", json=CORPS_VIDE, headers=caissier
    )
    assert reponse.status_code == 403


def test_modification_produit_introuvable_404(client: TestClient, db: Session) -> None:
    comptable = _entete_auth(db, "COMPTABLE")
    reponse = client.patch(
        f"/credit/produits/{uuid.uuid4()}/rattachements", json=CORPS_VIDE, headers=comptable
    )
    assert reponse.status_code == 404


def test_modification_possible_sur_produit_deja_valide(client: TestClient, db: Session) -> None:
    """Cohérence avec valider_produit (lot 3a) : modifier les rattachements d'un produit DÉJÀ
    validé reste possible — seule la validation elle-même contrôle leur présence."""
    membre = _compte(db, "252131")
    produit = _produit(
        db, code="PC-VALIDE", compte_credit_membre_id=membre.id, is_provisional=False
    )
    nouveau_membre = _compte(db, "252132")
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.patch(
        f"/credit/produits/{produit.id}/rattachements",
        json={
            "compte_credit_membre": nouveau_membre.account_number,
            "compte_credit_client": None,
            "compte_produits_interets": None,
            "motif": "Correction après coup",
        },
        headers=comptable,
    )

    assert reponse.status_code == 200
    assert reponse.json()["compte_credit_membre"]["account_number"] == "252132"
    db.refresh(produit)
    assert produit.is_provisional is False  # non touché par ce endpoint


# --- LE double garde-fou : contourner le sélecteur, prouver que ça mord quand même --------


def test_compte_de_regroupement_soumis_directement_est_refuse(
    client: TestClient, db: Session
) -> None:
    regroupement = _compte(db, "2T9E5", is_posting=False, normal_side="C")
    produit = _produit(db, code="PC-GROUP")
    db.commit()  # checkpoint : le rollback de la requête (422) ne doit pas emporter ce setup.
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.patch(
        f"/credit/produits/{produit.id}/rattachements",
        json={
            "compte_credit_membre": regroupement.account_number,
            "compte_credit_client": None,
            "compte_produits_interets": None,
            "motif": "Tentative de contournement du sélecteur",
        },
        headers=comptable,
    )

    assert reponse.status_code == 422
    assert "regroupement" in reponse.json()["detail"].lower()
    assert (
        db.execute(
            select(Product.compte_credit_membre_id).where(Product.id == produit.id)
        ).scalar_one()
        is None
    )


def test_compte_desactive_soumis_directement_est_refuse(client: TestClient, db: Session) -> None:
    desactive = _compte(db, "2T9E6", is_active=False, normal_side="C")
    produit = _produit(db, code="PC-INACTIF")
    db.commit()  # checkpoint : le rollback de la requête (422) ne doit pas emporter ce setup.
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.patch(
        f"/credit/produits/{produit.id}/rattachements",
        json={
            "compte_credit_membre": desactive.account_number,
            "compte_credit_client": None,
            "compte_produits_interets": None,
            "motif": "Tentative de contournement du sélecteur",
        },
        headers=comptable,
    )

    assert reponse.status_code == 422
    assert (
        db.execute(
            select(Product.compte_credit_membre_id).where(Product.id == produit.id)
        ).scalar_one()
        is None
    )
