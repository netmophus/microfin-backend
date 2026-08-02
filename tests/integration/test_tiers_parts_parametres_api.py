"""API des paramètres d'institution des parts sociales (Bloc 5 du paramétrage comptable).

  - lecture résolue en numéro de compte (jamais un UUID) ;
  - écriture : motif obligatoire et tracé, moment d'adhésion limité à un enum valide (422
    Pydantic sur toute autre valeur), vider un rattachement est légitime ;
  - DOUBLE garde-fou sur les comptes, prouvé en contournant le sélecteur directement ;
  - la ligne reste UNIQUE (migration 0029) : une deuxième INSERT directe est refusée par la
    base, pas seulement par convention applicative.
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.comptabilite.models import Account
from app.modules.security.jwt import creer_access_token
from app.modules.security.models import Role, User, UserRole
from app.modules.security.password import hasher_mot_de_passe
from app.modules.tiers.models import ShareParameters

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
        "normal_side": "C",
        "is_posting": True,
        "is_system": False,
        **overrides,
    }
    compte = Account(**valeurs)
    db.add(compte)
    db.flush()
    return compte


def _corps(**overrides: object) -> dict:
    base = {
        "unit_value": 5000,
        "minimum_shares": 1,
        "is_refundable": True,
        "membership_on": "liberation",
        "compte_parts_liberees": None,
        "compte_parts_non_liberees": None,
        "motif": "Ajustement des paramètres de parts",
    }
    base.update(overrides)
    return base


# --- Lecture ---------------------------------------------------------------------------


def test_lecture_resout_les_comptes_en_numero(client: TestClient, db: Session) -> None:
    comptable = _entete_auth(db, "COMPTABLE")
    reponse = client.get("/tiers/parts/parametres", headers=comptable)

    assert reponse.status_code == 200
    donnees = reponse.json()
    assert "unit_value" in donnees
    assert "compte_parts_liberees" in donnees


def test_lecture_sans_permission_403(client: TestClient, db: Session) -> None:
    caissier = _entete_auth(db, "CAISSIER")
    reponse = client.get("/tiers/parts/parametres", headers=caissier)
    assert reponse.status_code == 403


# --- Écriture : succès, motif, moment d'adhésion, vider un rattachement -------------------


def test_modification_reussie_avec_motif_trace(client: TestClient, db: Session) -> None:
    _compte(db, "1T9P1")
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.patch(
        "/tiers/parts/parametres",
        json=_corps(unit_value=10000, compte_parts_liberees="1T9P1"),
        headers=comptable,
    )

    assert reponse.status_code == 200
    donnees = reponse.json()
    assert donnees["unit_value"] == 10000
    assert donnees["compte_parts_liberees"]["account_number"] == "1T9P1"

    config_id = db.execute(select(ShareParameters.id)).scalar_one()
    ligne = db.execute(
        text(
            "SELECT old_values, new_values FROM audit.audit_logs "
            "WHERE action = 'tiers.share_parameters.updated' AND resource_id = :r "
            "ORDER BY occurred_at DESC LIMIT 1"
        ),
        {"r": config_id},
    ).one()
    assert ligne.new_values["unit_value"] == 10000
    assert ligne.new_values["motif"] == "Ajustement des paramètres de parts"


def test_moment_adhesion_invalide_refuse_par_le_schema(client: TestClient, db: Session) -> None:
    comptable = _entete_auth(db, "COMPTABLE")
    reponse = client.patch(
        "/tiers/parts/parametres",
        json=_corps(membership_on="autre_chose"),
        headers=comptable,
    )
    assert reponse.status_code == 422


def test_motif_absent_refuse(client: TestClient, db: Session) -> None:
    comptable = _entete_auth(db, "COMPTABLE")
    reponse = client.patch(
        "/tiers/parts/parametres", json=_corps(motif=""), headers=comptable
    )
    assert reponse.status_code == 422


def test_vider_un_rattachement_est_legitime(client: TestClient, db: Session) -> None:
    _compte(db, "1T9P2")
    comptable = _entete_auth(db, "COMPTABLE")

    client.patch(
        "/tiers/parts/parametres",
        json=_corps(compte_parts_non_liberees="1T9P2"),
        headers=comptable,
    )
    reponse = client.patch(
        "/tiers/parts/parametres",
        json=_corps(compte_parts_non_liberees=None),
        headers=comptable,
    )

    assert reponse.status_code == 200
    assert reponse.json()["compte_parts_non_liberees"] is None


def test_modification_sans_permission_403(client: TestClient, db: Session) -> None:
    caissier = _entete_auth(db, "CAISSIER")
    reponse = client.patch("/tiers/parts/parametres", json=_corps(), headers=caissier)
    assert reponse.status_code == 403


# --- Double garde-fou : contourner le sélecteur, prouver que ça mord quand même -----------


def test_compte_de_regroupement_soumis_directement_est_refuse(
    client: TestClient, db: Session
) -> None:
    regroupement = _compte(db, "1T9P3", is_posting=False)
    db.commit()  # checkpoint : le rollback de la requête (422) ne doit pas emporter ce setup.
    avant = db.execute(select(ShareParameters.compte_parts_liberees_id)).scalar_one()
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.patch(
        "/tiers/parts/parametres",
        json=_corps(
            compte_parts_liberees=regroupement.account_number,
            motif="Tentative de contournement du sélecteur",
        ),
        headers=comptable,
    )

    assert reponse.status_code == 422
    assert "regroupement" in reponse.json()["detail"].lower()
    # RIEN écrit : le rattachement garde EXACTEMENT sa valeur d'avant la tentative.
    assert db.execute(select(ShareParameters.compte_parts_liberees_id)).scalar_one() == avant


def test_compte_desactive_soumis_directement_est_refuse(client: TestClient, db: Session) -> None:
    desactive = _compte(db, "1T9P4", is_active=False)
    db.commit()
    avant = db.execute(select(ShareParameters.compte_parts_liberees_id)).scalar_one()
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.patch(
        "/tiers/parts/parametres",
        json=_corps(
            compte_parts_liberees=desactive.account_number,
            motif="Tentative de contournement du sélecteur",
        ),
        headers=comptable,
    )

    assert reponse.status_code == 422
    assert db.execute(select(ShareParameters.compte_parts_liberees_id)).scalar_one() == avant


# --- Unicité de la ligne (migration 0029) : la base refuse, pas juste la convention -------


def test_deuxieme_ligne_share_parameters_refusee_par_la_base(db: Session) -> None:
    with pytest.raises(IntegrityError):
        db.execute(
            text(
                "INSERT INTO tiers.share_parameters (unit_value, minimum_shares) "
                "VALUES (1000, 1)"
            )
        )
        db.flush()
