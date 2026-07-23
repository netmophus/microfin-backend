"""Création des tiers (T1c) — tests d'intégration via TestClient.

Ce que ces tests protègent, par ordre d'importance :

  1. LE CLOISONNEMENT VAUT AUSSI EN ÉCRITURE. Un chargé de clientèle cloisonné ne crée que
     dans SON agence — et forcer une AUTRE agence dans la requête est refusé (422), jamais
     silencieusement rattaché à la bonne. Sans quoi il créerait une fiche orpheline, hors de
     son propre périmètre de lecture.
  2. LA DOUBLE TRACE DIT VRAI : un lifecycle_event 'created' ET une ligne d'audit tier.created
     avec l'acteur en user_id et la fiche en resource_id.
  3. LE GATE DE PERMISSION MORD : sans tiers.create, la création est refusée (403).
"""

import uuid
from collections.abc import Callable, Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.parameters.models import Agency
from app.modules.security.jwt import creer_access_token
from app.modules.security.models import Permission, Role, RolePermission, User, UserRole
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


@pytest.fixture
def accorder(db: Session) -> Callable[[str, str], None]:
    """Accorde une permission à un rôle, le temps du test (annulé au rollback)."""

    def _accorder(role_code: str, permission_code: str) -> None:
        role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
        permission = db.execute(
            select(Permission).where(Permission.code == permission_code)
        ).scalar_one()
        db.add(RolePermission(role_id=role.id, permission_id=permission.id))
        db.flush()

    return _accorder


def _agence(db: Session, nom: str) -> Agency:
    agence = Agency(code=f"AG-{uuid.uuid4().hex[:6]}", name=nom)
    db.add(agence)
    db.flush()
    return agence


def _utilisateur(db: Session, role_code: str, agence: Agency) -> User:
    role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
    suffixe = uuid.uuid4().hex[:8]
    user = User(
        matricule=f"MAT-{suffixe}",
        email=f"{suffixe}@example.com",
        username=f"u{suffixe}",
        password_hash=hasher_mot_de_passe("Motdepasse!123"),
        last_name="Test",
        first_name="Agent",
        primary_agency_id=agence.id,
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.flush()
    return user


def _entete(user: User, role_code: str) -> dict[str, str]:
    jeton = creer_access_token(
        user_id=user.id, roles=[role_code], primary_agency_id=user.primary_agency_id
    )
    return {"Authorization": f"Bearer {jeton}"}


def _pays(db: Session, code: str) -> str:
    return str(
        db.execute(
            text("SELECT id FROM parameters.countries WHERE code = :c"), {"c": code}
        ).scalar_one()
    )


def _individu(db: Session, **extra: object) -> dict[str, object]:
    corps: dict[str, object] = {
        "last_name": "Diallo",
        "first_name": "Amadou",
        "birth_date": "1990-05-12",
        "gender": "M",
        "nationality_id": _pays(db, "SN"),
    }
    corps.update(extra)
    return corps


# --- création des trois types ----------------------------------------------------------


def test_charge_clientele_cree_une_personne_physique(db: Session, client: TestClient) -> None:
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "CHARGE_CLIENTELE", agence)

    reponse = client.post(
        "/tiers/individuals", json=_individu(db), headers=_entete(user, "CHARGE_CLIENTELE")
    )

    assert reponse.status_code == 201, reponse.text
    corps = reponse.json()
    assert corps["tier_number"].startswith("M-")
    assert corps["tier_type"] == "individual"
    assert corps["status"] == "prospect"
    assert corps["individu"]["last_name"] == "Diallo"
    assert corps["primary_agency_id"] == str(agence.id)


def test_cree_une_personne_morale(db: Session, client: TestClient) -> None:
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "CHARGE_CLIENTELE", agence)

    reponse = client.post(
        "/tiers/legal-entities",
        json={
            "legal_name": "ACME SARL",
            "legal_form": "SARL",
            "constitution_date": "2020-01-01",
            "headquarters_country_id": _pays(db, "SN"),
        },
        headers=_entete(user, "CHARGE_CLIENTELE"),
    )

    assert reponse.status_code == 201, reponse.text
    corps = reponse.json()
    assert corps["tier_number"].startswith("P-")
    assert corps["tier_type"] == "legal_entity"
    assert corps["personne_morale"]["legal_name"] == "ACME SARL"


def test_cree_un_groupement(db: Session, client: TestClient) -> None:
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "CHARGE_CLIENTELE", agence)

    reponse = client.post(
        "/tiers/groups",
        json={
            "group_name": "Femmes de Ouallam",
            "group_type": "caution_solidaire",
            "constitution_date": "2021-03-01",
        },
        headers=_entete(user, "CHARGE_CLIENTELE"),
    )

    assert reponse.status_code == 201, reponse.text
    corps = reponse.json()
    assert corps["tier_number"].startswith("G-")
    assert corps["tier_type"] == "group"
    assert corps["groupement"]["group_name"] == "Femmes de Ouallam"


# --- double trace ----------------------------------------------------------------------


def test_la_creation_ecrit_l_audit_et_la_frise(db: Session, client: TestClient) -> None:
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "CHARGE_CLIENTELE", agence)

    reponse = client.post(
        "/tiers/individuals", json=_individu(db), headers=_entete(user, "CHARGE_CLIENTELE")
    )
    tid = reponse.json()["id"]

    # audit_logs : acteur = créateur, cible = la fiche, action = tier.created.
    audit = db.execute(
        text(
            "SELECT user_id, resource_type, action FROM audit.audit_logs "
            "WHERE resource_id = CAST(:rid AS uuid) AND action = 'tier.created'"
        ),
        {"rid": tid},
    ).one()
    assert audit.user_id == user.id
    assert audit.resource_type == "tier"

    # lifecycle_events : un événement 'created' vers le statut prospect.
    frise = db.execute(
        text(
            "SELECT event_type, new_status, performed_by FROM tiers.lifecycle_events "
            "WHERE tier_id = CAST(:rid AS uuid)"
        ),
        {"rid": tid},
    ).one()
    assert frise.event_type == "created"
    assert frise.new_status == "prospect"
    assert frise.performed_by == user.id


# --- gate de permission ----------------------------------------------------------------


def test_sans_permission_create_la_creation_est_refusee(db: Session, client: TestClient) -> None:
    # CAISSIER n'a que tiers.read.basic, pas tiers.create.
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "CAISSIER", agence)

    reponse = client.post(
        "/tiers/individuals", json=_individu(db), headers=_entete(user, "CAISSIER")
    )

    assert reponse.status_code == 403


# --- cloisonnement en écriture ---------------------------------------------------------


def test_un_cloisonne_ne_peut_pas_forcer_une_autre_agence(db: Session, client: TestClient) -> None:
    """Le point dur : un chargé de clientèle qui force l'agence d'autrui dans la requête."""
    mienne = _agence(db, "Mon agence")
    autre = _agence(db, "Agence voisine")
    user = _utilisateur(db, "CHARGE_CLIENTELE", mienne)

    reponse = client.post(
        "/tiers/individuals",
        json=_individu(db, primary_agency_id=str(autre.id)),
        headers=_entete(user, "CHARGE_CLIENTELE"),
    )

    assert reponse.status_code == 422, reponse.text


def test_un_cloisonne_cree_dans_sa_propre_agence_par_defaut(
    db: Session, client: TestClient
) -> None:
    agence = _agence(db, "Mon agence")
    user = _utilisateur(db, "CHARGE_CLIENTELE", agence)

    reponse = client.post(
        "/tiers/individuals",  # sans primary_agency_id
        json=_individu(db),
        headers=_entete(user, "CHARGE_CLIENTELE"),
    )

    assert reponse.status_code == 201, reponse.text
    assert reponse.json()["primary_agency_id"] == str(agence.id)


def test_une_portee_reseau_peut_viser_une_autre_agence(
    db: Session, client: TestClient, accorder: Callable[[str, str], None]
) -> None:
    # DIRECTION_GENERALE détient perimetre.reseau ; on lui prête tiers.create le temps du test.
    accorder("DIRECTION_GENERALE", "tiers.create")
    siege = _agence(db, "Siège")
    cible = _agence(db, "Agence de brousse")
    user = _utilisateur(db, "DIRECTION_GENERALE", siege)

    reponse = client.post(
        "/tiers/individuals",
        json=_individu(db, primary_agency_id=str(cible.id)),
        headers=_entete(user, "DIRECTION_GENERALE"),
    )

    assert reponse.status_code == 201, reponse.text
    assert reponse.json()["primary_agency_id"] == str(cible.id)


def test_une_portee_reseau_doit_preciser_l_agence(
    db: Session, client: TestClient, accorder: Callable[[str, str], None]
) -> None:
    accorder("DIRECTION_GENERALE", "tiers.create")
    siege = _agence(db, "Siège")
    user = _utilisateur(db, "DIRECTION_GENERALE", siege)

    # Une portée réseau n'a pas d'agence « courante » à supposer : « toutes » n'en est pas une.
    jeton = creer_access_token(user_id=user.id, roles=["DIRECTION_GENERALE"], agency_id=None)
    reponse = client.post(
        "/tiers/individuals",
        json=_individu(db),  # sans primary_agency_id
        headers={"Authorization": f"Bearer {jeton}"},
    )

    assert reponse.status_code == 422, reponse.text
