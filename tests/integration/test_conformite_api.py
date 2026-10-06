"""API des ratios prudentiels RCSFD (lot P2.1.a) — tableau de bord (volet 1,
`conformite.ratio.read`) et paramétrage CRUD (volet 2, `conformite.ratio.manage`).

PREUVE CENTRALE (« rien en dur ») : `test_ratio_cree_via_api_se_calcule_sans_une_ligne_de_code`
paramètre deux agrégats et un ratio ENTIÈREMENT par l'API, poste des écritures réelles, et lit
un ratio conforme calculé — aucune des trois entités n'existe dans le code applicatif.

SINGLETON `parametre_institution` : le volet 2 demande explicitement « GET + PATCH, pas de
POST/DELETE ». Le test « le singleton refuse un second enregistrement » vérifie donc la
contrainte CHECK + UNIQUE directement en base (comme à l'étape 1, scratch DB), PAS via l'API —
puisqu'aucune route POST n'existe par construction. Signalé dans le rapport de livraison.
"""

import uuid
from collections.abc import Generator
from datetime import date
from decimal import Decimal
from typing import cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.cli.seed_conformite import executer_seed_conformite
from app.core.database import engine, get_db
from app.main import app
from app.modules.comptabilite import ecritures, journee
from app.modules.comptabilite.ecritures import LigneSaisie
from app.modules.comptabilite.models import Account, Journal
from app.modules.conformite.models import ParametreInstitution
from app.modules.security.jwt import creer_access_token
from app.modules.security.models import Role, User, UserRole
from app.modules.security.password import hasher_mot_de_passe

pytestmark = pytest.mark.integration

AUJOURDHUI = date(2026, 6, 15)


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


@pytest.fixture(autouse=True)
def _journee_ouverte(request: pytest.FixtureRequest) -> None:
    if "db" not in request.fixturenames:
        return
    db = request.getfixturevalue("db")
    journee.ouvrir_journee(db, journee.prochaine_date_ouvree(db), None)


def _agence_id(db: Session) -> uuid.UUID:
    resultat = db.execute(text("SELECT id FROM parameters.agencies LIMIT 1")).scalar_one()
    return cast(uuid.UUID, resultat)


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


def _compte(db: Session, numero: str, *, normal_side: str) -> Account:
    compte = Account(
        account_number=numero,
        name=f"Compte test {numero}",
        account_class=int(numero[0]),
        normal_side=normal_side,
        is_posting=True,
        is_system=False,
    )
    db.add(compte)
    db.flush()
    return compte


def _journal_id(db: Session, code: str) -> uuid.UUID:
    return db.execute(select(Journal.id).where(Journal.code == code)).scalar_one()


def _valider_od(db: Session, lignes: list[LigneSaisie], entry_date: date) -> None:
    entry = ecritures.creer_brouillon(
        db,
        journal_id=_journal_id(db, "OD"),
        entry_date=entry_date,
        description="Mouvement de test (API conformité)",
        lignes=lignes,
        par=None,
    )
    ecritures.valider(db, entry, None)


# --- Volet 1 — Lecture (tableau de bord) ----------------------------------------------------


def test_lecture_renvoie_les_10_ratios_2_calcules_8_en_attente(
    client: TestClient, db: Session
) -> None:
    executer_seed_conformite(db)
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.get("/conformite/ratios", headers=comptable)

    assert reponse.status_code == 200
    corps = reponse.json()["ratios"]
    assert len(corps) == 10
    actifs = [r for r in corps if r["actif"]]
    en_attente = [r for r in corps if not r["actif"]]
    assert len(actifs) == 2
    assert len(en_attente) == 8
    assert {r["code"] for r in actifs} == {
        "RATIO_1_COUVERTURE_RISQUES",
        "RATIO_5_DIVISION_RISQUES",
    }
    for ratio in en_attente:
        assert ratio["statut"] == "NON_CALCULABLE"
        assert ratio["conforme"] is None
        assert ratio["valeur_numerateur"] is None
        assert ratio["avertissements"] == []  # jamais évalué : rien à avertir


def test_lecture_enveloppe_leve_aucune_ecriture_sur_une_vraie_base_vide(
    client: TestClient, db: Session
) -> None:
    executer_seed_conformite(db)
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.get("/conformite/ratios?a_la_date=1900-01-01", headers=comptable)

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["aucune_ecriture_validee"] is True
    assert len(corps["ratios"]) == 10


def test_lecture_enveloppe_baisse_l_indicateur_des_qu_une_ecriture_existe(
    client: TestClient, db: Session
) -> None:
    executer_seed_conformite(db)
    comptable = _entete_auth(db, "COMPTABLE")
    caisse = db.execute(select(Account.id).where(Account.account_number == "101111")).scalar_one()
    depots = db.execute(select(Account.id).where(Account.account_number == "251121")).scalar_one()
    _valider_od(db, [LigneSaisie(caisse, "D", 100), LigneSaisie(depots, "C", 100)], AUJOURDHUI)

    url = f"/conformite/ratios?a_la_date={AUJOURDHUI.isoformat()}"
    reponse = client.get(url, headers=comptable)

    assert reponse.json()["aucune_ecriture_validee"] is False


def test_lecture_renvoie_les_avertissements_du_ratio_1_sur_depots_sans_capital(
    client: TestClient, db: Session
) -> None:
    executer_seed_conformite(db)
    comptable = _entete_auth(db, "COMPTABLE")
    caisse = db.execute(select(Account.id).where(Account.account_number == "101111")).scalar_one()
    depots = db.execute(select(Account.id).where(Account.account_number == "251121")).scalar_one()
    _valider_od(db, [LigneSaisie(caisse, "D", 5000), LigneSaisie(depots, "C", 5000)], AUJOURDHUI)

    url = f"/conformite/ratios?a_la_date={AUJOURDHUI.isoformat()}"
    reponse = client.get(url, headers=comptable)

    ratios = reponse.json()["ratios"]
    ratio_1 = next(r for r in ratios if r["code"] == "RATIO_1_COUVERTURE_RISQUES")
    assert ratio_1["statut"] == "CONFORME"  # le contrat de statut est inchangé
    assert {a["code"] for a in ratio_1["avertissements"]} == {
        "NUMERATEUR_NUL",
        "FONDS_PROPRES_NULS",
    }
    assert all(a["libelle"] for a in ratio_1["avertissements"])


def test_lecture_sans_permission_403(client: TestClient, db: Session) -> None:
    caissier = _entete_auth(db, "CAISSIER")
    reponse = client.get("/conformite/ratios", headers=caissier)
    assert reponse.status_code == 403


def test_direction_lit_le_tableau_de_bord(client: TestClient, db: Session) -> None:
    executer_seed_conformite(db)
    direction = _entete_auth(db, "DIRECTION_GENERALE")

    reponse = client.get("/conformite/ratios", headers=direction)

    assert reponse.status_code == 200
    assert len(reponse.json()["ratios"]) == 10


def test_detail_ratio_introuvable_404(client: TestClient, db: Session) -> None:
    comptable = _entete_auth(db, "COMPTABLE")
    reponse = client.get("/conformite/ratios/INEXISTANT", headers=comptable)
    assert reponse.status_code == 404


# --- Preuve centrale : paramétrage de bout en bout, aucune ligne de code -------------------


def test_ratio_cree_via_api_se_calcule_sans_une_ligne_de_code(
    client: TestClient, db: Session
) -> None:
    """Deux agrégats et un ratio, paramétrés UNIQUEMENT via l'API (aucun seed), évalués
    correctement — la preuve que le moteur ne connaît AUCUNE formule câblée."""
    numerateur = _compte(db, "9T10", normal_side="D")
    denominateur = _compte(db, "9T11", normal_side="C")
    contra = _compte(db, "9T12", normal_side="C")
    _valider_od(
        db, [LigneSaisie(numerateur.id, "D", 1000), LigneSaisie(contra.id, "C", 1000)], AUJOURDHUI
    )
    _valider_od(
        db, [LigneSaisie(contra.id, "D", 500), LigneSaisie(denominateur.id, "C", 500)], AUJOURDHUI
    )
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")

    rep_num = client.post(
        "/conformite/admin/agregats",
        json={
            "code": "TEST_NUM", "libelle": "Numérateur de test", "reference": None,
            "type": "BALANCE", "calcul_special": None, "nets_de_provisions": False,
            "applique_complement_provisions_tutelle": False,
            "composition": [{"prefixe_compte": "9T10", "sens": 1}],
            "motif": "Test API bout en bout",
        },
        headers=admin,
    )
    assert rep_num.status_code == 201

    rep_denom = client.post(
        "/conformite/admin/agregats",
        json={
            "code": "TEST_DENOM", "libelle": "Dénominateur de test", "reference": None,
            "type": "BALANCE", "calcul_special": None, "nets_de_provisions": False,
            "applique_complement_provisions_tutelle": False,
            "composition": [{"prefixe_compte": "9T11", "sens": 1}],
            "motif": "Test API bout en bout",
        },
        headers=admin,
    )
    assert rep_denom.status_code == 201

    rep_ratio = client.post(
        "/conformite/admin/ratios",
        json={
            "code": "RATIO_TEST", "libelle": "Ratio de test", "reference_reglementaire": None,
            "agregat_numerateur_code": "TEST_NUM", "agregat_denominateur_code": "TEST_DENOM",
            "operateur": "GE", "actif": True, "ordre": 1,
            "motif": "Test API bout en bout",
        },
        headers=admin,
    )
    assert rep_ratio.status_code == 201
    ratio_id = rep_ratio.json()["id"]

    rep_seuil = client.post(
        f"/conformite/admin/ratios/{ratio_id}/seuils",
        json={"categorie_sfd": None, "valeur_seuil": "150", "motif": "Seuil de test"},
        headers=admin,
    )
    assert rep_seuil.status_code == 201

    comptable = _entete_auth(db, "COMPTABLE")
    reponse = client.get("/conformite/ratios/RATIO_TEST", headers=comptable)

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["valeur_numerateur"] == 1000
    assert corps["valeur_denominateur"] == 500
    assert Decimal(str(corps["valeur_ratio_pct"])) == Decimal("200.0000")
    assert corps["conforme"] is True
    assert corps["statut"] == "CONFORME"
    assert Decimal(str(corps["marge"])) == Decimal("50.0000")
    composant = corps["agregat_numerateur"]["composants"][0]
    assert composant == {
        "prefixe_compte": "9T10", "sens": 1, "solde": 1000, "contribution": 1000
    }


# --- Volet 2 — Paramétrage : permissions ----------------------------------------------------


def test_manage_refuse_comptable_403(client: TestClient, db: Session) -> None:
    comptable = _entete_auth(db, "COMPTABLE")
    reponse = client.get("/conformite/admin/agregats", headers=comptable)
    assert reponse.status_code == 403


def test_manage_autorise_admin_fonctionnel_200(client: TestClient, db: Session) -> None:
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")
    reponse = client.get("/conformite/admin/agregats", headers=admin)
    assert reponse.status_code == 200


# --- Volet 2 — Validations 422 --------------------------------------------------------------


def test_creation_agregat_special_sans_calcul_422(client: TestClient, db: Session) -> None:
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")
    reponse = client.post(
        "/conformite/admin/agregats",
        json={
            "code": "SPECIAL_SANS_CALCUL", "libelle": "X", "reference": None,
            "type": "SPECIAL", "calcul_special": None, "nets_de_provisions": False,
            "applique_complement_provisions_tutelle": False, "composition": [],
            "motif": "Test validation",
        },
        headers=admin,
    )
    assert reponse.status_code == 422


def test_creation_agregat_balance_avec_calcul_422(client: TestClient, db: Session) -> None:
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")
    reponse = client.post(
        "/conformite/admin/agregats",
        json={
            "code": "BALANCE_AVEC_CALCUL", "libelle": "X", "reference": None,
            "type": "BALANCE", "calcul_special": "UN_CALCUL", "nets_de_provisions": False,
            "applique_complement_provisions_tutelle": False, "composition": [],
            "motif": "Test validation",
        },
        headers=admin,
    )
    assert reponse.status_code == 422


def test_creation_agregat_code_deja_utilise_422(client: TestClient, db: Session) -> None:
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")
    corps_base: dict[str, object] = {
        "code": "DOUBLON_AGR", "libelle": "X", "reference": None, "type": "BALANCE",
        "calcul_special": None, "nets_de_provisions": False,
        "applique_complement_provisions_tutelle": False, "composition": [],
        "motif": "Test validation",
    }
    premiere = client.post("/conformite/admin/agregats", json=corps_base, headers=admin)
    assert premiere.status_code == 201

    reponse = client.post("/conformite/admin/agregats", json=corps_base, headers=admin)
    assert reponse.status_code == 422


def test_creation_ratio_operateur_invalide_422(client: TestClient, db: Session) -> None:
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")
    client.post(
        "/conformite/admin/agregats",
        json={
            "code": "AGR_OP", "libelle": "X", "reference": None, "type": "BALANCE",
            "calcul_special": None, "nets_de_provisions": False,
            "applique_complement_provisions_tutelle": False, "composition": [],
            "motif": "Test",
        },
        headers=admin,
    )
    reponse = client.post(
        "/conformite/admin/ratios",
        json={
            "code": "RATIO_OP", "libelle": "X", "reference_reglementaire": None,
            "agregat_numerateur_code": "AGR_OP", "agregat_denominateur_code": "AGR_OP",
            "operateur": "EQ", "actif": True, "ordre": 500, "motif": "Test",
        },
        headers=admin,
    )
    assert reponse.status_code == 422


def test_creation_ratio_agregat_inexistant_422(client: TestClient, db: Session) -> None:
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")
    reponse = client.post(
        "/conformite/admin/ratios",
        json={
            "code": "RATIO_FANTOME", "libelle": "X", "reference_reglementaire": None,
            "agregat_numerateur_code": "N_EXISTE_PAS", "agregat_denominateur_code": "N_EXISTE_PAS",
            "operateur": "GE", "actif": True, "ordre": 501, "motif": "Test",
        },
        headers=admin,
    )
    assert reponse.status_code == 422


def test_creation_seuil_negatif_422(client: TestClient, db: Session) -> None:
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")
    client.post(
        "/conformite/admin/agregats",
        json={
            "code": "AGR_SEUIL", "libelle": "X", "reference": None, "type": "BALANCE",
            "calcul_special": None, "nets_de_provisions": False,
            "applique_complement_provisions_tutelle": False, "composition": [],
            "motif": "Test",
        },
        headers=admin,
    )
    rep_ratio = client.post(
        "/conformite/admin/ratios",
        json={
            "code": "RATIO_SEUIL", "libelle": "X", "reference_reglementaire": None,
            "agregat_numerateur_code": "AGR_SEUIL", "agregat_denominateur_code": "AGR_SEUIL",
            "operateur": "GE", "actif": True, "ordre": 502, "motif": "Test",
        },
        headers=admin,
    )
    ratio_id = rep_ratio.json()["id"]

    reponse = client.post(
        f"/conformite/admin/ratios/{ratio_id}/seuils",
        json={"categorie_sfd": None, "valeur_seuil": "-10", "motif": "Test"},
        headers=admin,
    )
    assert reponse.status_code == 422


def test_creation_seuil_categorie_hors_enum_422(client: TestClient, db: Session) -> None:
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")
    client.post(
        "/conformite/admin/agregats",
        json={
            "code": "AGR_CAT", "libelle": "X", "reference": None, "type": "BALANCE",
            "calcul_special": None, "nets_de_provisions": False,
            "applique_complement_provisions_tutelle": False, "composition": [],
            "motif": "Test",
        },
        headers=admin,
    )
    rep_ratio = client.post(
        "/conformite/admin/ratios",
        json={
            "code": "RATIO_CAT", "libelle": "X", "reference_reglementaire": None,
            "agregat_numerateur_code": "AGR_CAT", "agregat_denominateur_code": "AGR_CAT",
            "operateur": "GE", "actif": True, "ordre": 503, "motif": "Test",
        },
        headers=admin,
    )
    ratio_id = rep_ratio.json()["id"]

    reponse = client.post(
        f"/conformite/admin/ratios/{ratio_id}/seuils",
        json={"categorie_sfd": "HORS_ENUM", "valeur_seuil": "10", "motif": "Test"},
        headers=admin,
    )
    assert reponse.status_code == 422


def test_suppression_agregat_reference_422(client: TestClient, db: Session) -> None:
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")
    rep_agr = client.post(
        "/conformite/admin/agregats",
        json={
            "code": "AGR_REF", "libelle": "X", "reference": None, "type": "BALANCE",
            "calcul_special": None, "nets_de_provisions": False,
            "applique_complement_provisions_tutelle": False, "composition": [],
            "motif": "Test",
        },
        headers=admin,
    )
    agregat_id = rep_agr.json()["id"]
    client.post(
        "/conformite/admin/ratios",
        json={
            "code": "RATIO_REF", "libelle": "X", "reference_reglementaire": None,
            "agregat_numerateur_code": "AGR_REF", "agregat_denominateur_code": "AGR_REF",
            "operateur": "GE", "actif": True, "ordre": 504, "motif": "Test",
        },
        headers=admin,
    )

    reponse = client.post(
        f"/conformite/admin/agregats/{agregat_id}/retirer",
        json={"motif": "Tentative"},
        headers=admin,
    )
    assert reponse.status_code == 422


# --- Singleton parametre_institution ---------------------------------------------------------


def test_singleton_refuse_un_deuxieme_enregistrement_en_base(
    client: TestClient, db: Session
) -> None:
    """Le volet 2 n'expose AUCUNE route POST pour ce singleton (demande explicite) — la
    garantie « au plus une ligne » est donc vérifiée directement en base (CHECK + UNIQUE sur
    `singleton`), exactement comme à l'étape 1 (validation en scratch DB)."""
    executer_seed_conformite(db)
    db.flush()

    with pytest.raises(IntegrityError):
        db.add(ParametreInstitution(categorie_sfd="AFFILIE", complement_provisions_tutelle=0))
        db.flush()


def test_lecture_parametre_institution(client: TestClient, db: Session) -> None:
    executer_seed_conformite(db)
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")

    reponse = client.get("/conformite/admin/parametre-institution", headers=admin)

    assert reponse.status_code == 200
    assert reponse.json()["categorie_sfd"] == "NON_AFFILIE"


def test_modification_parametre_institution(client: TestClient, db: Session) -> None:
    executer_seed_conformite(db)
    admin = _entete_auth(db, "ADMIN_FONCTIONNEL")

    reponse = client.patch(
        "/conformite/admin/parametre-institution",
        json={
            "categorie_sfd": "AFFILIE", "complement_provisions_tutelle": 5000,
            "motif": "Changement de catégorie réglementaire",
        },
        headers=admin,
    )

    assert reponse.status_code == 200
    assert reponse.json()["categorie_sfd"] == "AFFILIE"
    assert reponse.json()["complement_provisions_tutelle"] == 5000
