"""API de gestion du référentiel produit de crédit (création, modification métier, validation,
activation) — chantier « gestion des produits », lot 3a (backend), miroir du lot 1 épargne
avec un garde-fou de validation STRICT propre au crédit.

  - credit.product.manage (ADMIN_FONCTIONNEL) garde ces 4 endpoints d'écriture + le référentiel
    en lecture (credit.product.read) — jamais compta.plan.manage, réservé aux rattachements/
    taux (lot 3b, pas encore construits) ;
  - code unique -> 422 clair, jamais une 500 (IntegrityError traduite) ;
  - GARDE-FOU STRICT : compte membre indispensable (422 si absent) ; SI taux_bp > 0, compte de
    produits d'intérêts aussi indispensable (422 si absent — sinon le produit casse au premier
    remboursement portant une part d'intérêts, remboursement.py) ; compte client optionnel par
    construction -> avertissement NON bloquant, jamais silencieux ;
  - taux d'usure : `taux_usure_max_bp` (migration 0050) revérifié à la création ET à la
    modification, NULL = pas de plafond (aucune valeur codée en dur) ;
  - activation/désactivation : motif obligatoire dans les deux sens.
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
from app.modules.credit.models import Product
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
        "code": f"PC{uuid.uuid4().hex[:6]}",
        "name": "Produit de test",
        **overrides,
    }
    produit = Product(**valeurs)
    db.add(produit)
    db.flush()
    return produit


CORPS_CREATION = {"code": "TSTC01", "name": "Crédit test"}


# --- Référentiel (liste de gestion, actifs + inactifs) -------------------------------------


def test_referentiel_inclut_un_produit_inactif(client: TestClient, db: Session) -> None:
    _produit(db, code="REF-INACTIF", is_active=False)
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")

    reponse = client.get("/credit/produits/referentiel", headers=admin)

    assert reponse.status_code == 200
    codes = {p["code"] for p in reponse.json()}
    assert "REF-INACTIF" in codes
    ligne = next(p for p in reponse.json() if p["code"] == "REF-INACTIF")
    assert ligne["is_active"] is False


def test_referentiel_sans_permission_403(client: TestClient, db: Session) -> None:
    # CAISSIER ne détient PAS credit.product.read (contrairement à epargne.product.read) —
    # bon candidat direct pour ce 403.
    caissier = _entete_auth(db, "CAISSIER")
    reponse = client.get("/credit/produits/referentiel", headers=caissier)
    assert reponse.status_code == 403


# --- Création ---------------------------------------------------------------------------


def test_creation_reussie_produit_provisoire(client: TestClient, db: Session) -> None:
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")

    reponse = client.post("/credit/produits", json=CORPS_CREATION, headers=admin)

    assert reponse.status_code == 201
    corps = reponse.json()
    assert corps["code"] == "TSTC01"
    assert corps["is_provisional"] is True
    assert corps["is_active"] is True
    assert corps["taux_bp"] == 0
    assert corps["taux_usure_max_bp"] is None
    # base_jours GELÉ : jamais saisi, reste à son défaut base (voir echeancier.py).
    assert corps["base_jours"] == 360

    ligne = db.execute(
        text(
            "SELECT action, resource_id FROM audit.audit_logs "
            "WHERE action = 'credit.product.created' AND resource_id = :r"
        ),
        {"r": corps["id"]},
    ).one()
    assert str(ligne.resource_id) == corps["id"]


def test_creation_code_duplique_422(client: TestClient, db: Session) -> None:
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")
    _produit(db, code="DUPC01")

    reponse = client.post(
        "/credit/produits", json={"code": "DUPC01", "name": "Doublon"}, headers=admin
    )

    assert reponse.status_code == 422
    assert "DUPC01" in reponse.json()["detail"]


def test_creation_base_jours_rejetee_422(client: TestClient, db: Session) -> None:
    """base_jours GELÉ (echeancier.py : calcul périodique, pas jour-par-jour) : n'existe plus
    dans le schéma de création — extra="forbid" REJETTE, plutôt que d'ignorer en silence."""
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")

    reponse = client.post(
        "/credit/produits",
        json={**CORPS_CREATION, "base_jours": 365},
        headers=admin,
    )

    assert reponse.status_code == 422
    assert db.execute(
        text("SELECT count(*) FROM credit.products WHERE code = :c"), {"c": "TSTC01"}
    ).scalar_one() == 0


def test_creation_sans_permission_403(client: TestClient, db: Session) -> None:
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.post("/credit/produits", json=CORPS_CREATION, headers=comptable)

    assert reponse.status_code == 403


def test_creation_taux_depasse_usure_422(client: TestClient, db: Session) -> None:
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")

    reponse = client.post(
        "/credit/produits",
        json={"code": "USUR01", "name": "Trop cher", "taux_bp": 3000, "taux_usure_max_bp": 2000},
        headers=admin,
    )

    assert reponse.status_code == 422
    assert db.execute(
        text("SELECT count(*) FROM credit.products WHERE code = :c"), {"c": "USUR01"}
    ).scalar_one() == 0


def test_creation_taux_usure_null_aucun_plafond(client: TestClient, db: Session) -> None:
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")

    reponse = client.post(
        "/credit/produits",
        json={"code": "USUR02", "name": "Sans plafond", "taux_bp": 9000},
        headers=admin,
    )

    assert reponse.status_code == 201
    assert reponse.json()["taux_bp"] == 9000
    assert reponse.json()["taux_usure_max_bp"] is None


# --- Modification métier -----------------------------------------------------------------


def _corps_modification(**overrides: object) -> dict[str, object]:
    # base_jours ABSENT à dessein (GELÉ, extra="forbid" le rejetterait) — voir
    # test_modification_base_jours_rejetee_422 pour la vérification dédiée.
    base = {
        "name": "Nouveau nom",
        "taux_bp": 500,
        "periodicite": "trimestrielle",
        "methode_amortissement": "capital_constant",
        "regle_arrondi": "plancher",
        "taux_usure_max_bp": None,
        "motif": "Refonte du produit",
    }
    base.update(overrides)
    return base


def test_modification_reussie_avec_motif_trace(client: TestClient, db: Session) -> None:
    produit = _produit(db, code="MODC01", name="Ancien nom")
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")

    reponse = client.patch(
        f"/credit/produits/{produit.id}", json=_corps_modification(), headers=admin
    )

    assert reponse.status_code == 200
    resultat = reponse.json()
    assert resultat["name"] == "Nouveau nom"
    assert resultat["methode_amortissement"] == "capital_constant"
    assert resultat["taux_bp"] == 500

    ligne = db.execute(
        text(
            "SELECT old_values, new_values FROM audit.audit_logs "
            "WHERE action = 'credit.product.updated' AND resource_id = :r"
        ),
        {"r": produit.id},
    ).one()
    assert ligne.old_values["name"] == "Ancien nom"
    assert ligne.new_values["motif"] == "Refonte du produit"


def test_modification_motif_absent_refusee(client: TestClient, db: Session) -> None:
    produit = _produit(db, code="MODC02")
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")

    reponse = client.patch(
        f"/credit/produits/{produit.id}", json=_corps_modification(motif=""), headers=admin
    )
    assert reponse.status_code == 422


def test_modification_sans_permission_403(client: TestClient, db: Session) -> None:
    produit = _produit(db, code="MODC03")
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.patch(
        f"/credit/produits/{produit.id}", json=_corps_modification(), headers=comptable
    )
    assert reponse.status_code == 403


def test_modification_produit_introuvable_404(client: TestClient, db: Session) -> None:
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")
    reponse = client.patch(
        f"/credit/produits/{uuid.uuid4()}", json=_corps_modification(), headers=admin
    )
    assert reponse.status_code == 404


def test_modification_base_jours_rejetee_422(client: TestClient, db: Session) -> None:
    """Même garde-fou qu'à la création : base_jours n'existe plus dans le schéma de
    modification — extra="forbid" rejette, la valeur en base reste inchangée (360)."""
    produit = _produit(db, code="MODC-BJ")
    db.commit()  # checkpoint : le rollback de la requête (422) ne doit pas emporter ce setup.
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")

    reponse = client.patch(
        f"/credit/produits/{produit.id}",
        json=_corps_modification(base_jours=365),
        headers=admin,
    )

    assert reponse.status_code == 422
    db.refresh(produit)
    assert produit.base_jours == 360


def test_modification_taux_depasse_usure_422(client: TestClient, db: Session) -> None:
    produit = _produit(db, code="MODC-USUR", taux_usure_max_bp=1000)
    db.commit()  # checkpoint : le rollback de la requête (422) ne doit pas emporter ce setup.
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")

    reponse = client.patch(
        f"/credit/produits/{produit.id}",
        json=_corps_modification(taux_bp=1500, taux_usure_max_bp=1000),
        headers=admin,
    )

    assert reponse.status_code == 422
    db.refresh(produit)
    assert produit.taux_bp != 1500


# --- Validation (garde-fou STRICT) ---------------------------------------------------------


def test_validation_refusee_si_compte_membre_manquant(client: TestClient, db: Session) -> None:
    produit = _produit(db, code="VALC01", compte_credit_membre_id=None)
    db.commit()  # checkpoint : le rollback de la requête (422) ne doit pas emporter ce setup.
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")

    reponse = client.post(f"/credit/produits/{produit.id}/valider", headers=admin)

    assert reponse.status_code == 422
    assert "compte membre" in reponse.json()["detail"]


def test_validation_refusee_si_taux_non_nul_sans_compte_interets(
    client: TestClient, db: Session
) -> None:
    """LE test clé du garde-fou strict : membre présent, taux non nul, intérêts absent."""
    membre = _compte(db, "451111")
    produit = _produit(
        db, code="VALC02", compte_credit_membre_id=membre.id,
        taux_bp=500, compte_produits_interets_id=None,
    )
    db.commit()
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")

    reponse = client.post(f"/credit/produits/{produit.id}/valider", headers=admin)

    assert reponse.status_code == 422
    assert "produits d'intérêts" in reponse.json()["detail"]
    db.refresh(produit)
    assert produit.is_provisional is True


def test_validation_ok_si_taux_nul_sans_compte_interets(client: TestClient, db: Session) -> None:
    """Taux nul : pas besoin du compte d'intérêts (miroir du comportement épargne)."""
    membre = _compte(db, "451121")
    produit = _produit(
        db, code="VALC03", compte_credit_membre_id=membre.id,
        taux_bp=0, compte_produits_interets_id=None,
    )
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")

    reponse = client.post(f"/credit/produits/{produit.id}/valider", headers=admin)

    assert reponse.status_code == 200
    assert reponse.json()["is_provisional"] is False


def test_validation_ok_avec_avertissement_si_compte_client_manquant(
    client: TestClient, db: Session
) -> None:
    membre = _compte(db, "451131")
    interets = _compte(db, "751132", account_class=7, normal_side="C")
    produit = _produit(
        db, code="VALC04", compte_credit_membre_id=membre.id,
        taux_bp=500, compte_produits_interets_id=interets.id,
        compte_credit_client_id=None,
    )
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")

    reponse = client.post(f"/credit/produits/{produit.id}/valider", headers=admin)

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["is_provisional"] is False
    assert len(corps["avertissements"]) == 1
    assert "compte client" in corps["avertissements"][0].lower()


def test_validation_ok_sans_avertissement_si_tout_rattache(
    client: TestClient, db: Session
) -> None:
    membre = _compte(db, "451141")
    client_compte = _compte(db, "451142")
    interets = _compte(db, "751143", account_class=7, normal_side="C")
    produit = _produit(
        db, code="VALC05", compte_credit_membre_id=membre.id,
        compte_credit_client_id=client_compte.id,
        taux_bp=500, compte_produits_interets_id=interets.id,
    )
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")

    reponse = client.post(f"/credit/produits/{produit.id}/valider", headers=admin)

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["is_provisional"] is False
    assert corps["avertissements"] == []


def test_validation_sans_permission_403(client: TestClient, db: Session) -> None:
    membre = _compte(db, "451151")
    produit = _produit(db, code="VALC06", compte_credit_membre_id=membre.id)
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.post(f"/credit/produits/{produit.id}/valider", headers=comptable)
    assert reponse.status_code == 403


def test_validation_produit_introuvable_404(client: TestClient, db: Session) -> None:
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")
    reponse = client.post(f"/credit/produits/{uuid.uuid4()}/valider", headers=admin)
    assert reponse.status_code == 404


# --- Activation / désactivation du catalogue -----------------------------------------------


def test_desactivation_avec_motif(client: TestClient, db: Session) -> None:
    produit = _produit(db, code="ACTC01")
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")

    reponse = client.patch(
        f"/credit/produits/{produit.id}/activation",
        json={"is_active": False, "motif": "Produit retiré du catalogue"},
        headers=admin,
    )

    assert reponse.status_code == 200
    assert reponse.json()["is_active"] is False

    ligne = db.execute(
        text(
            "SELECT new_values FROM audit.audit_logs "
            "WHERE action = 'credit.product.deactivated' AND resource_id = :r"
        ),
        {"r": produit.id},
    ).one()
    assert ligne.new_values["motif"] == "Produit retiré du catalogue"


def test_reactivation_avec_motif(client: TestClient, db: Session) -> None:
    produit = _produit(db, code="ACTC02", is_active=False)
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")

    reponse = client.patch(
        f"/credit/produits/{produit.id}/activation",
        json={"is_active": True, "motif": "Produit remis au catalogue"},
        headers=admin,
    )

    assert reponse.status_code == 200
    assert reponse.json()["is_active"] is True


def test_activation_motif_absent_refusee(client: TestClient, db: Session) -> None:
    produit = _produit(db, code="ACTC03")
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")

    reponse = client.patch(
        f"/credit/produits/{produit.id}/activation",
        json={"is_active": False, "motif": ""},
        headers=admin,
    )
    assert reponse.status_code == 422


def test_activation_sans_permission_403(client: TestClient, db: Session) -> None:
    produit = _produit(db, code="ACTC04")
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.patch(
        f"/credit/produits/{produit.id}/activation",
        json={"is_active": False, "motif": "Tentative"},
        headers=comptable,
    )
    assert reponse.status_code == 403


def test_activation_produit_introuvable_404(client: TestClient, db: Session) -> None:
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")
    reponse = client.patch(
        f"/credit/produits/{uuid.uuid4()}/activation",
        json={"is_active": False, "motif": "Tentative"},
        headers=admin,
    )
    assert reponse.status_code == 404
