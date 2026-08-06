"""API Crédit — recherche du guichet de remboursement (CR6d) :

  - trouve un crédit décaissé par numéro de dossier, numéro de tiers ou nom (partiel,
    insensible à la casse) ;
  - un dossier entièrement soldé reste dans les résultats, `prochaine_echeance` absente
    (jamais un silence qui ressemble à « rien trouvé ») ;
  - un dossier non décaissé, ou hors du périmètre de l'acteur, n'apparaît pas ;
  - gardée sur credit.remboursement.create (le CAISSIER, seul acteur du guichet, ne détient
    pas credit.demande.read) — pas accessible à un chargé de prêt seul.
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.credit.decaissement import decaisser
from app.modules.credit.demandes import creer_demande, decider
from app.modules.credit.models import Installment
from app.modules.credit.models import Product as CreditProduct
from app.modules.credit.remboursement import rembourser
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


def _tier(
    db: Session, agence: Agency, last_name: str = "Ba", first_name: str = "Kadiatou"
) -> uuid.UUID:
    tier_id = db.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES (:n, 'individual', :a, 'actif') RETURNING id"
        ),
        {"n": f"M-REM-{uuid.uuid4().hex[:6]}", "a": agence.id},
    ).scalar_one()
    nat = db.execute(text("SELECT id FROM parameters.countries LIMIT 1")).scalar_one()
    db.execute(
        text(
            "INSERT INTO tiers.individual_profiles "
            "(tier_id, last_name, first_name, birth_date, gender, nationality_id) "
            "VALUES (:t, :ln, :fn, '1985-01-01', 'F', :nat)"
        ),
        {"t": tier_id, "ln": last_name, "fn": first_name, "nat": nat},
    )
    return tier_id


def _produit_credit(db: Session) -> CreditProduct:
    produit = CreditProduct(
        code=f"REM{uuid.uuid4().hex[:5]}",
        name="Crédit test recherche remboursement",
        compte_credit_membre_id=_cid(db, "202211"),
        compte_credit_client_id=_cid(db, "202221"),
        compte_produits_interets_id=_cid(db, "7021"),
        taux_bp=1200,
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


def _credit_decaisse(
    db: Session, agence: Agency, tier_id: uuid.UUID, produit: CreditProduct, montant: int = 300000
):
    demande = creer_demande(
        db, tier_id=tier_id, agency_id=agence.id, product_id=produit.id,
        montant_demande=montant, duree_echeances=6, objet="Test recherche remboursement", par=None,
    )
    decider(db, demande, decision="approuve", montant_decide=montant, motif="OK", par=None)
    decaisser(db, demande, par=None)
    db.commit()
    return demande


def _solder(db: Session, demande) -> None:
    while True:
        echeance = db.execute(
            select(Installment)
            .where(Installment.application_id == demande.id, Installment.status == "a_echoir")
            .order_by(Installment.numero)
            .limit(1)
        ).scalar_one_or_none()
        if echeance is None:
            return
        rembourser(db, demande, montant=echeance.total, par=None)
        db.commit()


def test_recherche_par_numero_de_dossier(client: TestClient, db: Session) -> None:
    agence = _agence(db, "REA1")
    tier_id = _tier(db, agence)
    produit = _produit_credit(db)
    demande = _credit_decaisse(db, agence, tier_id, produit, montant=300000)
    caissier = _entete(db, agence, "CAISSIER")

    reponse = client.get(
        "/credit/recherche-remboursement",
        params={"q": demande.application_number},
        headers=caissier,
    )

    assert reponse.status_code == 200
    resultats = reponse.json()
    assert len(resultats) == 1
    assert resultats[0]["application_number"] == demande.application_number
    assert resultats[0]["tier_nom"] == "Ba Kadiatou" or "Ba" in resultats[0]["tier_nom"]
    assert resultats[0]["prochaine_echeance"]["numero"] == 1
    premiere = db.execute(
        select(Installment).where(Installment.application_id == demande.id, Installment.numero == 1)
    ).scalar_one()
    assert resultats[0]["prochaine_echeance"]["total"] == premiere.total


def test_recherche_trouve_echeance_partiellement_payee_avec_le_solde_du(
    client: TestClient, db: Session
) -> None:
    """CR5b, garde-fou (c) : une échéance PARTIELLEMENT payée doit rester trouvable par la
    recherche du guichet, avec le SOLDE restant (pas le total d'origine) — une sélection sur
    status = 'a_echoir' seul la manquerait une fois le versement partiel posé."""
    agence = _agence(db, "REA9")
    tier_id = _tier(db, agence)
    produit = _produit_credit(db)
    demande = _credit_decaisse(db, agence, tier_id, produit, montant=100000)

    premiere = db.execute(
        select(Installment).where(
            Installment.application_id == demande.id, Installment.numero == 1
        )
    ).scalar_one()
    versement = premiere.total // 2
    rembourser(db, demande, montant=versement, par=None)
    db.commit()

    caissier = _entete(db, agence, "CAISSIER")
    reponse = client.get(
        "/credit/recherche-remboursement",
        params={"q": demande.application_number},
        headers=caissier,
    )

    assert reponse.status_code == 200
    resultats = reponse.json()
    assert len(resultats) == 1
    echeance = resultats[0]["prochaine_echeance"]
    assert echeance is not None
    assert echeance["numero"] == 1
    assert echeance["montant_paye"] == versement
    assert echeance["solde_du"] == premiere.total - versement


def test_recherche_par_numero_de_tiers(client: TestClient, db: Session) -> None:
    agence = _agence(db, "REA2")
    tier_id = _tier(db, agence)
    produit = _produit_credit(db)
    demande = _credit_decaisse(db, agence, tier_id, produit)
    numero_tier = db.execute(
        text("SELECT tier_number FROM tiers.tiers WHERE id = :t"), {"t": tier_id}
    ).scalar_one()
    caissier = _entete(db, agence, "CAISSIER")

    reponse = client.get(
        "/credit/recherche-remboursement", params={"q": numero_tier}, headers=caissier
    )

    assert reponse.status_code == 200
    resultats = reponse.json()
    assert any(r["application_number"] == demande.application_number for r in resultats)


def test_recherche_par_nom_partiel_insensible_a_la_casse(client: TestClient, db: Session) -> None:
    agence = _agence(db, "REA3")
    tier_id = _tier(db, agence, last_name="Diallo", first_name="Amadou")
    produit = _produit_credit(db)
    demande = _credit_decaisse(db, agence, tier_id, produit)
    caissier = _entete(db, agence, "CAISSIER")

    reponse = client.get(
        "/credit/recherche-remboursement", params={"q": "diallo"}, headers=caissier
    )

    assert reponse.status_code == 200
    resultats = reponse.json()
    assert any(r["application_number"] == demande.application_number for r in resultats)


def test_dossier_solde_apparait_sans_prochaine_echeance(client: TestClient, db: Session) -> None:
    agence = _agence(db, "REA4")
    tier_id = _tier(db, agence)
    produit = _produit_credit(db)
    demande = _credit_decaisse(db, agence, tier_id, produit, montant=100000)
    _solder(db, demande)
    caissier = _entete(db, agence, "CAISSIER")

    reponse = client.get(
        "/credit/recherche-remboursement",
        params={"q": demande.application_number},
        headers=caissier,
    )

    assert reponse.status_code == 200
    resultats = reponse.json()
    assert len(resultats) == 1
    assert resultats[0]["prochaine_echeance"] is None


def test_dossier_non_decaisse_absent_des_resultats(client: TestClient, db: Session) -> None:
    agence = _agence(db, "REA5")
    tier_id = _tier(db, agence)
    produit = _produit_credit(db)
    demande = creer_demande(
        db, tier_id=tier_id, agency_id=agence.id, product_id=produit.id,
        montant_demande=200000, duree_echeances=6, objet="Non décaissé", par=None,
    )
    decider(db, demande, decision="approuve", montant_decide=200000, motif="OK", par=None)
    db.commit()
    caissier = _entete(db, agence, "CAISSIER")

    reponse = client.get(
        "/credit/recherche-remboursement",
        params={"q": demande.application_number},
        headers=caissier,
    )

    assert reponse.status_code == 200
    assert reponse.json() == []


def test_hors_perimetre_absent(client: TestClient, db: Session) -> None:
    agence_a = _agence(db, "REA6A")
    agence_b = _agence(db, "REA6B")
    tier_id = _tier(db, agence_a)
    produit = _produit_credit(db)
    demande = _credit_decaisse(db, agence_a, tier_id, produit)
    caissier_autre_agence = _entete(db, agence_b, "CAISSIER")

    reponse = client.get(
        "/credit/recherche-remboursement",
        params={"q": demande.application_number},
        headers=caissier_autre_agence,
    )

    assert reponse.status_code == 200
    assert reponse.json() == []


def test_aucun_resultat_renvoie_liste_vide(client: TestClient, db: Session) -> None:
    agence = _agence(db, "REA7")
    caissier = _entete(db, agence, "CAISSIER")

    reponse = client.get(
        "/credit/recherche-remboursement", params={"q": "INTROUVABLE-XYZ"}, headers=caissier
    )

    assert reponse.status_code == 200
    assert reponse.json() == []


def test_charge_de_pret_seul_ne_peut_pas_chercher(client: TestClient, db: Session) -> None:
    """CHARGE_PRET a credit.demande.read mais PAS credit.remboursement.create — la recherche
    du guichet lui est fermée (403), pas une liste vide qui masquerait le vrai refus."""
    agence = _agence(db, "REA8")
    charge = _entete(db, agence, "CHARGE_PRET")

    reponse = client.get(
        "/credit/recherche-remboursement", params={"q": "peu importe"}, headers=charge
    )

    assert reponse.status_code == 403
