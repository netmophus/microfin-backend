"""API — rangement manuel d'un compte sans poste (POST /etats/mapping), liste des orphelins
(GET /etats/mapping/orphelins) et audit du PATCH (compta.plan.manage)."""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.comptabilite import comptes
from app.modules.comptabilite.models import Account, FinancialStatementMapping
from app.modules.security.jwt import creer_access_token
from app.modules.security.models import Role, User, UserRole
from app.modules.security.password import hasher_mot_de_passe

pytestmark = pytest.mark.integration

CORPS = {
    "etat": "BILAN",
    "masse": "ACTIF",
    "poste_libelle": "Poste choisi",
    "poste_ordre": 40,
}


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


def _entete(db: Session, role_code: str) -> dict[str, str]:
    role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
    agence_id = db.execute(text("SELECT id FROM parameters.agencies LIMIT 1")).scalar_one()
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


def _creer(db: Session, numero: str, parent: str | None, *, posting: bool = True) -> Account:
    return comptes.creer(
        db,
        account_number=numero,
        name=f"Compte {numero}",
        short_name=None,
        account_class=int(numero[0]),
        parent_number=parent,
        normal_side="D",
        is_posting=posting,
        notes=None,
        par=None,
    )


def _audit(db: Session, action: str, compte_id: uuid.UUID) -> list:
    return list(
        db.execute(
            text(
                "SELECT old_values, new_values FROM audit.audit_logs "
                "WHERE action = :a AND resource_id = :r"
            ),
            {"a": action, "r": compte_id},
        ).all()
    )


def test_creation_ok_est_verrouillee_contre_le_seed_et_auditee(
    client: TestClient, db: Session
) -> None:
    compte = _creer(db, "1T9000", None)  # parent absent : orphelin
    comptable = _entete(db, "COMPTABLE")

    reponse = client.post(
        "/comptabilite/etats/mapping", json={**CORPS, "account_id": str(compte.id)},
        headers=comptable,
    )

    assert reponse.status_code == 201
    assert reponse.json()["gere_manuellement"] is True
    ligne = db.get(FinancialStatementMapping, compte.id)
    assert ligne is not None and ligne.gere_manuellement is True
    assert ligne.poste_libelle == "Poste choisi"
    [(ancien, nouveau)] = _audit(db, "compta.mapping.created", compte.id)
    assert ancien is None
    assert nouveau["poste_libelle"] == "Poste choisi" and nouveau["poste_ordre"] == 40


def test_doublon_refuse_409(client: TestClient, db: Session) -> None:
    compte = _creer(db, "1T9001", None)
    comptable = _entete(db, "COMPTABLE")
    corps = {**CORPS, "account_id": str(compte.id)}
    premier = client.post("/comptabilite/etats/mapping", json=corps, headers=comptable)
    assert premier.status_code == 201

    reponse = client.post("/comptabilite/etats/mapping", json=corps, headers=comptable)

    assert reponse.status_code == 409
    assert reponse.json()["detail"] == "Ce compte est déjà mappé — utilisez Modifier."


def test_regroupement_refuse_422(client: TestClient, db: Session) -> None:
    regroupement = _creer(db, "1T9002", None, posting=False)
    comptable = _entete(db, "COMPTABLE")

    reponse = client.post(
        "/comptabilite/etats/mapping", json={**CORPS, "account_id": str(regroupement.id)},
        headers=comptable,
    )

    assert reponse.status_code == 422
    assert db.get(FinancialStatementMapping, regroupement.id) is None


def test_etat_ou_masse_hors_liste_refuse_422(client: TestClient, db: Session) -> None:
    compte = _creer(db, "1T9003", None)
    comptable = _entete(db, "COMPTABLE")
    for mauvais in ({"etat": "AUTRE"}, {"masse": "INCONNUE"}):
        reponse = client.post(
            "/comptabilite/etats/mapping",
            json={**CORPS, "account_id": str(compte.id), **mauvais},
            headers=comptable,
        )
        assert reponse.status_code == 422


def test_compte_inexistant_404(client: TestClient, db: Session) -> None:
    comptable = _entete(db, "COMPTABLE")

    reponse = client.post(
        "/comptabilite/etats/mapping", json={**CORPS, "account_id": str(uuid.uuid4())},
        headers=comptable,
    )

    assert reponse.status_code == 404


def test_orphelins_liste_le_compte_sous_parent_non_mappe_pas_l_herite(
    client: TestClient, db: Session
) -> None:
    parent_non_mappe = _creer(db, "1T9100", None)
    orphelin = _creer(db, "1T910001", "1T9100")
    herite = _creer(db, "10111188", "101111")  # parent mappé : hérite
    regroupement = _creer(db, "1T9200", None, posting=False)
    inactif = _creer(db, "1T9300", None)
    inactif.is_active = False
    db.flush()
    comptable = _entete(db, "COMPTABLE")

    reponse = client.get("/comptabilite/etats/mapping/orphelins", headers=comptable)

    assert reponse.status_code == 200
    par_numero = {ligne["account_number"]: ligne for ligne in reponse.json()}
    assert "1T910001" in par_numero and "1T9100" in par_numero
    assert par_numero["1T910001"]["parent_number"] == "1T9100"
    assert par_numero["1T910001"]["parent_poste_libelle"] is None  # parent non mappé
    assert herite.account_number not in par_numero
    assert regroupement.account_number not in par_numero
    assert inactif.account_number not in par_numero
    assert orphelin.id and parent_non_mappe.id


def test_orphelin_dont_le_parent_est_mappe_expose_le_poste_propose(
    client: TestClient, db: Session
) -> None:
    parent = _creer(db, "1T9400", None)
    db.add(
        FinancialStatementMapping(
            account_id=parent.id, etat="BILAN", masse="ACTIF", poste_libelle="Poste parent",
            poste_ordre=70,
        )
    )
    db.flush()
    # Ajouté sans passer par creer() : orphelin malgré un parent mappé (base d'avant la règle).
    db.add(
        Account(
            account_number="1T940001", name="Orphelin", account_class=1, parent_id=parent.id,
            normal_side="D", is_posting=True, is_system=False,
        )
    )
    db.flush()
    comptable = _entete(db, "COMPTABLE")

    reponse = client.get("/comptabilite/etats/mapping/orphelins", headers=comptable)

    ligne = {x["account_number"]: x for x in reponse.json()}["1T940001"]
    assert ligne["parent_poste_libelle"] == "Poste parent"
    assert (ligne["parent_etat"], ligne["parent_masse"], ligne["parent_poste_ordre"]) == (
        "BILAN",
        "ACTIF",
        70,
    )


def test_patch_produit_une_entree_d_audit_avec_ancien_et_nouveau(
    client: TestClient, db: Session
) -> None:
    compte = _creer(db, "10111177", "101111")  # hérite de « Valeurs en caisse »
    comptable = _entete(db, "COMPTABLE")

    reponse = client.patch(
        f"/comptabilite/etats/mapping/{compte.id}",
        json={**CORPS, "poste_libelle": "Autre poste"},
        headers=comptable,
    )

    assert reponse.status_code == 200
    [(ancien, nouveau)] = _audit(db, "compta.mapping.updated", compte.id)
    assert ancien["poste_libelle"] == "Valeurs en caisse"
    assert nouveau["poste_libelle"] == "Autre poste" and nouveau["poste_ordre"] == 40


@pytest.mark.parametrize("methode", ["get", "post"])
def test_sans_permission_refuse_403(client: TestClient, db: Session, methode: str) -> None:
    caissier = _entete(db, "CAISSIER")
    compte = _creer(db, "1T9500", None)

    if methode == "get":
        reponse = client.get("/comptabilite/etats/mapping/orphelins", headers=caissier)
    else:
        reponse = client.post(
            "/comptabilite/etats/mapping", json={**CORPS, "account_id": str(compte.id)},
            headers=caissier,
        )

    assert reponse.status_code == 403
    assert db.get(FinancialStatementMapping, compte.id) is None
