"""API Guichet (F2a) — dépôt / retrait, et surtout les REFUS parlants.

  - recherche par numéro : renvoie le NOM du membre (vérification humaine contre une faute de
    frappe dans le numéro) ;
  - dépôt : crédite ; retrait > solde -> 422 avec « disponible » ; membre suspendu -> 422 (gel) ;
    compte fermé -> 422 ; montant <= 0 -> 422 ; sans permission -> 403 ; hors agence -> 404.
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.epargne import service
from app.modules.epargne.models import Product, SavingsAccount
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


def _membre_nomme(db: Session, agence: Agency, nom: str, prenom: str) -> uuid.UUID:
    tier_id = db.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES (:n, 'individual', :a, 'actif') RETURNING id"
        ),
        {"n": f"M-G-{uuid.uuid4().hex[:6]}", "a": agence.id},
    ).scalar_one()
    nat = db.execute(text("SELECT id FROM parameters.countries LIMIT 1")).scalar_one()
    db.execute(
        text(
            "INSERT INTO tiers.individual_profiles "
            "(tier_id, last_name, first_name, birth_date, gender, nationality_id) "
            "VALUES (:t, :n, :p, '1990-01-01', 'F', :nat)"
        ),
        {"t": tier_id, "n": nom, "p": prenom, "nat": nat},
    )
    return tier_id


def _compte(db: Session, agence: Agency) -> SavingsAccount:
    produit = Product(
        code=f"PG{uuid.uuid4().hex[:4]}", name="Épargne à vue", type="a_vue",
        compte_epargne_id=_cid(db, "251111"),
    )
    db.add(produit)
    db.flush()
    tier_id = _membre_nomme(db, agence, "Traoré", "Fatoumata")
    return service.ouvrir_compte(
        db, tier_id=tier_id, product_id=produit.id, agency_id=agence.id, par=None
    )


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


def test_recherche_par_numero_renvoie_le_nom_du_membre(client: TestClient, db: Session) -> None:
    agence = _agence(db, "AG-G1")
    compte = _compte(db, agence)
    entete = _entete(db, agence, "CAISSIER")

    reponse = client.get(
        "/epargne/recherche-compte", params={"numero": compte.account_number}, headers=entete
    )
    assert reponse.status_code == 200
    # Nom proéminent = vérification humaine contre une faute de frappe dans le numéro.
    assert reponse.json()["membre_nom"] == "Traoré Fatoumata"


def test_depot_puis_retrait(client: TestClient, db: Session) -> None:
    agence = _agence(db, "AG-G2")
    compte = _compte(db, agence)
    entete = _entete(db, agence, "CAISSIER")

    depot = client.post(
        f"/epargne/comptes/{compte.id}/depot", json={"montant": 10000}, headers=entete
    )
    assert depot.status_code == 200
    assert depot.json()["nouveau_solde"] == 10000

    retrait = client.post(
        f"/epargne/comptes/{compte.id}/retrait", json={"montant": 3000}, headers=entete
    )
    assert retrait.json()["nouveau_solde"] == 7000


def test_retrait_solde_insuffisant_422_avec_disponible(client: TestClient, db: Session) -> None:
    agence = _agence(db, "AG-G3")
    compte = _compte(db, agence)
    entete = _entete(db, agence, "CAISSIER")
    client.post(f"/epargne/comptes/{compte.id}/depot", json={"montant": 1000}, headers=entete)

    reponse = client.post(
        f"/epargne/comptes/{compte.id}/retrait", json={"montant": 5000}, headers=entete
    )
    assert reponse.status_code == 422
    assert "disponible" in reponse.json()["detail"].lower()


def test_operation_membre_suspendu_422(client: TestClient, db: Session) -> None:
    agence = _agence(db, "AG-G4")
    compte = _compte(db, agence)
    db.execute(
        text("UPDATE tiers.tiers SET status = 'suspendu_temporaire' WHERE id = :t"),
        {"t": compte.tier_id},
    )
    entete = _entete(db, agence, "CAISSIER")

    reponse = client.post(
        f"/epargne/comptes/{compte.id}/depot", json={"montant": 1000}, headers=entete
    )
    assert reponse.status_code == 422
    assert "gel" in reponse.json()["detail"].lower()


def test_operation_compte_ferme_422(client: TestClient, db: Session) -> None:
    agence = _agence(db, "AG-G5")
    compte = _compte(db, agence)
    db.execute(
        text("UPDATE epargne.accounts SET status = 'cloture' WHERE id = :a"), {"a": compte.id}
    )
    entete = _entete(db, agence, "CAISSIER")

    reponse = client.post(
        f"/epargne/comptes/{compte.id}/depot", json={"montant": 1000}, headers=entete
    )
    assert reponse.status_code == 422


def test_montant_invalide_422(client: TestClient, db: Session) -> None:
    agence = _agence(db, "AG-G6")
    compte = _compte(db, agence)
    entete = _entete(db, agence, "CAISSIER")

    reponse = client.post(
        f"/epargne/comptes/{compte.id}/depot", json={"montant": 0}, headers=entete
    )
    assert reponse.status_code == 422


def test_depot_sans_permission_403(client: TestClient, db: Session) -> None:
    agence = _agence(db, "AG-G7")
    compte = _compte(db, agence)
    # AUDITEUR_INTERNE lit mais n'opère pas au guichet.
    entete = _entete(db, agence, "AUDITEUR_INTERNE")

    reponse = client.post(
        f"/epargne/comptes/{compte.id}/depot", json={"montant": 1000}, headers=entete
    )
    assert reponse.status_code == 403


def test_compte_hors_agence_404(client: TestClient, db: Session) -> None:
    agence_a = _agence(db, "AG-GA")
    agence_b = _agence(db, "AG-GB")
    compte = _compte(db, agence_a)
    entete_b = _entete(db, agence_b, "CAISSIER")

    reponse = client.post(
        f"/epargne/comptes/{compte.id}/depot", json={"montant": 1000}, headers=entete_b
    )
    assert reponse.status_code == 404


# --- Fermeture (F3a) : responsable, restitution, refus débiteur ---------------------


def test_fermeture_restitue_et_ferme(client: TestClient, db: Session) -> None:
    agence = _agence(db, "AG-F1")
    compte = _compte(db, agence)
    caissier = _entete(db, agence, "CAISSIER")
    client.post(f"/epargne/comptes/{compte.id}/depot", json={"montant": 5000}, headers=caissier)
    resp = _entete(db, agence, "RESPONSABLE_AGENCE")

    reponse = client.post(f"/epargne/comptes/{compte.id}/fermeture", headers=resp)
    assert reponse.status_code == 200
    assert reponse.json()["status"] == "cloture"
    assert reponse.json()["balance"] == 0  # solde restitué


def test_fermeture_debiteur_422_message_humain(client: TestClient, db: Session) -> None:
    agence = _agence(db, "AG-F2")
    compte = _compte(db, agence)
    db.execute(
        text("UPDATE epargne.accounts SET balance = -100 WHERE id = :a"), {"a": compte.id}
    )
    resp = _entete(db, agence, "RESPONSABLE_AGENCE")

    reponse = client.post(f"/epargne/comptes/{compte.id}/fermeture", headers=resp)
    assert reponse.status_code == 422
    detail = reponse.json()["detail"]
    assert "débiteur" in detail.lower() and ">" not in detail  # humain, sans syntaxe technique


def test_fermeture_sans_permission_403(client: TestClient, db: Session) -> None:
    agence = _agence(db, "AG-F3")
    compte = _compte(db, agence)
    # Le caissier opère mais ne FERME pas (acte de gestion réservé au responsable).
    caissier = _entete(db, agence, "CAISSIER")

    reponse = client.post(f"/epargne/comptes/{compte.id}/fermeture", headers=caissier)
    assert reponse.status_code == 403


# --- Intérêts (F4a) : prévisualisation détaillée, versement, « déjà versé », rapprochement ------


def _compte_remunere(db: Session, agence: Agency) -> SavingsAccount:
    """Un compte sur un produit rémunéré à 10 % l'an (base 365) : un dépôt gardé toute l'année
    produit exactement 10 % — de quoi vérifier le détail de la prévisualisation."""
    produit = Product(
        code=f"PR{uuid.uuid4().hex[:4]}", name="Épargne rémunérée", type="a_vue",
        compte_epargne_id=_cid(db, "251111"), compte_charge_interet_id=_cid(db, "602511"),
        taux_bp=1000, base_jours=365, methode_calcul_solde="fin_periode",
    )
    db.add(produit)
    db.flush()
    tier_id = _membre_nomme(db, agence, "Diallo", "Aminata")
    return service.ouvrir_compte(
        db, tier_id=tier_id, product_id=produit.id, agency_id=agence.id, par=None
    )


def test_apercu_interets_montre_le_total_et_le_detail(client: TestClient, db: Session) -> None:
    agence = _agence(db, "AG-I1")
    compte = _compte_remunere(db, agence)
    caissier = _entete(db, agence, "CAISSIER")
    client.post(f"/epargne/comptes/{compte.id}/depot", json={"montant": 100000}, headers=caissier)
    direction = _entete(db, agence, "DIRECTION_GENERALE")

    reponse = client.post(
        "/epargne/interets/apercu",
        json={"periode": "2026", "debut": "2026-01-01", "fin": "2026-12-31"},
        headers=direction,
    )
    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["total"] >= 10000
    # DÉTAIL par compte : le taux appliqué est visible, pas seulement le total (erreur de taux
    # détectable AVANT de créer de l'argent).
    mien = next(
        x for x in corps["echantillon"] if x["account_number"] == compte.account_number
    )
    assert mien["montant"] == 10000
    assert mien["taux_bp"] == 1000
    assert mien["methode"] == "fin_periode"

    # DRY-RUN : le solde n'a pas bougé (aucun versement).
    db.refresh(compte)
    assert compte.balance == 100000


def test_versement_puis_apercu_dit_deja_verse(client: TestClient, db: Session) -> None:
    agence = _agence(db, "AG-I2")
    compte = _compte_remunere(db, agence)
    caissier = _entete(db, agence, "CAISSIER")
    client.post(f"/epargne/comptes/{compte.id}/depot", json={"montant": 100000}, headers=caissier)
    direction = _entete(db, agence, "DIRECTION_GENERALE")
    bornes = {"periode": "2026", "debut": "2026-01-01", "fin": "2026-12-31"}

    versement = client.post("/epargne/interets", json=bornes, headers=direction)
    assert versement.status_code == 200
    assert versement.json()["credites"] >= 1

    # Relance en aperçu : la période est déjà versée -> le compte n'est plus à créditer, et on
    # sait QUAND (pas d'échec silencieux).
    apercu = client.post("/epargne/interets/apercu", json=bornes, headers=direction)
    corps = apercu.json()
    assert not any(x["account_number"] == compte.account_number for x in corps["echantillon"])
    assert corps["deja_traites"] >= 1
    assert corps["deja_verse_le"] is not None


def test_interets_reserves_a_la_direction_403(client: TestClient, db: Session) -> None:
    agence = _agence(db, "AG-I3")
    _compte_remunere(db, agence)
    # Le responsable d'agence NE verse PAS les intérêts : c'est un acte d'institution.
    resp = _entete(db, agence, "RESPONSABLE_AGENCE")
    bornes = {"periode": "2026", "debut": "2026-01-01", "fin": "2026-12-31"}

    assert client.post("/epargne/interets/apercu", json=bornes, headers=resp).status_code == 403
    assert client.post("/epargne/interets", json=bornes, headers=resp).status_code == 403


def test_rapprochement_concordant_apres_depot(client: TestClient, db: Session) -> None:
    agence = _agence(db, "AG-R1")
    compte = _compte(db, agence)
    caissier = _entete(db, agence, "CAISSIER")
    client.post(f"/epargne/comptes/{compte.id}/depot", json={"montant": 40000}, headers=caissier)
    auditeur = _entete(db, agence, "AUDITEUR_INTERNE")

    reponse = client.get("/epargne/rapprochement", headers=auditeur)
    assert reponse.status_code == 200
    ligne = next(x for x in reponse.json() if x["compte_general"] == "251111")
    # Σ soldes d'épargne == solde comptable de 251111 : concordant, écart nul.
    assert ligne["concordant"] is True
    assert ligne["ecart"] == 0


def test_rapprochement_sans_permission_403(client: TestClient, db: Session) -> None:
    agence = _agence(db, "AG-R2")
    # Le caissier opère au guichet mais ne CONTRÔLE pas le rapprochement.
    caissier = _entete(db, agence, "CAISSIER")

    reponse = client.get("/epargne/rapprochement", headers=caissier)
    assert reponse.status_code == 403
