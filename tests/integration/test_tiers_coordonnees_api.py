"""Coordonnées des tiers (T2b) — normalisation, échappatoire tracée, périmètre, double trace.

Ce que ces tests protègent, par ordre d'importance :

  1. L'ÉCHAPPATOIRE FONCTIONNE ET SE MESURE. Un numéro que la bibliothèque refuse est refusé
     par défaut (422, avec `forcable` pour guider l'écran) ; forcé, il s'enregistre MAIS avec
     phone_normalized=false — un forçage jamais mesuré cesse d'être un garde-fou. Le charabia
     (< 6 chiffres) reste refusé même forcé.
  2. LE TÉLÉPHONE DE CRÉATION DEVIENT UN CONTACT. primary_phone n'est plus écrit sur la fiche :
     il part en contact téléphone principal, et la fiche le relit depuis les contacts.
  3. PÉRIMÈTRE : les coordonnées d'une fiche d'une autre agence -> 404, jamais 403.
  4. DOUBLE TRACE : ajouter une coordonnée écrit un lifecycle_event 'updated' ET un audit
     tier.contact_added.
  5. SUPPRESSION LOGIQUE AVEC MOTIF, et set-primary débascule l'ancien principal.
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


def _tier(db: Session, agence: Agency, **extra: object) -> IndividualProfile:
    tier = IndividualProfile(
        tier_number=f"M-2999-{uuid.uuid4().int % 10_000_000:07d}",
        primary_agency_id=agence.id,
        last_name="Diallo",
        first_name="Amadou",
        birth_date=date(1990, 5, 12),
        gender="M",
        nationality_id=_pays(db, "SN"),
        **extra,
    )
    db.add(tier)
    db.flush()
    return tier


# --- normalisation et échappatoire -----------------------------------------------------


def test_un_numero_valide_est_normalise_en_e164(db: Session, client: TestClient) -> None:
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "CHARGE_CLIENTELE", agence)
    tier = _tier(db, agence)

    reponse = client.post(
        f"/tiers/{tier.id}/phones",
        json={"phone": "90 12 34 56", "is_primary": True},
        headers=_entete(user, "CHARGE_CLIENTELE"),
    )

    assert reponse.status_code == 201, reponse.text
    corps = reponse.json()
    assert corps["phone_number"] == "+22790123456"  # normalisé, indicatif Niger déduit
    assert corps["phone_normalized"] is True
    assert corps["is_primary"] is True


def test_un_numero_refuse_renvoie_422_forcable(db: Session, client: TestClient) -> None:
    """La bibliothèque refuse ce numéro : 422 par défaut, avec forcable=true pour proposer
    « enregistrer quand même » (bonne longueur, juste non reconnu)."""
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "CHARGE_CLIENTELE", agence)
    tier = _tier(db, agence)

    reponse = client.post(
        f"/tiers/{tier.id}/phones",
        json={"phone": "999999999999"},
        headers=_entete(user, "CHARGE_CLIENTELE"),
    )

    assert reponse.status_code == 422, reponse.text
    assert reponse.json()["detail"]["forcable"] is True


def test_le_forcage_enregistre_le_numero_mais_le_marque_non_normalise(
    db: Session, client: TestClient
) -> None:
    """Le point dur : l'échappatoire enregistre le numéro refusé, MAIS phone_normalized=false —
    ce drapeau rend le forçage mesurable (le futur Décisionnel suivra le « % de forcés »)."""
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "CHARGE_CLIENTELE", agence)
    tier = _tier(db, agence)

    reponse = client.post(
        f"/tiers/{tier.id}/phones",
        json={"phone": "999999999999", "forcer": True},
        headers=_entete(user, "CHARGE_CLIENTELE"),
    )

    assert reponse.status_code == 201, reponse.text
    assert reponse.json()["phone_normalized"] is False  # le forçage laisse sa trace, mesurable

    # Et la trace est en base, pas seulement dans la réponse.
    stocke = db.execute(
        text("SELECT phone_normalized FROM tiers.contacts WHERE tier_id = :t"), {"t": tier.id}
    ).scalar_one()
    assert stocke is False


def test_le_charabia_est_refuse_meme_force(db: Session, client: TestClient) -> None:
    """Le garde-fou ≥ 6 chiffres : forcer ne fait pas passer n'importe quoi. forcable=false ->
    l'écran ne doit même pas proposer « enregistrer quand même »."""
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "CHARGE_CLIENTELE", agence)
    tier = _tier(db, agence)

    reponse = client.post(
        f"/tiers/{tier.id}/phones",
        json={"phone": "12345", "forcer": True},
        headers=_entete(user, "CHARGE_CLIENTELE"),
    )

    assert reponse.status_code == 422, reponse.text
    assert reponse.json()["detail"]["forcable"] is False


# --- transition primary_phone : de la colonne vers un contact --------------------------


def test_le_telephone_de_creation_devient_un_contact_principal(
    db: Session, client: TestClient
) -> None:
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "CHARGE_CLIENTELE", agence)

    creation = client.post(
        "/tiers/individuals",
        json={
            "last_name": "Ba",
            "first_name": "Fatou",
            "birth_date": "1985-02-03",
            "gender": "F",
            "nationality_id": str(_pays(db, "SN")),
            "primary_phone": "90123456",
        },
        headers=_entete(user, "CHARGE_CLIENTELE"),
    )
    assert creation.status_code == 201, creation.text
    tid = creation.json()["id"]

    # La colonne legacy n'est plus écrite : le numéro vit dans un contact téléphone principal.
    colonne = db.execute(
        text("SELECT primary_phone FROM tiers.tiers WHERE id = CAST(:t AS uuid)"), {"t": tid}
    ).scalar_one()
    assert colonne is None

    contacts = client.get(f"/tiers/{tid}/contacts", headers=_entete(user, "CHARGE_CLIENTELE"))
    principaux = [c for c in contacts.json() if c["contact_type"] == "phone" and c["is_primary"]]
    assert len(principaux) == 1
    assert principaux[0]["phone_number"] == "+22790123456"

    # Et la fiche relit ce numéro depuis les contacts.
    fiche = client.get(f"/tiers/{tid}", headers=_entete(user, "CHARGE_CLIENTELE"))
    assert fiche.json()["primary_phone"] == "+22790123456"


# --- périmètre -------------------------------------------------------------------------


def test_les_coordonnees_hors_perimetre_sont_404(db: Session, client: TestClient) -> None:
    autre = _agence(db, "Agence voisine")
    mienne = _agence(db, "Mon agence")
    tier_voisin = _tier(db, autre)  # fiche d'une autre agence
    intrus = _utilisateur(db, "CHARGE_CLIENTELE", mienne)

    lecture = client.get(
        f"/tiers/{tier_voisin.id}/contacts", headers=_entete(intrus, "CHARGE_CLIENTELE")
    )
    assert lecture.status_code == 404, lecture.text

    ajout = client.post(
        f"/tiers/{tier_voisin.id}/phones",
        json={"phone": "90123456"},
        headers=_entete(intrus, "CHARGE_CLIENTELE"),
    )
    assert ajout.status_code == 404, ajout.text  # 404, jamais 403 : « n'existe pas pour moi »


# --- double trace ----------------------------------------------------------------------


def test_ajouter_une_coordonnee_ecrit_l_audit_et_la_frise(
    db: Session, client: TestClient
) -> None:
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "CHARGE_CLIENTELE", agence)
    tier = _tier(db, agence)

    reponse = client.post(
        f"/tiers/{tier.id}/phones",
        json={"phone": "90123456"},
        headers=_entete(user, "CHARGE_CLIENTELE"),
    )
    cid = reponse.json()["id"]

    audit = db.execute(
        text(
            "SELECT user_id, resource_type, action FROM audit.audit_logs "
            "WHERE resource_id = CAST(:rid AS uuid) AND action = 'tier.contact_added'"
        ),
        {"rid": cid},
    ).one()
    assert audit.user_id == user.id
    assert audit.resource_type == "contact"

    frise = db.execute(
        text(
            "SELECT event_type FROM tiers.lifecycle_events "
            "WHERE tier_id = CAST(:t AS uuid) AND event_type = 'updated'"
        ),
        {"t": tier.id},
    ).one()
    assert frise.event_type == "updated"


# --- suppression logique et principal --------------------------------------------------


def test_la_suppression_est_logique_et_garde_le_motif(db: Session, client: TestClient) -> None:
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "CHARGE_CLIENTELE", agence)
    tier = _tier(db, agence)
    ajout = client.post(
        f"/tiers/{tier.id}/phones",
        json={"phone": "90123456"},
        headers=_entete(user, "CHARGE_CLIENTELE"),
    )
    cid = ajout.json()["id"]

    suppression = client.request(
        "DELETE",
        f"/tiers/{tier.id}/contacts/{cid}",
        json={"motif": "Numéro erroné signalé par le client"},
        headers=_entete(user, "CHARGE_CLIENTELE"),
    )
    assert suppression.status_code == 204, suppression.text

    # Sortie des listes...
    restants = client.get(f"/tiers/{tier.id}/contacts", headers=_entete(user, "CHARGE_CLIENTELE"))
    assert restants.json() == []

    # ...mais jamais effacée : deleted_at et motif conservés.
    ligne = db.execute(
        text("SELECT deleted_at, deletion_reason FROM tiers.contacts WHERE id = CAST(:c AS uuid)"),
        {"c": cid},
    ).one()
    assert ligne.deleted_at is not None
    assert ligne.deletion_reason == "Numéro erroné signalé par le client"


def test_definir_principal_debascule_l_ancien(db: Session, client: TestClient) -> None:
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "CHARGE_CLIENTELE", agence)
    tier = _tier(db, agence)
    entete = _entete(user, "CHARGE_CLIENTELE")

    premier = client.post(
        f"/tiers/{tier.id}/phones",
        json={"phone": "90123456", "is_primary": True},
        headers=entete,
    ).json()
    second = client.post(
        f"/tiers/{tier.id}/phones",
        json={"phone": "91234567", "is_primary": False},
        headers=entete,
    ).json()

    bascule = client.post(
        f"/tiers/{tier.id}/contacts/{second['id']}/set-primary", headers=entete
    )
    assert bascule.status_code == 200, bascule.text
    assert bascule.json()["is_primary"] is True

    # L'ancien principal ne l'est plus : une seule principale par type (l'index partiel veille).
    contacts = {c["id"]: c for c in client.get(f"/tiers/{tier.id}/contacts", headers=entete).json()}
    assert contacts[premier["id"]]["is_primary"] is False
    assert contacts[second["id"]]["is_primary"] is True


# --- email et adresse rurale -----------------------------------------------------------


def test_email_et_adresse_au_repere_seul(db: Session, client: TestClient) -> None:
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "CHARGE_CLIENTELE", agence)
    tier = _tier(db, agence)
    entete = _entete(user, "CHARGE_CLIENTELE")

    email = client.post(
        f"/tiers/{tier.id}/emails",
        json={"email": "Fatou.BA@example.com", "is_primary": True},
        headers=entete,
    )
    assert email.status_code == 201, email.text
    assert email.json()["email_address"] == "Fatou.BA@example.com"

    # Zone rurale : un point de repère seul suffit, aucune rue.
    adresse = client.post(
        f"/tiers/{tier.id}/addresses",
        json={"landmark": "Derrière la mosquée, près du puits", "is_primary": True},
        headers=entete,
    )
    assert adresse.status_code == 201, adresse.text
    assert adresse.json()["landmark"].startswith("Derrière la mosquée")


def test_une_adresse_vide_est_refusee_en_422(db: Session, client: TestClient) -> None:
    agence = _agence(db, "Agence Centre")
    user = _utilisateur(db, "CHARGE_CLIENTELE", agence)
    tier = _tier(db, agence)

    reponse = client.post(
        f"/tiers/{tier.id}/addresses",
        json={"quarter": "Plateau"},  # ni rue ni repère
        headers=_entete(user, "CHARGE_CLIENTELE"),
    )
    assert reponse.status_code == 422, reponse.text
