"""Cycle de vie des tiers (T1e) — transitions de statut et activation stub.

Ce que ces tests protègent, par ordre d'importance :

  1. LE TYPE VÉRIFIÉ AVANT LA BASE : un décès sur une personne morale répond 409 PROPRE, pas
     un 500 opaque. Le service et le CHECK D4 disent la même chose.
  2. SOFT DELETE : désactiver fait SORTIR la fiche de l'annuaire ; un décédé, lui, RESTE.
  3. LE PÉRIMÈTRE À L'ÉCRITURE : on ne pilote pas une fiche hors de son agence -> 404.
  4. L'ACTIVATION STUB renvoie TOUTES les conditions manquantes (412).
  5. LA SÉPARATION : désactiver est réservé au responsable, refusé au chargé de clientèle.
"""

import uuid
from collections.abc import Generator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.parameters.models import Agency
from app.modules.security.jwt import creer_access_token
from app.modules.security.models import Role, User, UserRole
from app.modules.security.password import hasher_mot_de_passe
from app.modules.tiers.models import IndividualProfile, LegalEntityProfile, Tier

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


def _utilisateur(db: Session, role_code: str, agence: Agency) -> User:
    role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
    suffixe = uuid.uuid4().hex[:8]
    user = User(
        matricule=f"MAT-{suffixe}",
        email=f"{suffixe}@example.com",
        username=f"u{suffixe}",
        password_hash=hasher_mot_de_passe("Motdepasse!123"),
        last_name="Sow",
        first_name="Responsable",
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


def _pays(db: Session, code: str) -> uuid.UUID:
    return db.execute(
        text("SELECT id FROM parameters.countries WHERE code = :c"), {"c": code}
    ).scalar_one()


def _individu(db: Session, agence: Agency, *, status: str = "actif") -> Tier:
    tier = IndividualProfile(
        tier_number=f"M-2999-{uuid.uuid4().int % 10_000_000:07d}",
        primary_agency_id=agence.id,
        status=status,
        last_name="Diallo",
        first_name="Amadou",
        birth_date=date(1990, 5, 12),
        gender="M",
        nationality_id=_pays(db, "SN"),
    )
    db.add(tier)
    db.flush()
    return tier


def _morale(db: Session, agence: Agency, *, status: str = "actif") -> Tier:
    tier = LegalEntityProfile(
        tier_number=f"P-2999-{uuid.uuid4().int % 10_000_000:07d}",
        primary_agency_id=agence.id,
        status=status,
        legal_name="ACME SARL",
        legal_form="SARL",
        constitution_date=date(2020, 1, 1),
        headquarters_country_id=_pays(db, "SN"),
    )
    db.add(tier)
    db.flush()
    return tier


# --- transitions légales ---------------------------------------------------------------


def test_suspendre_puis_reactiver(db: Session, client: TestClient) -> None:
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "RESPONSABLE_AGENCE", agence)
    tier = _individu(db, agence, status="actif")

    r1 = client.post(
        f"/tiers/{tier.id}/suspend",
        json={"motif": "Pièce d'identité expirée"},
        headers=_entete(user, "RESPONSABLE_AGENCE"),
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["status"] == "suspendu_temporaire"

    r2 = client.post(f"/tiers/{tier.id}/reactivate", headers=_entete(user, "RESPONSABLE_AGENCE"))
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "actif"


def test_transition_illegale_repond_409_en_nommant_le_statut(
    db: Session, client: TestClient
) -> None:
    # Suspendre suppose une fiche ACTIVE ; une prospect ne l'est pas.
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "RESPONSABLE_AGENCE", agence)
    tier = _individu(db, agence, status="prospect")

    r = client.post(f"/tiers/{tier.id}/suspend", headers=_entete(user, "RESPONSABLE_AGENCE"))

    assert r.status_code == 409, r.text
    assert "prospect" in r.json()["detail"]  # le message nomme le statut courant


# --- l'alignement D4 : type vérifié AVANT la base --------------------------------------


def test_deces_sur_personne_morale_est_un_409_propre_pas_un_500(
    db: Session, client: TestClient
) -> None:
    """Le point dur : le service arrête le mauvais type en 409, la base ne fire jamais son CHECK."""
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "RESPONSABLE_AGENCE", agence)
    morale = _morale(db, agence, status="actif")

    r = client.post(
        f"/tiers/{morale.id}/mark-deceased", headers=_entete(user, "RESPONSABLE_AGENCE")
    )

    assert r.status_code == 409, r.text
    assert "personne physique" in r.json()["detail"]


# --- soft delete : le décédé reste, le désactivé sort ----------------------------------


def test_le_decede_reste_visible_dans_l_annuaire(db: Session, client: TestClient) -> None:
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "RESPONSABLE_AGENCE", agence)
    tier = _individu(db, agence, status="actif")

    r = client.post(f"/tiers/{tier.id}/mark-deceased", headers=_entete(user, "RESPONSABLE_AGENCE"))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "decede"

    # Toujours dans la liste (succession, épargne à liquider) : pas de deleted_at.
    liste = client.get("/tiers", headers=_entete(user, "RESPONSABLE_AGENCE")).json()
    assert any(ligne["id"] == str(tier.id) for ligne in liste["lignes"])


def test_le_desactive_sort_de_l_annuaire(db: Session, client: TestClient) -> None:
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "RESPONSABLE_AGENCE", agence)
    tier = _individu(db, agence, status="actif")

    r = client.post(f"/tiers/{tier.id}/deactivate", headers=_entete(user, "RESPONSABLE_AGENCE"))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "desactive"

    # Soft delete : deleted_at posé en base.
    deleted_at = db.execute(
        text("SELECT deleted_at FROM tiers.tiers WHERE id = :i"), {"i": tier.id}
    ).scalar_one()
    assert deleted_at is not None

    # Et elle disparaît des lectures normales.
    liste = client.get("/tiers", headers=_entete(user, "RESPONSABLE_AGENCE")).json()
    assert all(ligne["id"] != str(tier.id) for ligne in liste["lignes"])


# --- séparation : désactiver est réservé au responsable --------------------------------


def test_le_charge_de_clientele_ne_peut_pas_desactiver(db: Session, client: TestClient) -> None:
    # CHARGE_CLIENTELE a tiers.suspend mais PAS tiers.deactivate.
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "CHARGE_CLIENTELE", agence)
    tier = _individu(db, agence, status="actif")

    r = client.post(f"/tiers/{tier.id}/deactivate", headers=_entete(user, "CHARGE_CLIENTELE"))

    assert r.status_code == 403


# --- activation stub -------------------------------------------------------------------


def test_activation_renvoie_412_avec_les_conditions_manquantes(
    db: Session, client: TestClient
) -> None:
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "RESPONSABLE_AGENCE", agence)
    tier = _individu(db, agence, status="prospect")

    r = client.post(f"/tiers/{tier.id}/activate", headers=_entete(user, "RESPONSABLE_AGENCE"))

    assert r.status_code == 412, r.text
    codes = [c["code"] for c in r.json()["detail"]["conditions_manquantes"]]
    assert "KYC_NON_VALIDE" in codes  # T3 en ajoutera d'autres, toutes renvoyées d'un coup


# --- périmètre à l'écriture ------------------------------------------------------------


def test_on_ne_suspend_pas_une_fiche_hors_de_son_agence(db: Session, client: TestClient) -> None:
    ailleurs = _agence(db, "Agence voisine")
    tier = _individu(db, ailleurs, status="actif")
    # Un responsable de MON agence ne voit pas la fiche d'ailleurs -> 404, pas 403.
    user = _utilisateur(db, "RESPONSABLE_AGENCE", _agence(db, "Mon agence"))

    r = client.post(f"/tiers/{tier.id}/suspend", headers=_entete(user, "RESPONSABLE_AGENCE"))

    assert r.status_code == 404


# --- double trace ----------------------------------------------------------------------


def test_la_suspension_est_tracee_et_auditee(db: Session, client: TestClient) -> None:
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "RESPONSABLE_AGENCE", agence)
    tier = _individu(db, agence, status="actif")

    client.post(f"/tiers/{tier.id}/suspend", headers=_entete(user, "RESPONSABLE_AGENCE"))

    frise = db.execute(
        text(
            "SELECT event_type, previous_status, new_status FROM tiers.lifecycle_events "
            "WHERE tier_id = :i AND event_type = 'suspended'"
        ),
        {"i": tier.id},
    ).one()
    assert frise.previous_status == "actif"
    assert frise.new_status == "suspendu_temporaire"

    audit = db.execute(
        text(
            "SELECT user_id, action FROM audit.audit_logs "
            "WHERE resource_id = :i AND action = 'tier.suspended'"
        ),
        {"i": tier.id},
    ).one()
    assert audit.user_id == user.id
