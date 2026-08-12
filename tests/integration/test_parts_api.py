"""API Parts sociales (bloc 2) — souscription / libération, et la SÉPARATION DES POUVOIRS.

  - consultation GET /tiers/{id}/parts (tiers.shares.read) ;
  - souscription (engagement) -> tiers.shares.subscribe (chargé de clientèle) ;
  - souscription au comptant / libération -> tiers.shares.pay (caissier, encaissement) ;
  - un rôle qui n'a pas la bonne permission -> 403 (les actes sont distincts).
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.caisse.models import Poste, PosteAssignation
from app.modules.caisse.service import ouvrir_session
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.security.jwt import creer_access_token, decoder_access_token
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


def _cadre(db: Session, suffixe: str) -> tuple[Agency, uuid.UUID]:
    agence = Agency(code=f"AGPA-{suffixe}", name="Agence", compte_caisse_id=_cid(db, "101111"))
    db.add(agence)
    db.flush()
    tier_id = db.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES (:n, 'individual', :a, 'actif') RETURNING id"
        ),
        {"n": f"M-PA-{suffixe}", "a": agence.id},
    ).scalar_one()
    db.execute(text("DELETE FROM tiers.share_parameters"))
    db.execute(
        text(
            "INSERT INTO tiers.share_parameters "
            "(unit_value, minimum_shares, is_refundable, membership_on, "
            " compte_parts_liberees_id, compte_parts_non_liberees_id, is_provisional) "
            "VALUES (5000, 1, TRUE, 'liberation', "
            " (SELECT id FROM comptabilite.accounts WHERE account_number='571111'), "
            " (SELECT id FROM comptabilite.accounts WHERE account_number='571121'), TRUE)"
        )
    )
    return agence, tier_id


def _entete(db: Session, agence: Agency, role_code: str) -> dict[str, str]:
    role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
    s = uuid.uuid4().hex[:8]
    user = User(
        matricule=f"MAT-{s}", email=f"{s}@ex.com", username=f"u{s}",
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


def _ouvrir_session_caisse(db: Session, agence: Agency, entete: dict[str, str]) -> None:
    """Poste + session de caisse OUVERTE pour l'acteur de `entete` (Bloc C3) : la souscription
    au comptant exige désormais SA session, plus la seule agence — même compte que
    `Agency.compte_caisse_id`, miroir du backfill de la migration 0041."""
    jeton = entete["Authorization"].removeprefix("Bearer ")
    user_id = decoder_access_token(jeton).sub
    poste = Poste(
        agency_id=agence.id, code="01", libelle="Caisse principale",
        compte_caisse_id=agence.compte_caisse_id,
    )
    db.add(poste)
    db.flush()
    db.add(PosteAssignation(poste_id=poste.id, user_id=user_id))
    db.flush()
    ouvrir_session(
        db,
        UtilisateurCourant(
            user_id=user_id, roles=(), permissions=frozenset(),
            primary_agency_id=agence.id, agency_id=agence.id, voit_tout=True,
        ),
        poste_id=poste.id, fonds_initial=0,
    )


def test_comptant_par_caissier_rend_membre(client: TestClient, db: Session) -> None:
    agence, tier_id = _cadre(db, "C1")
    caissier = _entete(db, agence, "CAISSIER")  # a tiers.shares.pay
    _ouvrir_session_caisse(db, agence, caissier)

    reponse = client.post(
        f"/tiers/{tier_id}/parts/souscription-comptant", json={"shares_count": 10}, headers=caissier
    )
    assert reponse.status_code == 200
    assert reponse.json()["is_member"] is True
    assert reponse.json()["shares_liberees"] == 10


def test_souscription_engagement_par_charge(client: TestClient, db: Session) -> None:
    agence, tier_id = _cadre(db, "E1")
    charge = _entete(db, agence, "CHARGE_CLIENTELE")  # a tiers.shares.subscribe

    reponse = client.post(
        f"/tiers/{tier_id}/parts/souscription", json={"shares_count": 4}, headers=charge
    )
    assert reponse.status_code == 200
    # Engagement sans paiement : pas encore membre (membership_on = liberation).
    assert reponse.json()["is_member"] is False
    assert reponse.json()["shares_non_liberees"] == 4


def test_caissier_ne_peut_pas_souscrire_engagement_403(client: TestClient, db: Session) -> None:
    agence, tier_id = _cadre(db, "X1")
    caissier = _entete(db, agence, "CAISSIER")  # pay mais PAS subscribe

    reponse = client.post(
        f"/tiers/{tier_id}/parts/souscription", json={"shares_count": 4}, headers=caissier
    )
    assert reponse.status_code == 403


def test_charge_ne_peut_pas_encaisser_403(client: TestClient, db: Session) -> None:
    agence, tier_id = _cadre(db, "X2")
    charge = _entete(db, agence, "CHARGE_CLIENTELE")  # subscribe mais PAS pay

    reponse = client.post(
        f"/tiers/{tier_id}/parts/souscription-comptant", json={"shares_count": 4}, headers=charge
    )
    assert reponse.status_code == 403


def test_liberation_par_caissier_apres_engagement_rend_membre(
    client: TestClient, db: Session
) -> None:
    agence, tier_id = _cadre(db, "L1")
    charge = _entete(db, agence, "CHARGE_CLIENTELE")
    caissier = _entete(db, agence, "CAISSIER")

    client.post(f"/tiers/{tier_id}/parts/souscription", json={"shares_count": 3}, headers=charge)
    reponse = client.post(
        f"/tiers/{tier_id}/parts/liberation", json={"shares_count": 3}, headers=caissier
    )
    assert reponse.status_code == 200
    assert reponse.json()["is_member"] is True  # membre à la libération
    assert reponse.json()["shares_liberees"] == 3


def test_consultation_par_charge(client: TestClient, db: Session) -> None:
    agence, tier_id = _cadre(db, "R1")
    charge = _entete(db, agence, "CHARGE_CLIENTELE")  # a tiers.shares.read

    reponse = client.get(f"/tiers/{tier_id}/parts", headers=charge)
    assert reponse.status_code == 200
    assert reponse.json()["unit_value"] == 5000
    assert reponse.json()["shares_liberees"] == 0


def test_remboursement_total_par_responsable_redevient_client(
    client: TestClient, db: Session
) -> None:
    agence, tier_id = _cadre(db, "RB")
    resp = _entete(db, agence, "RESPONSABLE_AGENCE")  # a pay + refund
    _ouvrir_session_caisse(db, agence, resp)
    client.post(
        f"/tiers/{tier_id}/parts/souscription-comptant", json={"shares_count": 10}, headers=resp
    )

    reponse = client.post(
        f"/tiers/{tier_id}/parts/remboursement", json={"shares_count": 10}, headers=resp
    )
    assert reponse.status_code == 200
    assert reponse.json()["is_member"] is False  # redevient client
    assert reponse.json()["shares_liberees"] == 0


def test_engagements_desactivation_avant_apres_remboursement(
    client: TestClient, db: Session
) -> None:
    """L'écran lit les blocages AVANT de proposer « Désactiver » : parts détenues -> liste non
    vide (message actionnable avec le capital en francs) ; après remboursement total -> vide."""
    agence, tier_id = _cadre(db, "EN")
    resp = _entete(db, agence, "RESPONSABLE_AGENCE")  # a tiers.deactivate + pay + refund
    _ouvrir_session_caisse(db, agence, resp)
    client.post(
        f"/tiers/{tier_id}/parts/souscription-comptant", json={"shares_count": 10}, headers=resp
    )

    avant = client.get(f"/tiers/{tier_id}/deactivation-engagements", headers=resp)
    assert avant.status_code == 200
    (engagement,) = [e for e in avant.json() if e["domaine"] == "parts_sociales"]
    # Actionnable : le capital en francs ET quoi faire.
    assert "50 000 F" in engagement["libelle"]
    assert "Remboursez" in engagement["libelle"]

    client.post(f"/tiers/{tier_id}/parts/remboursement", json={"shares_count": 10}, headers=resp)
    apres = client.get(f"/tiers/{tier_id}/deactivation-engagements", headers=resp)
    assert [e for e in apres.json() if e["domaine"] == "parts_sociales"] == []


def test_charge_ne_peut_pas_rembourser_403(client: TestClient, db: Session) -> None:
    agence, tier_id = _cadre(db, "RX")
    resp = _entete(db, agence, "RESPONSABLE_AGENCE")
    charge = _entete(db, agence, "CHARGE_CLIENTELE")  # PAS de refund
    _ouvrir_session_caisse(db, agence, resp)
    client.post(
        f"/tiers/{tier_id}/parts/souscription-comptant", json={"shares_count": 5}, headers=resp
    )

    reponse = client.post(
        f"/tiers/{tier_id}/parts/remboursement", json={"shares_count": 5}, headers=charge
    )
    assert reponse.status_code == 403
