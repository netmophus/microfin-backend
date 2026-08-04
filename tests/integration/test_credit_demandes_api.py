"""API Crédit (CR1) — demande et décision.

  - création : gate KYC (tiers 'actif'), VU MORDRE via le service ET via le trigger réel (pas un
    stub) ; produit inactif -> 422 ; hors périmètre -> 404 ; numéro atomique CR-AAAA-NNNNNNN ;
  - décision : motif obligatoire dans LES DEUX sens ; montant décidé <= montant demandé ;
    décision FIGÉE (pas de redécision) ; APPROBATION re-vérifie le tiers (refus jamais bloqué,
    même pour un tiers devenu non-actif ENTRE la création et la décision) ;
  - permissions : create/read/decide, 403 sinon.
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.credit.demandes import creer_demande
from app.modules.credit.models import Product
from app.modules.parameters.models import Agency
from app.modules.security.jwt import creer_access_token
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


def _agence(db: Session, code: str) -> Agency:
    agence = Agency(code=code, name=f"Agence {code}", compte_caisse_id=_cid(db, "101111"))
    db.add(agence)
    db.flush()
    return agence


def _tier(db: Session, agence: Agency, statut: str = "actif") -> uuid.UUID:
    tier_id = db.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES (:n, 'individual', :a, :s) RETURNING id"
        ),
        {"n": f"M-CR-{uuid.uuid4().hex[:6]}", "a": agence.id, "s": statut},
    ).scalar_one()
    nat = db.execute(text("SELECT id FROM parameters.countries LIMIT 1")).scalar_one()
    db.execute(
        text(
            "INSERT INTO tiers.individual_profiles "
            "(tier_id, last_name, first_name, birth_date, gender, nationality_id) "
            "VALUES (:t, 'Diallo', 'Amadou', '1985-01-01', 'M', :nat)"
        ),
        {"t": tier_id, "nat": nat},
    )
    return tier_id


def _produit(db: Session) -> Product:
    produit = Product(
        code=f"CRT{uuid.uuid4().hex[:5]}",
        name="Crédit test",
        compte_credit_membre_id=_cid(db, "202211"),
        compte_credit_client_id=_cid(db, "202221"),
    )
    db.add(produit)
    db.flush()
    return produit


def _entete(db: Session, agence: Agency, role_code: str) -> dict[str, str]:
    role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
    suffixe = uuid.uuid4().hex[:8]
    user = User(
        matricule=f"MAT-{suffixe}", email=f"{suffixe}@ex.com", username=f"u{suffixe}",
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


# --- Création ------------------------------------------------------------------------------


def test_creer_demande_reussit_avec_numero_atomique(client: TestClient, db: Session) -> None:
    agence = _agence(db, "CRA1")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    charge = _entete(db, agence, "CHARGE_PRET")

    reponse = client.post(
        f"/tiers/{tier_id}/demandes-credit",
        json={"product_id": str(produit.id), "montant_demande": 500000, "duree_echeances": 12},
        headers=charge,
    )
    assert reponse.status_code == 201
    corps = reponse.json()
    assert corps["status"] == "en_instruction"
    assert corps["application_number"].startswith("CR-")
    assert corps["montant_demande"] == 500000


def test_creer_demande_tiers_non_actif_422(client: TestClient, db: Session) -> None:
    agence = _agence(db, "CRA2")
    tier_id = _tier(db, agence, statut="prospect")
    produit = _produit(db)
    charge = _entete(db, agence, "CHARGE_PRET")

    reponse = client.post(
        f"/tiers/{tier_id}/demandes-credit",
        json={"product_id": str(produit.id), "montant_demande": 500000, "duree_echeances": 12},
        headers=charge,
    )
    assert reponse.status_code == 422
    assert "actif" in reponse.json()["detail"].lower()


def test_trigger_creation_mord_reellement_pas_seulement_le_service(db: Session) -> None:
    """Le GARDE-FOU EN BASE (dernier rempart) refuse une insertion directe, même en
    contournant le service — pas un mécanisme vert mais inerte."""
    agence = _agence(db, "CRA3")
    tier_id = _tier(db, agence, statut="suspendu_temporaire")
    produit = _produit(db)

    with pytest.raises(DBAPIError):
        db.execute(
            text(
                "INSERT INTO credit.applications "
                "(application_number, tier_id, agency_id, product_id, montant_demande, "
                " duree_echeances, status) "
                "VALUES ('CR-TEST-0000001', :t, :a, :p, 100000, 12, 'en_instruction')"
            ),
            {"t": tier_id, "a": agence.id, "p": produit.id},
        )


def test_creer_demande_produit_inactif_422(client: TestClient, db: Session) -> None:
    agence = _agence(db, "CRA4")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    produit.is_active = False
    db.flush()
    charge = _entete(db, agence, "CHARGE_PRET")

    reponse = client.post(
        f"/tiers/{tier_id}/demandes-credit",
        json={"product_id": str(produit.id), "montant_demande": 500000, "duree_echeances": 12},
        headers=charge,
    )
    assert reponse.status_code == 422


def test_creer_demande_tier_hors_agence_404(client: TestClient, db: Session) -> None:
    agence_tier = _agence(db, "CRA5")
    autre_agence = _agence(db, "CRA6")
    tier_id = _tier(db, agence_tier)
    produit = _produit(db)
    charge = _entete(db, autre_agence, "CHARGE_PRET")  # dans une AUTRE agence

    reponse = client.post(
        f"/tiers/{tier_id}/demandes-credit",
        json={"product_id": str(produit.id), "montant_demande": 500000, "duree_echeances": 12},
        headers=charge,
    )
    assert reponse.status_code == 404


def test_creer_demande_sans_permission_403(client: TestClient, db: Session) -> None:
    agence = _agence(db, "CRA7")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    caissier = _entete(db, agence, "CAISSIER")

    reponse = client.post(
        f"/tiers/{tier_id}/demandes-credit",
        json={"product_id": str(produit.id), "montant_demande": 500000, "duree_echeances": 12},
        headers=caissier,
    )
    assert reponse.status_code == 403


# --- Décision --------------------------------------------------------------------------------


def _demande(db: Session, agence: Agency, tier_id: uuid.UUID, produit: Product) -> uuid.UUID:
    application = creer_demande(
        db,
        tier_id=tier_id,
        agency_id=agence.id,
        product_id=produit.id,
        montant_demande=500000,
        duree_echeances=12,
        objet="Test",
        par=None,
    )
    db.commit()
    return application.id


def test_decision_approuve_avec_montant_reduit(client: TestClient, db: Session) -> None:
    agence = _agence(db, "CRD1")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande_id = _demande(db, agence, tier_id, produit)
    comite = _entete(db, agence, "MEMBRE_COMITE_CREDIT")

    reponse = client.post(
        f"/credit/demandes/{demande_id}/decision",
        json={"decision": "approuve", "montant_decide": 400000, "motif": "Capacité limitée"},
        headers=comite,
    )
    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["status"] == "approuve"
    assert corps["montant_decide"] == 400000


def test_decision_refuse_avec_motif(client: TestClient, db: Session) -> None:
    agence = _agence(db, "CRD2")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande_id = _demande(db, agence, tier_id, produit)
    comite = _entete(db, agence, "MEMBRE_COMITE_CREDIT")

    reponse = client.post(
        f"/credit/demandes/{demande_id}/decision",
        json={"decision": "refuse", "motif": "Garanties insuffisantes"},
        headers=comite,
    )
    assert reponse.status_code == 200
    assert reponse.json()["status"] == "refuse"
    assert reponse.json()["montant_decide"] is None


def test_decision_montant_superieur_au_demande_422(client: TestClient, db: Session) -> None:
    agence = _agence(db, "CRD3")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande_id = _demande(db, agence, tier_id, produit)
    comite = _entete(db, agence, "MEMBRE_COMITE_CREDIT")

    reponse = client.post(
        f"/credit/demandes/{demande_id}/decision",
        json={"decision": "approuve", "montant_decide": 999999999, "motif": "Erreur"},
        headers=comite,
    )
    assert reponse.status_code == 422


def test_decision_motif_absent_422(client: TestClient, db: Session) -> None:
    agence = _agence(db, "CRD4")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande_id = _demande(db, agence, tier_id, produit)
    comite = _entete(db, agence, "MEMBRE_COMITE_CREDIT")

    reponse = client.post(
        f"/credit/demandes/{demande_id}/decision",
        json={"decision": "refuse", "motif": ""},
        headers=comite,
    )
    assert reponse.status_code == 422


def test_redecision_refusee(client: TestClient, db: Session) -> None:
    agence = _agence(db, "CRD5")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande_id = _demande(db, agence, tier_id, produit)
    comite = _entete(db, agence, "MEMBRE_COMITE_CREDIT")

    premiere = client.post(
        f"/credit/demandes/{demande_id}/decision",
        json={"decision": "refuse", "motif": "Premier refus"},
        headers=comite,
    )
    assert premiere.status_code == 200

    seconde = client.post(
        f"/credit/demandes/{demande_id}/decision",
        json={"decision": "approuve", "montant_decide": 100000, "motif": "Changement d'avis"},
        headers=comite,
    )
    assert seconde.status_code == 422
    assert "déjà" in seconde.json()["detail"].lower()


def test_approbation_refusee_si_tiers_suspendu_entre_temps(
    client: TestClient, db: Session
) -> None:
    """LE point sensible : demande créée quand le tiers était actif, tiers suspendu ENSUITE,
    APPROBATION refusée à ce moment-là — pas seulement à la création."""
    agence = _agence(db, "CRD6")
    tier_id = _tier(db, agence)  # actif à la création
    produit = _produit(db)
    demande_id = _demande(db, agence, tier_id, produit)

    db.execute(
        text("UPDATE tiers.tiers SET status = 'suspendu_temporaire' WHERE id = :t"),
        {"t": tier_id},
    )
    db.flush()

    comite = _entete(db, agence, "MEMBRE_COMITE_CREDIT")
    reponse = client.post(
        f"/credit/demandes/{demande_id}/decision",
        json={"decision": "approuve", "montant_decide": 400000, "motif": "Tentative"},
        headers=comite,
    )
    assert reponse.status_code == 422
    assert "actif" in reponse.json()["detail"].lower()


def test_refus_toujours_possible_meme_si_tiers_suspendu_entre_temps(
    client: TestClient, db: Session
) -> None:
    """Le REFUS, lui, n'est JAMAIS bloqué par le statut du tiers — aucun risque à clôturer un
    dossier périmé, et bloquer nuirait à la tenue du portefeuille."""
    agence = _agence(db, "CRD7")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande_id = _demande(db, agence, tier_id, produit)

    db.execute(
        text("UPDATE tiers.tiers SET status = 'suspendu_temporaire' WHERE id = :t"),
        {"t": tier_id},
    )
    db.flush()

    comite = _entete(db, agence, "MEMBRE_COMITE_CREDIT")
    reponse = client.post(
        f"/credit/demandes/{demande_id}/decision",
        json={"decision": "refuse", "motif": "Dossier périmé, tiers suspendu"},
        headers=comite,
    )
    assert reponse.status_code == 200
    assert reponse.json()["status"] == "refuse"


def test_decision_sans_permission_403(client: TestClient, db: Session) -> None:
    agence = _agence(db, "CRD8")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande_id = _demande(db, agence, tier_id, produit)
    charge = _entete(db, agence, "CHARGE_PRET")  # crée, mais ne décide pas

    reponse = client.post(
        f"/credit/demandes/{demande_id}/decision",
        json={"decision": "approuve", "montant_decide": 100000, "motif": "Test"},
        headers=charge,
    )
    assert reponse.status_code == 403


# --- Lecture ---------------------------------------------------------------------------------


def test_lister_et_lire_demande(client: TestClient, db: Session) -> None:
    agence = _agence(db, "CRL1")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande_id = _demande(db, agence, tier_id, produit)
    charge = _entete(db, agence, "CHARGE_PRET")

    liste = client.get("/credit/demandes", headers=charge)
    assert liste.status_code == 200
    assert any(d["id"] == str(demande_id) for d in liste.json())

    detail = client.get(f"/credit/demandes/{demande_id}", headers=charge)
    assert detail.status_code == 200
    assert detail.json()["objet"] == "Test"


def test_demande_hors_agence_404(client: TestClient, db: Session) -> None:
    agence = _agence(db, "CRL2")
    autre_agence = _agence(db, "CRL3")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande_id = _demande(db, agence, tier_id, produit)
    charge_autre = _entete(db, autre_agence, "CHARGE_PRET")

    reponse = client.get(f"/credit/demandes/{demande_id}", headers=charge_autre)
    assert reponse.status_code == 404


# --- Lecture par tiers (onglet Crédit de la fiche) --------------------------------------------


def test_lister_demandes_dun_tiers_ne_retourne_que_les_siennes(
    client: TestClient, db: Session
) -> None:
    agence = _agence(db, "CRT1")
    produit = _produit(db)
    tier_a = _tier(db, agence)
    tier_b = _tier(db, agence)
    demande_a = _demande(db, agence, tier_a, produit)
    _demande(db, agence, tier_b, produit)  # d'un AUTRE tiers : ne doit pas apparaître
    charge = _entete(db, agence, "CHARGE_PRET")

    reponse = client.get(f"/tiers/{tier_a}/demandes-credit", headers=charge)

    assert reponse.status_code == 200
    corps = reponse.json()
    assert [d["id"] for d in corps] == [str(demande_a)]


def test_lister_demandes_tier_hors_agence_404(client: TestClient, db: Session) -> None:
    agence = _agence(db, "CRT2")
    autre_agence = _agence(db, "CRT3")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    _demande(db, agence, tier_id, produit)
    charge_autre = _entete(db, autre_agence, "CHARGE_PRET")

    reponse = client.get(f"/tiers/{tier_id}/demandes-credit", headers=charge_autre)

    assert reponse.status_code == 404


def test_lister_demandes_tier_sans_permission_403(client: TestClient, db: Session) -> None:
    agence = _agence(db, "CRT4")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    _demande(db, agence, tier_id, produit)
    caissier = _entete(db, agence, "CAISSIER")  # n'a pas credit.demande.read

    reponse = client.get(f"/tiers/{tier_id}/demandes-credit", headers=caissier)

    assert reponse.status_code == 403
