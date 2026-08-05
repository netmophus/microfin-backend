"""API Crédit CR6b — aperçu PUR de l'échéancier d'une demande approuvée.

  - disponible dès `approuve`, refusé sinon (rien à prévisualiser) ;
  - RIEN N'EST ÉCRIT : le nombre de lignes credit.installments ne bouge pas ;
  - même moteur que le décaissement réel : aperçu et échéancier décaissé LE MÊME JOUR sont
    identiques ligne à ligne (montants ET dates) — la preuve de déterminisme demandée ;
  - un échéancier impossible (CR2) se révèle DÈS l'aperçu, avant toute tentative réelle ;
  - permissions et cloisonnement comme le reste du module.
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.credit.demandes import creer_demande, decider
from app.modules.credit.models import Installment, Product
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
        {"n": f"M-APC-{uuid.uuid4().hex[:6]}", "a": agence.id, "s": statut},
    ).scalar_one()
    nat = db.execute(text("SELECT id FROM parameters.countries LIMIT 1")).scalar_one()
    db.execute(
        text(
            "INSERT INTO tiers.individual_profiles "
            "(tier_id, last_name, first_name, birth_date, gender, nationality_id) "
            "VALUES (:t, 'Sow', 'Mariam', '1985-01-01', 'F', :nat)"
        ),
        {"t": tier_id, "nat": nat},
    )
    return tier_id


def _produit(db: Session, **kwargs: object) -> Product:
    produit = Product(
        code=f"APC{uuid.uuid4().hex[:5]}",
        name="Crédit test aperçu",
        compte_credit_membre_id=_cid(db, "202211"),
        compte_credit_client_id=_cid(db, "202221"),
        compte_produits_interets_id=_cid(db, "7021"),
        taux_bp=1200,
        **kwargs,
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


def _demande_approuvee(
    db: Session,
    agence: Agency,
    tier_id: uuid.UUID,
    produit: Product,
    montant_demande: int = 500000,
    montant_decide: int | None = None,
    duree_echeances: int = 12,
) -> uuid.UUID:
    demande = creer_demande(
        db, tier_id=tier_id, agency_id=agence.id, product_id=produit.id,
        montant_demande=montant_demande, duree_echeances=duree_echeances,
        objet="Test aperçu", par=None,
    )
    decider(
        db, demande, decision="approuve",
        montant_decide=montant_decide or montant_demande, motif="Dossier complet", par=None,
    )
    db.commit()
    return demande.id


# --- Disponibilité --------------------------------------------------------------------------


def test_apercu_disponible_des_que_approuve(client: TestClient, db: Session) -> None:
    agence = _agence(db, "APA1")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande_id = _demande_approuvee(db, agence, tier_id, produit, montant_demande=300000)
    responsable = _entete(db, agence, "RESPONSABLE_AGENCE")

    reponse = client.get(f"/credit/demandes/{demande_id}/echeancier-apercu", headers=responsable)

    assert reponse.status_code == 200
    lignes = reponse.json()
    assert len(lignes) == 12
    assert sum(ligne["capital"] for ligne in lignes) == 300000
    assert lignes[-1]["capital_restant_du"] == 0
    assert "status" not in lignes[0]  # rien à suivre pour un aperçu


def test_apercu_refuse_si_pas_encore_approuve(client: TestClient, db: Session) -> None:
    agence = _agence(db, "APA2")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande = creer_demande(
        db, tier_id=tier_id, agency_id=agence.id, product_id=produit.id,
        montant_demande=200000, duree_echeances=6, objet="Non décidée", par=None,
    )
    db.commit()
    responsable = _entete(db, agence, "RESPONSABLE_AGENCE")

    reponse = client.get(f"/credit/demandes/{demande.id}/echeancier-apercu", headers=responsable)

    assert reponse.status_code == 422
    assert "approuv" in reponse.json()["detail"].lower()


# --- Rien n'est écrit ------------------------------------------------------------------------


def test_apercu_necrit_rien_en_base(client: TestClient, db: Session) -> None:
    agence = _agence(db, "APA3")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande_id = _demande_approuvee(db, agence, tier_id, produit)
    responsable = _entete(db, agence, "RESPONSABLE_AGENCE")

    avant = db.execute(
        select(Installment).where(Installment.application_id == demande_id)
    ).first()
    assert avant is None

    reponse = client.get(f"/credit/demandes/{demande_id}/echeancier-apercu", headers=responsable)
    assert reponse.status_code == 200

    apres = db.execute(
        select(Installment).where(Installment.application_id == demande_id)
    ).first()
    assert apres is None  # toujours rien : l'aperçu n'a rien persisté


# --- Même moteur, déterminisme -----------------------------------------------------------


def test_apercu_et_decaissement_reel_le_meme_jour_sont_identiques(
    client: TestClient, db: Session
) -> None:
    agence = _agence(db, "APA4")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande_id = _demande_approuvee(db, agence, tier_id, produit, montant_demande=420000)
    responsable = _entete(db, agence, "RESPONSABLE_AGENCE")

    apercu = client.get(
        f"/credit/demandes/{demande_id}/echeancier-apercu", headers=responsable
    ).json()

    decaissement = client.post(
        f"/credit/demandes/{demande_id}/decaissement", headers=responsable
    )
    assert decaissement.status_code == 200

    reel = client.get(f"/credit/demandes/{demande_id}/echeancier", headers=responsable).json()

    assert len(apercu) == len(reel) == 12
    for ligne_apercu, ligne_reelle in zip(apercu, reel, strict=True):
        assert ligne_apercu["numero"] == ligne_reelle["numero"]
        assert ligne_apercu["due_date"] == ligne_reelle["due_date"]
        assert ligne_apercu["capital"] == ligne_reelle["capital"]
        assert ligne_apercu["interets"] == ligne_reelle["interets"]
        assert ligne_apercu["total"] == ligne_reelle["total"]
        assert ligne_apercu["capital_restant_du"] == ligne_reelle["capital_restant_du"]


# --- L'échéancier impossible se révèle plus tôt -------------------------------------------


def test_apercu_echeancier_impossible_revele_avant_le_decaissement(
    client: TestClient, db: Session
) -> None:
    agence = _agence(db, "APA5")
    tier_id = _tier(db, agence)
    produit = _produit(db, methode_amortissement="capital_constant", regle_arrondi="plus_proche")
    # Même cas pathologique que CR2/CR3 : montant=5, durée=10 -> capital de base arrondit à 1,
    # 1x9=9 > 5, impossible.
    demande_id = _demande_approuvee(
        db, agence, tier_id, produit, montant_demande=5, montant_decide=5, duree_echeances=10
    )
    responsable = _entete(db, agence, "RESPONSABLE_AGENCE")

    reponse = client.get(f"/credit/demandes/{demande_id}/echeancier-apercu", headers=responsable)

    assert reponse.status_code == 422
    detail = reponse.json()["detail"].lower()
    assert "impossible" in detail or "négatif" in detail or "capital" in detail


# --- Permissions et cloisonnement ----------------------------------------------------------


def test_apercu_sans_permission_403(client: TestClient, db: Session) -> None:
    agence = _agence(db, "APA6")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande_id = _demande_approuvee(db, agence, tier_id, produit)
    caissier = _entete(db, agence, "CAISSIER")  # n'a pas credit.demande.read

    reponse = client.get(f"/credit/demandes/{demande_id}/echeancier-apercu", headers=caissier)

    assert reponse.status_code == 403


def test_apercu_hors_agence_404(client: TestClient, db: Session) -> None:
    agence = _agence(db, "APA7")
    autre_agence = _agence(db, "APA8")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande_id = _demande_approuvee(db, agence, tier_id, produit)
    charge_autre = _entete(db, autre_agence, "CHARGE_PRET")

    reponse = client.get(
        f"/credit/demandes/{demande_id}/echeancier-apercu", headers=charge_autre
    )

    assert reponse.status_code == 404
