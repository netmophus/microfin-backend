"""Restauration d'une fiche désactivée (R2) — le chemin de retour du soft delete.

  1. restore ramène desactive -> PROSPECT (jamais actif : re-KYC), lève deleted_at, efface
     l'activation ; la fiche réapparaît dans les listes normales.
  2. réservé à tiers.deactivate (responsable) — symétrie avec la désactivation.
  3. restaurer une fiche NON désactivée -> 409 (transition illégale).
  4. cloisonnement : on ne restaure pas une fiche d'une autre agence -> 404.
  5. GARDE D'UNICITÉ : si une pièce à numéro unique de la fiche a été reprise ailleurs pendant
     l'absence -> 422 (fiche nommée si dans le périmètre) ; la fiche RESTE désactivée, tracée.
  6. double trace : lifecycle_event 'restored' + audit tier.restored.
"""

import uuid
from collections.abc import Generator
from datetime import UTC, date, datetime

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
from app.modules.tiers.pieces import normaliser_numero

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


def _tier(
    db: Session,
    agence: Agency,
    *,
    statut: str = "prospect",
    desactive: bool = False,
    nom: str = "X",
) -> IndividualProfile:
    tier = IndividualProfile(
        tier_number=f"M-2999-{uuid.uuid4().int % 10_000_000:07d}",
        primary_agency_id=agence.id,
        last_name=nom,
        first_name="A",
        birth_date=date(1990, 5, 12),
        gender="M",
        nationality_id=_pays(db, "SN"),
        status=statut,
    )
    if desactive:
        tier.deleted_at = datetime.now(UTC)
        tier.activated_at = datetime.now(UTC)  # elle avait été activée avant d'être désactivée
    db.add(tier)
    db.flush()
    return tier


def _piece_orm(db: Session, tier_id: uuid.UUID, type_id: uuid.UUID, numero: str) -> None:
    db.add(
        IdentityDocument(
            tier_id=tier_id,
            document_type_id=type_id,
            document_number=numero,
            document_number_normalized=normaliser_numero(numero),
            is_primary=True,
        )
    )
    db.flush()


# --- retour nominal --------------------------------------------------------------------


def test_restaurer_ramene_en_prospect_et_reintegre_l_annuaire(
    db: Session, client: TestClient
) -> None:
    agence = _agence(db, "Agence Centre")
    resp = _utilisateur(db, "RESPONSABLE_AGENCE", agence)
    tier = _tier(db, agence, statut="desactive", desactive=True)
    entete = _entete(resp, "RESPONSABLE_AGENCE")

    reponse = client.post(
        f"/tiers/{tier.id}/restore", json={"motif": "Membre revenu"}, headers=entete
    )
    assert reponse.status_code == 200, reponse.text
    assert reponse.json()["status"] == "prospect"  # jamais actif : re-validation KYC

    # deleted_at levé + activation effacée.
    ligne = db.execute(
        text("SELECT deleted_at, activated_at FROM tiers.tiers WHERE id = CAST(:t AS uuid)"),
        {"t": str(tier.id)},
    ).one()
    assert ligne.deleted_at is None
    assert ligne.activated_at is None

    # Réapparaît dans la liste NORMALE (sans inclure_desactives).
    liste = client.get("/tiers", headers=entete).json()
    assert tier.tier_number in {ligne["tier_number"] for ligne in liste["lignes"]}


def test_la_restauration_ecrit_l_audit_et_la_frise(db: Session, client: TestClient) -> None:
    agence = _agence(db, "Agence Centre")
    resp = _utilisateur(db, "RESPONSABLE_AGENCE", agence)
    tier = _tier(db, agence, statut="desactive", desactive=True)

    client.post(f"/tiers/{tier.id}/restore", headers=_entete(resp, "RESPONSABLE_AGENCE"))

    audit = db.execute(
        text(
            "SELECT action FROM audit.audit_logs "
            "WHERE resource_id = CAST(:t AS uuid) AND action = 'tier.restored'"
        ),
        {"t": str(tier.id)},
    ).one()
    assert audit.action == "tier.restored"
    frise = db.execute(
        text(
            "SELECT new_status FROM tiers.lifecycle_events "
            "WHERE tier_id = CAST(:t AS uuid) AND event_type = 'restored'"
        ),
        {"t": str(tier.id)},
    ).one()
    assert frise.new_status == "prospect"


# --- gardes ----------------------------------------------------------------------------


def test_restaurer_exige_tiers_deactivate(db: Session, client: TestClient) -> None:
    agence = _agence(db, "Agence Centre")
    charge = _utilisateur(db, "CHARGE_CLIENTELE", agence)  # a suspend, PAS deactivate
    tier = _tier(db, agence, statut="desactive", desactive=True)

    reponse = client.post(f"/tiers/{tier.id}/restore", headers=_entete(charge, "CHARGE_CLIENTELE"))
    assert reponse.status_code == 403


def test_restaurer_une_fiche_non_desactivee_est_409(db: Session, client: TestClient) -> None:
    agence = _agence(db, "Agence Centre")
    resp = _utilisateur(db, "RESPONSABLE_AGENCE", agence)
    tier = _tier(db, agence, statut="prospect")  # pas désactivée

    reponse = client.post(f"/tiers/{tier.id}/restore", headers=_entete(resp, "RESPONSABLE_AGENCE"))
    assert reponse.status_code == 409, reponse.text


def test_cloisonnement_a_la_restauration(db: Session, client: TestClient) -> None:
    autre = _agence(db, "Agence voisine")
    mienne = _agence(db, "Mon agence")
    tier_voisin = _tier(db, autre, statut="desactive", desactive=True)
    resp = _utilisateur(db, "RESPONSABLE_AGENCE", mienne)

    reponse = client.post(
        f"/tiers/{tier_voisin.id}/restore", headers=_entete(resp, "RESPONSABLE_AGENCE")
    )
    assert reponse.status_code == 404  # hors périmètre -> 404, jamais 403


# --- garde d'unicité de pièce ----------------------------------------------------------


def test_restaurer_est_refuse_si_une_piece_a_ete_reprise_ailleurs(
    db: Session, client: TestClient
) -> None:
    """Le point dur : pendant l'absence, la CNI de la fiche désactivée a été enregistrée sur une
    autre fiche vivante. La restauration recréerait le doublon -> refus 422, fiche nommée."""
    agence = _agence(db, "Agence Centre")
    resp = _utilisateur(db, "RESPONSABLE_AGENCE", agence)
    cni = _type_piece(db, "CNI")

    dormante = _tier(db, agence, statut="desactive", desactive=True, nom="Reveille")
    _piece_orm(db, dormante.id, cni, "NER-777")
    # Pendant l'absence, la même CNI a été enregistrée sur une fiche vivante.
    active = _tier(db, agence, statut="prospect", nom="Doublon")
    _piece_orm(db, active.id, cni, "NER-777")

    reponse = client.post(
        f"/tiers/{dormante.id}/restore", headers=_entete(resp, "RESPONSABLE_AGENCE")
    )
    assert reponse.status_code == 422, reponse.text
    # Fiche en conflit nommée (dans le périmètre), comme en T2c.
    assert reponse.json()["detail"]["tier_id"] == str(active.id)

    # La fiche RESTE désactivée : la restauration a été refusée avant toute mutation.
    ligne = db.execute(
        text("SELECT deleted_at, status FROM tiers.tiers WHERE id = CAST(:t AS uuid)"),
        {"t": str(dormante.id)},
    ).one()
    assert ligne.deleted_at is not None
    assert ligne.status == "desactive"

    # La collision est tracée pour la conformité.
    audit = db.execute(
        text(
            "SELECT 1 FROM audit.audit_logs WHERE action = 'tier.identity.duplicate_blocked' "
            "AND resource_id = CAST(:t AS uuid)"
        ),
        {"t": str(dormante.id)},
    ).first()
    assert audit is not None
