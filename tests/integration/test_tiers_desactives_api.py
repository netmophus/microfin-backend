"""Visibilité des fiches DÉSACTIVÉES (R1) — la lecture derrière tiers.read.deleted.

Une fiche désactivée (soft delete : deleted_at posé, status 'desactive') sort de toutes les
lectures normales. R1 rouvre un chemin de LECTURE, réservé à la supervision :

  1. absente des listes/fiche/frise SANS tiers.read.deleted, pour tout le monde ;
  2. visible AVEC, sur demande explicite (inclure_desactives) ;
  3. le paramètre inclure_desactives est IGNORÉ sans la permission (pas d'escalade par la requête) ;
  4. le cloisonnement mord toujours : un responsable ne voit pas les désactivés d'une autre agence.
"""

import uuid
from collections.abc import Generator
from datetime import UTC, date, datetime

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
from app.modules.tiers.models import IndividualProfile

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


def _agence(db: Session, nom: str) -> Agency:
    agence = Agency(code=f"AG-{uuid.uuid4().hex[:6]}", name=nom)
    db.add(agence)
    db.flush()
    return agence


def _pays(db: Session, code: str) -> uuid.UUID:
    from sqlalchemy import text

    return db.execute(
        text("SELECT id FROM parameters.countries WHERE code = :c"), {"c": code}
    ).scalar_one()


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


def _tier_desactive(db: Session, agence: Agency, nom: str = "Sortie") -> IndividualProfile:
    tier = IndividualProfile(
        tier_number=f"M-2999-{uuid.uuid4().int % 10_000_000:07d}",
        primary_agency_id=agence.id,
        last_name=nom,
        first_name="Ancien",
        birth_date=date(1990, 5, 12),
        gender="M",
        nationality_id=_pays(db, "SN"),
        status="desactive",
    )
    tier.deleted_at = datetime.now(UTC)  # soft delete : la fiche est sortie de l'annuaire
    db.add(tier)
    db.flush()
    return tier


def _numeros(reponse_json: dict[str, object]) -> set[str]:
    lignes = reponse_json["lignes"]
    assert isinstance(lignes, list)
    return {ligne["tier_number"] for ligne in lignes}


# --- liste -----------------------------------------------------------------------------


def test_un_desactive_est_absent_de_la_liste_normale(db: Session, client: TestClient) -> None:
    agence = _agence(db, "Agence Centre")
    resp = _utilisateur(db, "RESPONSABLE_AGENCE", agence)  # a pourtant tiers.read.deleted
    tier = _tier_desactive(db, agence)

    # Sans le paramètre, même un habilité ne les voit pas : la liste courante reste propre.
    reponse = client.get("/tiers", headers=_entete(resp, "RESPONSABLE_AGENCE"))
    assert reponse.status_code == 200
    assert tier.tier_number not in _numeros(reponse.json())


def test_un_habilite_voit_les_desactives_sur_demande(db: Session, client: TestClient) -> None:
    agence = _agence(db, "Agence Centre")
    resp = _utilisateur(db, "RESPONSABLE_AGENCE", agence)
    tier = _tier_desactive(db, agence)

    reponse = client.get(
        "/tiers?inclure_desactives=true", headers=_entete(resp, "RESPONSABLE_AGENCE")
    )
    assert reponse.status_code == 200
    assert tier.tier_number in _numeros(reponse.json())


def test_le_parametre_est_ignore_sans_la_permission(db: Session, client: TestClient) -> None:
    """Le point dur : un chargé de clientèle (sans tiers.read.deleted) qui FORCE le paramètre ne
    doit RIEN voir de plus — la permission décide, pas la requête."""
    agence = _agence(db, "Agence Centre")
    charge = _utilisateur(db, "CHARGE_CLIENTELE", agence)
    tier = _tier_desactive(db, agence)

    reponse = client.get(
        "/tiers?inclure_desactives=true", headers=_entete(charge, "CHARGE_CLIENTELE")
    )
    assert reponse.status_code == 200
    assert tier.tier_number not in _numeros(reponse.json())


# --- fiche + frise ---------------------------------------------------------------------


def test_la_fiche_d_un_desactive_est_404_sans_la_permission(
    db: Session, client: TestClient
) -> None:
    agence = _agence(db, "Agence Centre")
    charge = _utilisateur(db, "CHARGE_CLIENTELE", agence)
    tier = _tier_desactive(db, agence)

    reponse = client.get(f"/tiers/{tier.id}", headers=_entete(charge, "CHARGE_CLIENTELE"))
    assert reponse.status_code == 404


def test_la_fiche_et_la_frise_d_un_desactive_sont_visibles_avec_la_permission(
    db: Session, client: TestClient
) -> None:
    agence = _agence(db, "Agence Centre")
    resp = _utilisateur(db, "RESPONSABLE_AGENCE", agence)
    tier = _tier_desactive(db, agence)
    entete = _entete(resp, "RESPONSABLE_AGENCE")

    fiche = client.get(f"/tiers/{tier.id}", headers=entete)
    assert fiche.status_code == 200, fiche.text
    assert fiche.json()["status"] == "desactive"

    frise = client.get(f"/tiers/{tier.id}/timeline", headers=entete)
    assert frise.status_code == 200, frise.text


# --- cloisonnement ---------------------------------------------------------------------


def test_le_cloisonnement_mord_meme_sur_les_desactives(db: Session, client: TestClient) -> None:
    """Un responsable habilité ne voit QUE les désactivés de SON agence."""
    autre = _agence(db, "Agence voisine")
    mienne = _agence(db, "Mon agence")
    tier_voisin = _tier_desactive(db, autre)
    resp = _utilisateur(db, "RESPONSABLE_AGENCE", mienne)
    entete = _entete(resp, "RESPONSABLE_AGENCE")

    liste = client.get("/tiers?inclure_desactives=true", headers=entete)
    assert tier_voisin.tier_number not in _numeros(liste.json())

    fiche = client.get(f"/tiers/{tier_voisin.id}", headers=entete)
    assert fiche.status_code == 404  # hors périmètre -> 404, désactivé ou non
