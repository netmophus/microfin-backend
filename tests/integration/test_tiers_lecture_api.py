"""Lecture des tiers (T1d) — tests d'intégration via TestClient.

Ce que ces tests protègent, par ordre d'importance :

  1. UN read.basic N'OBTIENT JAMAIS LES CHAMPS SENSIBLES — même en tentant de forcer le niveau
     complet par un paramètre d'URL. Le niveau vient de la PERMISSION, pas de la requête. On
     prouve l'ABSENCE des clés sensibles (pas leur vacuité) : elles ne sont pas chargées.
  2. LE CLOISONNEMENT VAUT EN LECTURE : une fiche hors agence répond 404 (n'existe pas de mon
     point de vue), jamais 403.
  3. LA FICHE POLYMORPHE rend le bon jeu de champs selon le type.
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

# Clés qui ne doivent JAMAIS apparaître pour un read.basic — sur la fiche comme dans la liste.
CLES_SENSIBLES = frozenset(
    {"individu", "personne_morale", "groupement", "nationality_id", "monthly_income_estimate"}
)


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
    from sqlalchemy import text

    return str(
        db.execute(
            text("SELECT id FROM parameters.countries WHERE code = :c"), {"c": code}
        ).scalar_one()
    )


def _creer_individu(client: TestClient, db: Session, createur: User) -> str:
    """Crée une personne physique via l'API et rend son id (avec des champs sensibles remplis)."""
    reponse = client.post(
        "/tiers/individuals",
        json={
            "last_name": "Diallo",
            "first_name": "Amadou",
            "birth_date": "1990-05-12",
            "gender": "M",
            "nationality_id": _pays(db, "SN"),
            "profession": "Commerçante",
            "monthly_income_estimate": "150000.00",
        },
        headers=_entete(createur, "CHARGE_CLIENTELE"),
    )
    assert reponse.status_code == 201, reponse.text
    return reponse.json()["id"]


# --- 1. read.basic : jamais les champs sensibles, même en forçant ----------------------


def test_le_caissier_recoit_le_resume_sans_champ_sensible(db: Session, client: TestClient) -> None:
    agence = _agence(db, "Agence Centre")
    createur = _utilisateur(db, "CHARGE_CLIENTELE", agence)
    tid = _creer_individu(client, db, createur)

    caissier = _utilisateur(db, "CAISSIER", agence)
    reponse = client.get(f"/tiers/{tid}", headers=_entete(caissier, "CAISSIER"))

    assert reponse.status_code == 200, reponse.text
    corps = reponse.json()
    # Il voit l'identification…
    assert corps["tier_number"].startswith("M-")
    assert corps["display_name"] == "Diallo Amadou"
    assert corps["status"] == "prospect"
    # …et AUCUNE clé sensible n'est présente (absence, pas vacuité).
    assert CLES_SENSIBLES.isdisjoint(corps.keys())


def test_le_caissier_ne_peut_pas_forcer_le_niveau_complet(db: Session, client: TestClient) -> None:
    """Le point crucial : forcer le détail par des paramètres d'URL ne change RIEN."""
    agence = _agence(db, "Agence Centre")
    createur = _utilisateur(db, "CHARGE_CLIENTELE", agence)
    tid = _creer_individu(client, db, createur)
    caissier = _utilisateur(db, "CAISSIER", agence)

    # On tente de forcer par tous les leviers plausibles : le niveau vient de la permission,
    # aucun de ces paramètres n'est lu par la route.
    for suffixe in ("?detail=full", "?full=true", "?complet=1", "?niveau=complet"):
        reponse = client.get(f"/tiers/{tid}{suffixe}", headers=_entete(caissier, "CAISSIER"))
        assert reponse.status_code == 200, reponse.text
        assert CLES_SENSIBLES.isdisjoint(reponse.json().keys()), suffixe


def test_un_lecteur_complet_obtient_bien_les_champs_sensibles(
    db: Session, client: TestClient
) -> None:
    # Le contraste : le chargé de clientèle (tiers.read) reçoit, lui, le bloc complet.
    agence = _agence(db, "Agence Centre")
    createur = _utilisateur(db, "CHARGE_CLIENTELE", agence)
    tid = _creer_individu(client, db, createur)

    reponse = client.get(f"/tiers/{tid}", headers=_entete(createur, "CHARGE_CLIENTELE"))

    assert reponse.status_code == 200, reponse.text
    corps = reponse.json()
    assert corps["individu"]["profession"] == "Commerçante"
    assert corps["individu"]["monthly_income_estimate"] == "150000.00"


# --- 2. cloisonnement en lecture : 404, jamais 403 -------------------------------------


def test_une_fiche_hors_agence_repond_404(db: Session, client: TestClient) -> None:
    mienne = _agence(db, "Mon agence")
    ailleurs = _agence(db, "Agence voisine")
    # La fiche est créée par une portée réseau, rattachée à « ailleurs ».
    createur = _utilisateur(db, "CHARGE_CLIENTELE", ailleurs)
    tid = _creer_individu(client, db, createur)

    # Un chargé de clientèle de « mon agence » ne doit pas la voir — 404, pas 403.
    intrus = _utilisateur(db, "CHARGE_CLIENTELE", mienne)
    reponse = client.get(f"/tiers/{tid}", headers=_entete(intrus, "CHARGE_CLIENTELE"))

    assert reponse.status_code == 404


def test_la_liste_est_cloisonnee(db: Session, client: TestClient) -> None:
    mienne = _agence(db, "Mon agence")
    ailleurs = _agence(db, "Agence voisine")
    _creer_individu(client, db, _utilisateur(db, "CHARGE_CLIENTELE", ailleurs))
    moi = _utilisateur(db, "CHARGE_CLIENTELE", mienne)

    reponse = client.get("/tiers", headers=_entete(moi, "CHARGE_CLIENTELE"))

    assert reponse.status_code == 200, reponse.text
    corps = reponse.json()
    # La fiche d'ailleurs n'apparaît pas, et le total suit le filtre (0 pour mon agence vide).
    assert corps["total"] == 0
    assert corps["lignes"] == []


# --- 3. fiche polymorphe ---------------------------------------------------------------


def test_la_personne_morale_rend_son_bloc(db: Session, client: TestClient) -> None:
    agence = _agence(db, "Agence Centre")
    createur = _utilisateur(db, "CHARGE_CLIENTELE", agence)
    tid = client.post(
        "/tiers/legal-entities",
        json={
            "legal_name": "ACME SARL",
            "legal_form": "SARL",
            "constitution_date": "2020-01-01",
            "headquarters_country_id": _pays(db, "SN"),
        },
        headers=_entete(createur, "CHARGE_CLIENTELE"),
    ).json()["id"]

    corps = client.get(f"/tiers/{tid}", headers=_entete(createur, "CHARGE_CLIENTELE")).json()

    assert corps["tier_type"] == "legal_entity"
    assert corps["personne_morale"]["legal_name"] == "ACME SARL"
    assert corps["individu"] is None
    assert corps["groupement"] is None


# --- timeline --------------------------------------------------------------------------


def test_la_timeline_montre_la_creation(db: Session, client: TestClient) -> None:
    agence = _agence(db, "Agence Centre")
    createur = _utilisateur(db, "CHARGE_CLIENTELE", agence)
    tid = _creer_individu(client, db, createur)

    corps = client.get(
        f"/tiers/{tid}/timeline", headers=_entete(createur, "CHARGE_CLIENTELE")
    ).json()

    assert len(corps) == 1
    assert corps[0]["event_type"] == "created"
    assert corps[0]["new_status"] == "prospect"


def test_la_timeline_hors_agence_repond_404(db: Session, client: TestClient) -> None:
    ailleurs = _agence(db, "Agence voisine")
    tid = _creer_individu(client, db, _utilisateur(db, "CHARGE_CLIENTELE", ailleurs))
    intrus = _utilisateur(db, "CHARGE_CLIENTELE", _agence(db, "Mon agence"))

    reponse = client.get(f"/tiers/{tid}/timeline", headers=_entete(intrus, "CHARGE_CLIENTELE"))

    assert reponse.status_code == 404
