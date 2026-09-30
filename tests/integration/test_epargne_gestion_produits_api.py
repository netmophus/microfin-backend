"""API de gestion du référentiel produit d'épargne (création, modification métier, validation,
activation) — chantier « gestion des produits », lot 1/3 (backend).

  - epargne.product.manage (ADMIN_FONCTIONNEL) garde ces 4 endpoints — jamais compta.plan.manage,
    réservé aux rattachements comptables et aux paramètres d'intérêt (écrans distincts, déjà
    testés ailleurs) ;
  - code unique -> 422 clair, jamais une 500 (IntegrityError traduite) ;
  - GARDE-FOU de validation : compte membre indispensable (422 si absent) ; compte client
    optionnel par construction (une IMCEC n'a que des membres) -> avertissement NON bloquant,
    jamais silencieux (risque d'imputation en audit BCEAO) ;
  - activation/désactivation : motif obligatoire dans les deux sens, même patron que
    caisse.postes.changer_activation.
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.comptabilite.models import Account
from app.modules.epargne.models import Product
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


def _agence_id(db: Session) -> uuid.UUID:
    return db.execute(text("SELECT id FROM parameters.agencies LIMIT 1")).scalar_one()


def _entete_auth(db: Session, role_code: str) -> dict[str, str]:
    role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
    agence_id = _agence_id(db)
    s = uuid.uuid4().hex[:8]
    user = User(
        matricule=f"MAT-{s}", email=f"{s}@ex.com", username=f"u{s}",
        password_hash=hasher_mot_de_passe("Motdepasse!123"), last_name="T", first_name="A",
        primary_agency_id=agence_id,
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.flush()
    jeton = creer_access_token(
        user_id=user.id, roles=[role_code], primary_agency_id=agence_id, agency_id=agence_id
    )
    return {"Authorization": f"Bearer {jeton}"}


def _compte(db: Session, numero: str, **overrides: object) -> Account:
    valeurs = {
        "account_number": numero,
        "name": f"Compte {numero}",
        "account_class": int(numero[0]),
        "normal_side": "D",
        "is_posting": True,
        "is_system": False,
        **overrides,
    }
    compte = Account(**valeurs)
    db.add(compte)
    db.flush()
    return compte


def _produit(db: Session, **overrides: object) -> Product:
    valeurs = {
        "code": f"PT{uuid.uuid4().hex[:6]}",
        "name": "Produit de test",
        "type": "a_vue",
        **overrides,
    }
    produit = Product(**valeurs)
    db.add(produit)
    db.flush()
    return produit


CORPS_CREATION = {"code": "TST01", "name": "Produit test"}


# --- Référentiel (liste de gestion, actifs + inactifs) -------------------------------------


def test_referentiel_inclut_un_produit_inactif(client: TestClient, db: Session) -> None:
    _produit(db, code="REF-INACTIF", is_active=False)
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")

    reponse = client.get("/epargne/produits/referentiel", headers=admin)

    assert reponse.status_code == 200
    codes = {p["code"] for p in reponse.json()}
    assert "REF-INACTIF" in codes
    ligne = next(p for p in reponse.json() if p["code"] == "REF-INACTIF")
    assert ligne["is_active"] is False


def test_referentiel_sans_permission_403(client: TestClient, db: Session) -> None:
    # MEMBRE_COMITE_CREDIT : rôle minimal (credit.demande.*), aucun droit épargne — CAISSIER
    # détient déjà epargne.product.read (guichet), donc pas un bon candidat pour ce 403.
    membre_comite = _entete_auth(db, "MEMBRE_COMITE_CREDIT")
    reponse = client.get("/epargne/produits/referentiel", headers=membre_comite)
    assert reponse.status_code == 403


# --- Création ---------------------------------------------------------------------------


def test_creation_decouvert_autorise_rejetee_422(client: TestClient, db: Session) -> None:
    """Un SFD ne tient pas de comptes courants : decouvert_autorise n'existe pas dans le
    schéma de création — extra="forbid" REJETTE (422) plutôt que d'ignorer en silence."""
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")

    reponse = client.post(
        "/epargne/produits",
        json={**CORPS_CREATION, "decouvert_autorise": 5000},
        headers=admin,
    )

    assert reponse.status_code == 422
    assert db.execute(
        text("SELECT count(*) FROM epargne.products WHERE code = :c"), {"c": "TST01"}
    ).scalar_one() == 0


def test_creation_reussie_produit_provisoire(client: TestClient, db: Session) -> None:
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")

    reponse = client.post("/epargne/produits", json=CORPS_CREATION, headers=admin)

    assert reponse.status_code == 201
    corps = reponse.json()
    assert corps["code"] == "TST01"
    assert corps["is_provisional"] is True
    assert corps["is_active"] is True
    assert corps["type"] == "a_vue"
    assert corps["currency"] == "XOF"
    assert corps["taux_bp"] == 0

    ligne = db.execute(
        text(
            "SELECT action, resource_id FROM audit.audit_logs "
            "WHERE action = 'epargne.product.created' AND resource_id = :r"
        ),
        {"r": corps["id"]},
    ).one()
    assert str(ligne.resource_id) == corps["id"]


def test_creation_code_duplique_422(client: TestClient, db: Session) -> None:
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")
    _produit(db, code="DUP01")

    reponse = client.post(
        "/epargne/produits", json={"code": "DUP01", "name": "Doublon"}, headers=admin
    )

    assert reponse.status_code == 422
    assert "DUP01" in reponse.json()["detail"]


def test_creation_sans_permission_403(client: TestClient, db: Session) -> None:
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.post("/epargne/produits", json=CORPS_CREATION, headers=comptable)

    assert reponse.status_code == 403


# --- Modification métier -----------------------------------------------------------------


def test_modification_reussie_avec_motif_trace(client: TestClient, db: Session) -> None:
    produit = _produit(db, code="MOD01", name="Ancien nom")
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")
    corps = {
        "name": "Nouveau nom",
        "type": "terme",
        "taux_bp": 500,
        "periodicite": "mensuelle",
        "methode_calcul_solde": "moyen_quotidien",
        "base_jours": 365,
        "regle_arrondi": "plancher",
        "solde_minimum_remunere": 1000,
        "motif": "Refonte du produit",
    }

    reponse = client.patch(f"/epargne/produits/{produit.id}", json=corps, headers=admin)

    assert reponse.status_code == 200
    resultat = reponse.json()
    assert resultat["name"] == "Nouveau nom"
    assert resultat["type"] == "terme"
    assert resultat["taux_bp"] == 500

    ligne = db.execute(
        text(
            "SELECT old_values, new_values FROM audit.audit_logs "
            "WHERE action = 'epargne.product.updated' AND resource_id = :r"
        ),
        {"r": produit.id},
    ).one()
    assert ligne.old_values["name"] == "Ancien nom"
    assert ligne.new_values["motif"] == "Refonte du produit"


def test_modification_decouvert_autorise_rejetee_422(client: TestClient, db: Session) -> None:
    """Même garde-fou qu'à la création : decouvert_autorise n'existe pas dans le schéma de
    modification — extra="forbid" rejette, la valeur en base reste inchangée."""
    produit = _produit(db, code="MOD-DEC", decouvert_autorise=0)
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")
    corps = {
        "name": "X", "type": "a_vue", "taux_bp": 0, "periodicite": "annuelle",
        "methode_calcul_solde": "fin_periode", "base_jours": 360,
        "regle_arrondi": "plus_proche", "solde_minimum_remunere": 0, "motif": "Tentative",
        "decouvert_autorise": 5000,
    }

    reponse = client.patch(f"/epargne/produits/{produit.id}", json=corps, headers=admin)

    assert reponse.status_code == 422
    db.refresh(produit)
    assert produit.decouvert_autorise == 0


def test_modification_motif_absent_refusee(client: TestClient, db: Session) -> None:
    produit = _produit(db, code="MOD02")
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")
    corps = {
        "name": "X", "type": "a_vue", "taux_bp": 0, "periodicite": "annuelle",
        "methode_calcul_solde": "fin_periode", "base_jours": 360,
        "regle_arrondi": "plus_proche", "solde_minimum_remunere": 0, "motif": "",
    }

    reponse = client.patch(f"/epargne/produits/{produit.id}", json=corps, headers=admin)
    assert reponse.status_code == 422


def test_modification_sans_permission_403(client: TestClient, db: Session) -> None:
    produit = _produit(db, code="MOD03")
    comptable = _entete_auth(db, "COMPTABLE")
    corps = {
        "name": "X", "type": "a_vue", "taux_bp": 0, "periodicite": "annuelle",
        "methode_calcul_solde": "fin_periode", "base_jours": 360,
        "regle_arrondi": "plus_proche", "solde_minimum_remunere": 0, "motif": "Tentative",
    }

    reponse = client.patch(f"/epargne/produits/{produit.id}", json=corps, headers=comptable)
    assert reponse.status_code == 403


def test_modification_produit_introuvable_404(client: TestClient, db: Session) -> None:
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")
    corps = {
        "name": "X", "type": "a_vue", "taux_bp": 0, "periodicite": "annuelle",
        "methode_calcul_solde": "fin_periode", "base_jours": 360,
        "regle_arrondi": "plus_proche", "solde_minimum_remunere": 0, "motif": "Tentative",
    }

    reponse = client.patch(f"/epargne/produits/{uuid.uuid4()}", json=corps, headers=admin)
    assert reponse.status_code == 404


# --- Validation (lever le provisoire) -----------------------------------------------------


def test_validation_refusee_si_compte_membre_manquant(client: TestClient, db: Session) -> None:
    produit = _produit(db, code="VAL01", compte_epargne_id=None)
    db.commit()  # checkpoint : le rollback de la requête (422) ne doit pas emporter ce setup.
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")

    reponse = client.post(f"/epargne/produits/{produit.id}/valider", headers=admin)

    assert reponse.status_code == 422
    assert "compte membre" in reponse.json()["detail"]
    db.refresh(produit)
    assert produit.is_provisional is True


def test_validation_ok_avec_avertissement_si_compte_client_manquant(
    client: TestClient, db: Session
) -> None:
    membre = _compte(db, "351111")
    produit = _produit(
        db, code="VAL02", compte_epargne_id=membre.id, compte_epargne_client_id=None
    )
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")

    reponse = client.post(f"/epargne/produits/{produit.id}/valider", headers=admin)

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["is_provisional"] is False
    assert len(corps["avertissements"]) == 1
    assert "compte client" in corps["avertissements"][0].lower()
    assert "compte membre" in corps["avertissements"][0].lower()


def test_validation_ok_sans_avertissement_si_deux_comptes_rattaches(
    client: TestClient, db: Session
) -> None:
    membre = _compte(db, "351121")
    client_compte = _compte(db, "351122")
    produit = _produit(
        db,
        code="VAL03",
        compte_epargne_id=membre.id,
        compte_epargne_client_id=client_compte.id,
    )
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")

    reponse = client.post(f"/epargne/produits/{produit.id}/valider", headers=admin)

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["is_provisional"] is False
    assert corps["avertissements"] == []


def test_validation_sans_permission_403(client: TestClient, db: Session) -> None:
    membre = _compte(db, "351131")
    produit = _produit(db, code="VAL04", compte_epargne_id=membre.id)
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.post(f"/epargne/produits/{produit.id}/valider", headers=comptable)
    assert reponse.status_code == 403


def test_validation_produit_introuvable_404(client: TestClient, db: Session) -> None:
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")
    reponse = client.post(f"/epargne/produits/{uuid.uuid4()}/valider", headers=admin)
    assert reponse.status_code == 404


# --- Activation / désactivation du catalogue -----------------------------------------------


def test_desactivation_avec_motif(client: TestClient, db: Session) -> None:
    produit = _produit(db, code="ACT01")
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")

    reponse = client.patch(
        f"/epargne/produits/{produit.id}/activation",
        json={"is_active": False, "motif": "Produit retiré du catalogue"},
        headers=admin,
    )

    assert reponse.status_code == 200
    assert reponse.json()["is_active"] is False

    ligne = db.execute(
        text(
            "SELECT new_values FROM audit.audit_logs "
            "WHERE action = 'epargne.product.deactivated' AND resource_id = :r"
        ),
        {"r": produit.id},
    ).one()
    assert ligne.new_values["motif"] == "Produit retiré du catalogue"


def test_reactivation_avec_motif(client: TestClient, db: Session) -> None:
    produit = _produit(db, code="ACT02", is_active=False)
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")

    reponse = client.patch(
        f"/epargne/produits/{produit.id}/activation",
        json={"is_active": True, "motif": "Produit remis au catalogue"},
        headers=admin,
    )

    assert reponse.status_code == 200
    assert reponse.json()["is_active"] is True


def test_activation_motif_absent_refusee(client: TestClient, db: Session) -> None:
    produit = _produit(db, code="ACT03")
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")

    reponse = client.patch(
        f"/epargne/produits/{produit.id}/activation",
        json={"is_active": False, "motif": ""},
        headers=admin,
    )
    assert reponse.status_code == 422


def test_activation_sans_permission_403(client: TestClient, db: Session) -> None:
    produit = _produit(db, code="ACT04")
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.patch(
        f"/epargne/produits/{produit.id}/activation",
        json={"is_active": False, "motif": "Tentative"},
        headers=comptable,
    )
    assert reponse.status_code == 403


def test_activation_produit_introuvable_404(client: TestClient, db: Session) -> None:
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")
    reponse = client.patch(
        f"/epargne/produits/{uuid.uuid4()}/activation",
        json={"is_active": False, "motif": "Tentative"},
        headers=admin,
    )
    assert reponse.status_code == 404
