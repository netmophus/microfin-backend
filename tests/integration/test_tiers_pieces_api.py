"""Pièces d'identité (T2c) — unicité conditionnelle cloisonnée, validité calculée, vérification.

Ce que ces tests protègent, par ordre d'importance :

  1. UNICITÉ CONDITIONNELLE, EXPOSITION CLOISONNÉE. Un numéro à unicité (CNI) déjà pris sur une
     AUTRE fiche est refusé (422) ; la fiche est nommée si elle est dans mon périmètre, le refus
     est strictement générique sinon — et la collision est tracée dans l'audit dans les deux cas.
     Un type SANS unicité (attestation) tolère le doublon.
  2. VALIDITÉ CALCULÉE, JAMAIS STOCKÉE. Une pièce périmée passe (l'agent constate) ; son état
     (valide / expire_bientot / perimee / sans_objet) est dérivé de la date du jour, sans job.
  3. VÉRIFIER EST UN ACTE RÉSERVÉ. tiers.identity.verify (responsable/LBC), pas la saisie.
  4. SUPPRESSION LOGIQUE AVEC MOTIF ; refus de retirer la principale s'il en reste d'autres.
"""

import uuid
from collections.abc import Generator
from datetime import date, timedelta

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
from app.modules.tiers.models import IdentityDocument, IndividualProfile
from app.modules.tiers.pieces import etat_validite, normaliser_numero

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
    return db.execute(
        text("SELECT id FROM parameters.countries WHERE code = :c"), {"c": code}
    ).scalar_one()


def _type_piece(db: Session, code: str) -> uuid.UUID:
    return db.execute(
        text("SELECT id FROM parameters.identity_document_types WHERE code = :c"), {"c": code}
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


def _tier(db: Session, agence: Agency, last_name: str = "Diallo") -> IndividualProfile:
    tier = IndividualProfile(
        tier_number=f"M-2999-{uuid.uuid4().int % 10_000_000:07d}",
        primary_agency_id=agence.id,
        last_name=last_name,
        first_name="Amadou",
        birth_date=date(1990, 5, 12),
        gender="M",
        nationality_id=_pays(db, "SN"),
    )
    db.add(tier)
    db.flush()
    return tier


def _piece_orm(
    db: Session, tier_id: uuid.UUID, type_id: uuid.UUID, numero: str
) -> IdentityDocument:
    """Pose une pièce directement en base (le service n'est pas le sujet du test)."""
    piece = IdentityDocument(
        tier_id=tier_id,
        document_type_id=type_id,
        document_number=numero,
        document_number_normalized=normaliser_numero(numero),
        is_primary=True,
    )
    db.add(piece)
    db.flush()
    return piece


# --- validité calculée (unité) ---------------------------------------------------------


def test_la_validite_est_calculee_depuis_la_date_du_jour() -> None:
    jour = date(2026, 7, 23)
    assert etat_validite(None, aujourdhui=jour) == "sans_objet"
    assert etat_validite(date(2025, 1, 1), aujourdhui=jour) == "perimee"
    assert etat_validite(jour, aujourdhui=jour) == "expire_bientot"  # valide LE jour même
    assert etat_validite(jour + timedelta(days=30), aujourdhui=jour) == "expire_bientot"
    assert etat_validite(jour + timedelta(days=200), aujourdhui=jour) == "valide"


# --- saisie et validité via l'API ------------------------------------------------------


def test_une_piece_perimee_est_acceptee_et_signalee(db: Session, client: TestClient) -> None:
    """L'agent constate ce qu'il a : une CNI périmée s'enregistre, marquée 'perimee'."""
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "CHARGE_CLIENTELE", agence)
    tier = _tier(db, agence)

    reponse = client.post(
        f"/tiers/{tier.id}/identity-documents",
        json={
            "document_type_id": str(_type_piece(db, "CNI")),
            "document_number": "NER-0001",
            "expiry_date": "2020-01-01",
            "is_primary": True,
        },
        headers=_entete(user, "CHARGE_CLIENTELE"),
    )

    assert reponse.status_code == 201, reponse.text
    assert reponse.json()["validite"] == "perimee"  # signalée, pas refusée


# --- unicité conditionnelle ------------------------------------------------------------


def test_un_doublon_de_cni_dans_mon_agence_nomme_la_fiche(db: Session, client: TestClient) -> None:
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "CHARGE_CLIENTELE", agence)
    deja = _tier(db, agence, last_name="Traore")
    _piece_orm(db, deja.id, _type_piece(db, "CNI"), "NER-123")
    nouveau = _tier(db, agence, last_name="Sow")

    reponse = client.post(
        f"/tiers/{nouveau.id}/identity-documents",
        json={"document_type_id": str(_type_piece(db, "CNI")), "document_number": "NER-123"},
        headers=_entete(user, "CHARGE_CLIENTELE"),
    )

    assert reponse.status_code == 422, reponse.text
    detail = reponse.json()["detail"]
    assert deja.tier_number in detail and "Traore" in detail  # nommée : l'agent peut résoudre


def test_un_doublon_hors_perimetre_reste_generique_et_est_audite(
    db: Session, client: TestClient
) -> None:
    autre = _agence(db, "Agence voisine")
    mienne = _agence(db, "Mon agence")
    fiche_voisine = _tier(db, autre, last_name="Kone")
    _piece_orm(db, fiche_voisine.id, _type_piece(db, "CNI"), "NER-999")
    ma_fiche = _tier(db, mienne, last_name="Ba")
    user = _utilisateur(db, "CHARGE_CLIENTELE", mienne)

    reponse = client.post(
        f"/tiers/{ma_fiche.id}/identity-documents",
        json={"document_type_id": str(_type_piece(db, "CNI")), "document_number": "NER-999"},
        headers=_entete(user, "CHARGE_CLIENTELE"),
    )

    assert reponse.status_code == 422, reponse.text
    detail = reponse.json()["detail"]
    # Rien ne fuite : ni le numéro de la fiche voisine, ni le nom, ni l'agence.
    assert fiche_voisine.tier_number not in detail
    assert "Kone" not in detail

    # Mais la collision est tracée pour la conformité (piste privilégiée, réseau).
    audit = db.execute(
        text(
            "SELECT new_values FROM audit.audit_logs "
            "WHERE action = 'tier.identity.duplicate_blocked' "
            "AND resource_id = CAST(:rid AS uuid)"
        ),
        {"rid": str(ma_fiche.id)},
    ).one()
    assert str(fiche_voisine.id) in str(audit.new_values)


def test_la_normalisation_rattrape_les_variantes_d_espaces(
    db: Session, client: TestClient
) -> None:
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "CHARGE_CLIENTELE", agence)
    deja = _tier(db, agence, last_name="Cisse")
    _piece_orm(db, deja.id, _type_piece(db, "CNI"), "AB1234")
    nouveau = _tier(db, agence, last_name="Diop")

    # « ab 12 34 » est le même numéro que « AB1234 » -> doublon détecté.
    reponse = client.post(
        f"/tiers/{nouveau.id}/identity-documents",
        json={"document_type_id": str(_type_piece(db, "CNI")), "document_number": "ab 12 34"},
        headers=_entete(user, "CHARGE_CLIENTELE"),
    )
    assert reponse.status_code == 422, reponse.text


def test_un_type_sans_unicite_tolere_le_doublon(db: Session, client: TestClient) -> None:
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "CHARGE_CLIENTELE", agence)
    deja = _tier(db, agence, last_name="Barry")
    attestation = _type_piece(db, "ATTESTATION_NAISSANCE")  # enforce_unique = False
    _piece_orm(db, deja.id, attestation, "0001")
    nouveau = _tier(db, agence, last_name="Balde")

    reponse = client.post(
        f"/tiers/{nouveau.id}/identity-documents",
        json={"document_type_id": str(attestation), "document_number": "0001"},
        headers=_entete(user, "CHARGE_CLIENTELE"),
    )
    assert reponse.status_code == 201, reponse.text  # même numéro d'ordre toléré


# --- vérification : acte réservé -------------------------------------------------------


def test_le_charge_de_clientele_ne_peut_pas_verifier(db: Session, client: TestClient) -> None:
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "CHARGE_CLIENTELE", agence)
    tier = _tier(db, agence)
    piece = _piece_orm(db, tier.id, _type_piece(db, "CNI"), "NER-1")

    reponse = client.post(
        f"/tiers/{tier.id}/identity-documents/{piece.id}/verify",
        headers=_entete(user, "CHARGE_CLIENTELE"),
    )
    assert reponse.status_code == 403  # saisir oui, attester non


def test_le_responsable_verifie_la_piece(db: Session, client: TestClient) -> None:
    agence = _agence(db, "Agence Centre")
    resp = _utilisateur(db, "RESPONSABLE_AGENCE", agence)
    tier = _tier(db, agence)
    piece = _piece_orm(db, tier.id, _type_piece(db, "CNI"), "NER-2")

    reponse = client.post(
        f"/tiers/{tier.id}/identity-documents/{piece.id}/verify",
        json={"notes": "CNI vue en original, conforme"},
        headers=_entete(resp, "RESPONSABLE_AGENCE"),
    )
    assert reponse.status_code == 200, reponse.text
    corps = reponse.json()
    assert corps["is_verified"] is True
    assert corps["verified_at"] is not None
    assert corps["verification_notes"].startswith("CNI vue")


# --- suppression logique et principale -------------------------------------------------


def test_supprimer_la_principale_est_refuse_s_il_en_reste(db: Session, client: TestClient) -> None:
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "CHARGE_CLIENTELE", agence)
    tier = _tier(db, agence)
    entete = _entete(user, "CHARGE_CLIENTELE")
    principale = client.post(
        f"/tiers/{tier.id}/identity-documents",
        json={"document_type_id": str(_type_piece(db, "CNI")), "document_number": "P-1",
              "is_primary": True},
        headers=entete,
    ).json()
    client.post(
        f"/tiers/{tier.id}/identity-documents",
        json={"document_type_id": str(_type_piece(db, "PASSPORT")), "document_number": "P-2"},
        headers=entete,
    )

    refus = client.request(
        "DELETE",
        f"/tiers/{tier.id}/identity-documents/{principale['id']}",
        json={"motif": "erreur"},
        headers=entete,
    )
    assert refus.status_code == 409, refus.text  # désigner d'abord une autre principale


def test_la_suppression_est_logique_et_garde_le_motif(db: Session, client: TestClient) -> None:
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "CHARGE_CLIENTELE", agence)
    tier = _tier(db, agence)
    entete = _entete(user, "CHARGE_CLIENTELE")
    piece = client.post(
        f"/tiers/{tier.id}/identity-documents",
        json={"document_type_id": str(_type_piece(db, "CNI")), "document_number": "P-9",
              "is_primary": True},
        headers=entete,
    ).json()

    # Seule pièce, même principale : la suppression passe.
    suppression = client.request(
        "DELETE",
        f"/tiers/{tier.id}/identity-documents/{piece['id']}",
        json={"motif": "Doublon d'une autre pièce"},
        headers=entete,
    )
    assert suppression.status_code == 204, suppression.text

    restantes = client.get(f"/tiers/{tier.id}/identity-documents", headers=entete)
    assert restantes.json() == []

    ligne = db.execute(
        text(
            "SELECT deleted_at, deletion_reason FROM tiers.identity_documents "
            "WHERE id = CAST(:c AS uuid)"
        ),
        {"c": piece["id"]},
    ).one()
    assert ligne.deleted_at is not None
    assert ligne.deletion_reason == "Doublon d'une autre pièce"


# --- périmètre -------------------------------------------------------------------------


def test_les_pieces_hors_perimetre_sont_404(db: Session, client: TestClient) -> None:
    autre = _agence(db, "Agence voisine")
    mienne = _agence(db, "Mon agence")
    tier_voisin = _tier(db, autre)
    intrus = _utilisateur(db, "CHARGE_CLIENTELE", mienne)

    lecture = client.get(
        f"/tiers/{tier_voisin.id}/identity-documents",
        headers=_entete(intrus, "CHARGE_CLIENTELE"),
    )
    assert lecture.status_code == 404, lecture.text
