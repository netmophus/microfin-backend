"""API — Clôture TECHNIQUE d'un exercice comptable (chantier P1, lot b1).

  - solde chaque compte de classe 6/7 mouvementé sur l'exercice, contrepartie nette en 591
    (« Excédent ou déficit en instance d'approbation ») — JAMAIS 592 ni 58 (lot b2, séparé) ;
  - l'écriture de clôture est posée dans le journal OD, datée au dernier jour de l'exercice ;
  - refuse tant qu'il reste un brouillon dans l'exercice (quel que soit son journal) ;
  - refuse si rien à clôturer (aucun mouvement de charges/produits) ;
  - refuse une deuxième clôture (définitive, jamais de réouverture) ;
  - une fois clos, l'exercice refuse toute nouvelle écriture datée dedans (moteur existant,
    ecritures.exercice_ouvert_pour — pas de code nouveau pour ce verrouillage) ;
  - permissions : compta.exercice.manage (déjà attribuée à COMPTABLE), 403 sinon.

Exercice de test TOUJOURS synthétique (2099), jamais le "2026" réel de la base de dev partagée
— le clôturer casserait le travail d'autres chantiers (voir blast-radius-comptes-partages).
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
from app.modules.comptabilite import ecritures
from app.modules.comptabilite.ecritures import LigneSaisie
from app.modules.comptabilite.models import Account, Exercice, Journal
from app.modules.security.jwt import creer_access_token
from app.modules.security.models import Role, User, UserRole
from app.modules.security.password import hasher_mot_de_passe

pytestmark = pytest.mark.integration

DATE_DEBUT = date(2099, 1, 1)
DATE_FIN = date(2099, 12, 31)
JOUR = date(2099, 6, 15)


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


def _entete(db: Session, role_code: str) -> dict[str, str]:
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
    normal_side = "D" if numero.startswith("6") else "C"
    valeurs = {
        "account_number": numero,
        "name": f"Compte {numero}",
        "account_class": int(numero[0]),
        "normal_side": normal_side,
        "is_posting": True,
        "is_system": False,
        **overrides,
    }
    compte = Account(**valeurs)
    db.add(compte)
    db.flush()
    return compte


def _journal_id(db: Session, code: str) -> uuid.UUID:
    return db.execute(select(Journal.id).where(Journal.code == code)).scalar_one()


def _exercice(db: Session, code: str = "TEST-2099") -> Exercice:
    exercice = Exercice(
        code=code, label="Exercice de test", date_debut=DATE_DEBUT, date_fin=DATE_FIN
    )
    db.add(exercice)
    db.flush()
    return exercice


def _valider_od(db: Session, lignes: list[LigneSaisie], entry_date: date = JOUR) -> None:
    """Pose et valide directement via le moteur — plus court que de passer par l'API OD pour
    les scénarios qui n'exercent pas cette API elle-même."""
    entry = ecritures.creer_brouillon(
        db, journal_id=_journal_id(db, "OD"), entry_date=entry_date,
        description="Mouvement de test", lignes=lignes, par=None,
    )
    ecritures.valider(db, entry, None)


# --- Aperçu (dry-run) ------------------------------------------------------------------------


def test_apercu_sans_mouvement_est_vide_mais_cloturable(client: TestClient, db: Session) -> None:
    exercice = _exercice(db)
    db.commit()
    comptable = _entete(db, "COMPTABLE")

    reponse = client.get(
        f"/comptabilite/exercices/{exercice.id}/previsualisation-cloture", headers=comptable
    )

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["resultat"] == 0
    assert corps["lignes"] == []
    assert corps["brouillons_bloquants"] == []
    assert corps["cloturable"] is True
    assert corps["compte_resultat"] == "591"


def test_apercu_calcule_lexcedent_et_le_detail_par_compte(client: TestClient, db: Session) -> None:
    charge = _compte(db, "6T950")
    produit = _compte(db, "7T950")
    exercice = _exercice(db)
    _valider_od(db, [LigneSaisie(charge.id, "D", 40000), LigneSaisie(produit.id, "C", 40000)])
    _valider_od(db, [LigneSaisie(produit.id, "C", 60000), LigneSaisie(charge.id, "D", 60000)])
    db.commit()
    comptable = _entete(db, "COMPTABLE")

    reponse = client.get(
        f"/comptabilite/exercices/{exercice.id}/previsualisation-cloture", headers=comptable
    )

    assert reponse.status_code == 200
    corps = reponse.json()
    # 100 000 de produits, 100 000 de charges : résultat nul malgré le mouvement (cas limite).
    assert corps["resultat"] == 0
    comptes = {ligne["account_number"]: ligne for ligne in corps["lignes"]}
    assert comptes["6T950"]["side"] == "C" and comptes["6T950"]["amount"] == 100000
    assert comptes["7T950"]["side"] == "D" and comptes["7T950"]["amount"] == 100000


def test_apercu_excedent_strict(client: TestClient, db: Session) -> None:
    charge = _compte(db, "6T951")
    produit = _compte(db, "7T951")
    tresorerie = _compte(db, "5T951", account_class=5, normal_side="D")
    exercice = _exercice(db)
    _valider_od(db, [LigneSaisie(charge.id, "D", 30000), LigneSaisie(produit.id, "C", 30000)])
    # Produit supplémentaire SANS contrepartie charge (couplé à la trésorerie, hors 6/7) ->
    # excédent net de 15 000.
    _valider_od(db, [LigneSaisie(tresorerie.id, "D", 15000), LigneSaisie(produit.id, "C", 15000)])
    db.commit()
    comptable = _entete(db, "COMPTABLE")

    reponse = client.get(
        f"/comptabilite/exercices/{exercice.id}/previsualisation-cloture", headers=comptable
    )
    corps = reponse.json()
    assert corps["resultat"] == 15000
    comptes = {ligne["account_number"]: ligne for ligne in corps["lignes"]}
    assert comptes["7T951"]["side"] == "D" and comptes["7T951"]["amount"] == 45000
    assert comptes["6T951"]["side"] == "C" and comptes["6T951"]["amount"] == 30000


def test_apercu_signale_les_brouillons_bloquants(client: TestClient, db: Session) -> None:
    charge = _compte(db, "6T953")
    exercice = _exercice(db)
    ecritures.creer_brouillon(
        db, journal_id=_journal_id(db, "OD"), entry_date=JOUR,
        description="Brouillon oublié", lignes=[LigneSaisie(charge.id, "D", 1000)], par=None,
    )
    db.commit()
    comptable = _entete(db, "COMPTABLE")

    reponse = client.get(
        f"/comptabilite/exercices/{exercice.id}/previsualisation-cloture", headers=comptable
    )

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["cloturable"] is False
    assert len(corps["brouillons_bloquants"]) == 1
    assert corps["brouillons_bloquants"][0]["description"] == "Brouillon oublié"


def test_apercu_exercice_introuvable_404(client: TestClient, db: Session) -> None:
    comptable = _entete(db, "COMPTABLE")

    reponse = client.get(
        f"/comptabilite/exercices/{uuid.uuid4()}/previsualisation-cloture", headers=comptable
    )

    assert reponse.status_code == 404


def test_apercu_sans_permission_refuse_403(client: TestClient, db: Session) -> None:
    exercice = _exercice(db)
    db.commit()
    caissier = _entete(db, "CAISSIER")

    reponse = client.get(
        f"/comptabilite/exercices/{exercice.id}/previsualisation-cloture", headers=caissier
    )

    assert reponse.status_code == 403


# --- Liste des exercices -----------------------------------------------------------------


def test_liste_les_exercices(client: TestClient, db: Session) -> None:
    exercice = _exercice(db)
    db.commit()
    comptable = _entete(db, "COMPTABLE")

    reponse = client.get("/comptabilite/exercices", headers=comptable)

    assert reponse.status_code == 200
    codes = {e["code"] for e in reponse.json()}
    assert exercice.code in codes


# --- Clôture -------------------------------------------------------------------------------


def test_cloture_pose_lecriture_et_passe_lexercice_a_clos(
    client: TestClient, db: Session
) -> None:
    charge = _compte(db, "6T960")
    produit = _compte(db, "7T960")
    exercice = _exercice(db)
    # Excédent net de 12 000 : 50 000 de produits contre 38 000 de charges.
    _valider_od(db, [LigneSaisie(charge.id, "D", 38000), LigneSaisie(produit.id, "C", 38000)])
    # Produit supplémentaire sans contrepartie charge : couplé à un compte hors 6/7 (trésorerie)
    # pour rester une pièce valide (le moteur exige l'équilibre à la validation).
    tresorerie = _compte(db, "5T960", account_class=5, normal_side="D")
    _valider_od(db, [LigneSaisie(tresorerie.id, "D", 12000), LigneSaisie(produit.id, "C", 12000)])
    db.commit()
    comptable = _entete(db, "COMPTABLE")

    apercu = client.get(
        f"/comptabilite/exercices/{exercice.id}/previsualisation-cloture", headers=comptable
    ).json()
    assert apercu["resultat"] == 12000
    assert apercu["cloturable"] is True

    reponse = client.post(
        f"/comptabilite/exercices/{exercice.id}/cloture", headers=comptable
    )

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["resultat"] == 12000
    assert corps["exercice"]["status"] == "clos"
    assert corps["entry_number"] is not None

    # Vérifié directement en base : la ligne 591 est bien créditée de l'excédent.
    ligne_591 = db.execute(
        text(
            "SELECT jl.side, jl.amount FROM comptabilite.journal_lines jl "
            "JOIN comptabilite.journal_entries je ON je.id = jl.entry_id "
            "JOIN comptabilite.accounts a ON a.id = jl.account_id "
            "WHERE je.entry_number = :n AND a.account_number = '591'"
        ),
        {"n": corps["entry_number"]},
    ).one()
    assert ligne_591.side == "C"
    assert ligne_591.amount == 12000

    # L'exercice est bien 'clos' en base.
    statut = db.execute(
        text("SELECT status FROM comptabilite.exercices WHERE id = :id"), {"id": exercice.id}
    ).scalar_one()
    assert statut == "clos"


def test_cloture_deficit_debite_591(client: TestClient, db: Session) -> None:
    charge = _compte(db, "6T961")
    tresorerie = _compte(db, "5T961", account_class=5, normal_side="D")
    exercice = _exercice(db)
    # Charge sans contrepartie produit -> déficit de 7 000.
    _valider_od(db, [LigneSaisie(charge.id, "D", 7000), LigneSaisie(tresorerie.id, "C", 7000)])
    db.commit()
    comptable = _entete(db, "COMPTABLE")

    reponse = client.post(f"/comptabilite/exercices/{exercice.id}/cloture", headers=comptable)

    assert reponse.status_code == 200
    assert reponse.json()["resultat"] == -7000
    ligne_591 = db.execute(
        text(
            "SELECT jl.side, jl.amount FROM comptabilite.journal_lines jl "
            "JOIN comptabilite.journal_entries je ON je.id = jl.entry_id "
            "JOIN comptabilite.accounts a ON a.id = jl.account_id "
            "WHERE je.entry_number = :n AND a.account_number = '591'"
        ),
        {"n": reponse.json()["entry_number"]},
    ).one()
    assert ligne_591.side == "D"
    assert ligne_591.amount == 7000


def test_cloture_refusee_si_brouillon_en_attente(client: TestClient, db: Session) -> None:
    charge = _compte(db, "6T962")
    exercice = _exercice(db)
    ecritures.creer_brouillon(
        db, journal_id=_journal_id(db, "OD"), entry_date=JOUR,
        description="Oublié avant clôture", lignes=[LigneSaisie(charge.id, "D", 1000)], par=None,
    )
    db.commit()
    comptable = _entete(db, "COMPTABLE")

    reponse = client.post(f"/comptabilite/exercices/{exercice.id}/cloture", headers=comptable)

    assert reponse.status_code == 422
    assert "brouillon" in reponse.json()["detail"].lower()
    # L'exercice reste ouvert.
    assert db.execute(
        text("SELECT status FROM comptabilite.exercices WHERE id = :id"), {"id": exercice.id}
    ).scalar_one() == "ouvert"


def test_cloture_refusee_si_rien_a_clorer(client: TestClient, db: Session) -> None:
    exercice = _exercice(db)
    db.commit()
    comptable = _entete(db, "COMPTABLE")

    reponse = client.post(f"/comptabilite/exercices/{exercice.id}/cloture", headers=comptable)

    assert reponse.status_code == 422
    assert "rien à clôturer" in reponse.json()["detail"].lower()


def test_cloture_refusee_une_deuxieme_fois(client: TestClient, db: Session) -> None:
    charge = _compte(db, "6T963")
    tresorerie = _compte(db, "5T963", account_class=5, normal_side="D")
    exercice = _exercice(db)
    _valider_od(db, [LigneSaisie(charge.id, "D", 1000), LigneSaisie(tresorerie.id, "C", 1000)])
    db.commit()
    comptable = _entete(db, "COMPTABLE")
    premiere = client.post(f"/comptabilite/exercices/{exercice.id}/cloture", headers=comptable)
    assert premiere.status_code == 200

    reponse = client.post(f"/comptabilite/exercices/{exercice.id}/cloture", headers=comptable)

    assert reponse.status_code == 422
    assert "déjà clos" in reponse.json()["detail"].lower()


def test_exercice_clos_refuse_toute_nouvelle_ecriture(client: TestClient, db: Session) -> None:
    """Le verrouillage n'est PAS du code nouveau : ecritures.exercice_ouvert_pour le fait déjà
    (voir le diagnostic lot b1). Ce test prouve que le mécanisme MORD une fois l'exercice clos,
    pas seulement qu'il existe."""
    charge = _compte(db, "6T964")
    produit = _compte(db, "7T964")
    exercice = _exercice(db)
    _valider_od(db, [LigneSaisie(charge.id, "D", 500), LigneSaisie(produit.id, "C", 500)])
    db.commit()
    comptable = _entete(db, "COMPTABLE")
    client.post(f"/comptabilite/exercices/{exercice.id}/cloture", headers=comptable)

    reponse = client.post(
        "/comptabilite/ecritures",
        json={
            "entry_date": str(JOUR),
            "description": "Après clôture — doit être refusée",
            "lignes": [
                {"account_number": "6T964", "side": "D", "amount": 100, "label": None},
                {"account_number": "7T964", "side": "C", "amount": 100, "label": None},
            ],
        },
        headers=comptable,
    )

    assert reponse.status_code == 422


def test_cloture_exercice_introuvable_404(client: TestClient, db: Session) -> None:
    comptable = _entete(db, "COMPTABLE")

    reponse = client.post(
        f"/comptabilite/exercices/{uuid.uuid4()}/cloture", headers=comptable
    )

    assert reponse.status_code == 404


def test_cloture_sans_permission_refuse_403(client: TestClient, db: Session) -> None:
    exercice = _exercice(db)
    db.commit()
    caissier = _entete(db, "CAISSIER")

    reponse = client.post(f"/comptabilite/exercices/{exercice.id}/cloture", headers=caissier)

    assert reponse.status_code == 403
