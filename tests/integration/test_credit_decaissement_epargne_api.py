"""API Crédit — décaissement en mode 'epargne' : crédit direct sur un compte du tiers,
n'importe quel produit, sans transiter par la caisse.

  - crédite le bon compte (n'importe quel produit epargne.accounts, pas figé sur un type) ;
  - écriture D CREDIT / C EPARGNE sur le journal OD (virement interne, pas de mouvement de
    caisse) ;
  - le mouvement sur le compte crédité est identifiable (operation_type='decaissement_credit',
    label avec le numéro de dossier) ;
  - ownership : refuse un compte qui n'appartient pas à CE tiers, ou fermé ;
  - cohérence du corps (mode/compte_epargne_id) validée avant tout traitement ;
  - le rapprochement épargne concorde toujours après ce type de mouvement ;
  - transaction unique : un échéancier impossible ne laisse rien à moitié, y compris le solde
    du compte épargne (pas seulement la pièce comptable et l'échéancier de crédit).
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
from app.modules.credit.models import Application, Installment
from app.modules.credit.models import Product as CreditProduct
from app.modules.epargne import service as epargne_service
from app.modules.epargne.models import Product as EpargneProduct
from app.modules.epargne.models import SavingsAccount, SavingsMovement
from app.modules.epargne.rapprochement import rapprocher
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
        {"n": f"M-DEP-{uuid.uuid4().hex[:6]}", "a": agence.id, "s": statut},
    ).scalar_one()
    nat = db.execute(text("SELECT id FROM parameters.countries LIMIT 1")).scalar_one()
    db.execute(
        text(
            "INSERT INTO tiers.individual_profiles "
            "(tier_id, last_name, first_name, birth_date, gender, nationality_id) "
            "VALUES (:t, 'Ba', 'Kadiatou', '1985-01-01', 'F', :nat)"
        ),
        {"t": tier_id, "nat": nat},
    )
    return tier_id


def _produit_credit(db: Session, **kwargs: object) -> CreditProduct:
    produit = CreditProduct(
        code=f"DEP{uuid.uuid4().hex[:5]}",
        name="Crédit test décaissement compte",
        compte_credit_membre_id=_cid(db, "202211"),
        compte_credit_client_id=_cid(db, "202221"),
        compte_produits_interets_id=_cid(db, "7021"),
        taux_bp=1200,
        **kwargs,
    )
    db.add(produit)
    db.flush()
    return produit


def _produit_epargne(db: Session) -> EpargneProduct:
    produit = EpargneProduct(
        code=f"EAV{uuid.uuid4().hex[:5]}",
        name="Épargne à vue test",
        type="a_vue",
        compte_epargne_id=_cid(db, "251111"),
        compte_epargne_client_id=_cid(db, "251121"),
    )
    db.add(produit)
    db.flush()
    return produit


def _compte_epargne(
    db: Session, agence: Agency, tier_id: uuid.UUID, produit: EpargneProduct
) -> SavingsAccount:
    return epargne_service.ouvrir_compte(
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


def _demande_approuvee(
    db: Session,
    agence: Agency,
    tier_id: uuid.UUID,
    produit: CreditProduct,
    montant_demande: int = 300000,
    montant_decide: int | None = None,
    duree_echeances: int = 12,
) -> uuid.UUID:
    demande = creer_demande(
        db, tier_id=tier_id, agency_id=agence.id, product_id=produit.id,
        montant_demande=montant_demande, duree_echeances=duree_echeances,
        objet="Test décaissement compte", par=None,
    )
    decider(
        db, demande, decision="approuve",
        montant_decide=montant_decide or montant_demande, motif="Dossier complet", par=None,
    )
    db.commit()
    return demande.id


# --- Décaissement en mode 'epargne' : le compte choisi est crédité --------------------------


def test_decaissement_epargne_credite_le_bon_compte(client: TestClient, db: Session) -> None:
    agence = _agence(db, "DEA1")
    tier_id = _tier(db, agence)
    produit_credit = _produit_credit(db)
    produit_epargne = _produit_epargne(db)
    compte = _compte_epargne(db, agence, tier_id, produit_epargne)
    demande_id = _demande_approuvee(db, agence, tier_id, produit_credit, montant_demande=300000)
    responsable = _entete(db, agence, "RESPONSABLE_AGENCE")

    reponse = client.post(
        f"/credit/demandes/{demande_id}/decaissement",
        json={"mode": "epargne", "compte_epargne_id": str(compte.id)},
        headers=responsable,
    )

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["mode_decaissement"] == "epargne"
    assert corps["compte_destination_number"] == compte.account_number

    solde = db.execute(
        text("SELECT balance FROM epargne.accounts WHERE id = :c"), {"c": compte.id}
    ).scalar_one()
    assert solde == 300000

    demande = db.get(Application, demande_id)
    assert demande.mode_decaissement == "epargne"
    assert demande.compte_destination_id is not None


def test_decaissement_epargne_ecriture_sur_journal_od_sans_caisse(
    client: TestClient, db: Session
) -> None:
    agence = _agence(db, "DEA2")
    tier_id = _tier(db, agence)
    produit_credit = _produit_credit(db)
    produit_epargne = _produit_epargne(db)
    compte = _compte_epargne(db, agence, tier_id, produit_epargne)
    demande_id = _demande_approuvee(db, agence, tier_id, produit_credit, montant_demande=250000)
    responsable = _entete(db, agence, "RESPONSABLE_AGENCE")

    reponse = client.post(
        f"/credit/demandes/{demande_id}/decaissement",
        json={"mode": "epargne", "compte_epargne_id": str(compte.id)},
        headers=responsable,
    )
    numero_dossier = reponse.json()["application_number"]

    # Filtré sur CE dossier : le collectif (251111/251121) est PARTAGÉ par d'autres comptes du
    # tiers/tests dans la base de dev, une requête non filtrée trouverait plusieurs lignes.
    ligne = db.execute(
        text(
            "SELECT j.code, jl.side, jl.amount "
            "FROM comptabilite.journal_lines jl "
            "JOIN comptabilite.journal_entries je ON je.id = jl.entry_id "
            "JOIN comptabilite.journals j ON j.id = je.journal_id "
            "WHERE jl.account_id = :c AND je.description LIKE :desc"
        ),
        {"c": compte.compte_collectif_id, "desc": f"%{numero_dossier}%"},
    ).one()
    journal_code, side, amount = ligne
    assert journal_code == "OD"  # virement interne, PAS le journal de caisse
    assert side == "C"
    assert amount == 250000

    # La caisse de l'agence n'a REÇU aucune ligne pour CE décaissement précis.
    caisse_touchee = db.execute(
        text(
            "SELECT 1 FROM comptabilite.journal_lines jl "
            "JOIN comptabilite.journal_entries je ON je.id = jl.entry_id "
            "WHERE jl.account_id = :caisse AND je.description LIKE :desc"
        ),
        {"caisse": agence.compte_caisse_id, "desc": f"%{numero_dossier}%"},
    ).first()
    assert caisse_touchee is None


def test_decaissement_epargne_mouvement_identifiable(client: TestClient, db: Session) -> None:
    agence = _agence(db, "DEA3")
    tier_id = _tier(db, agence)
    produit_credit = _produit_credit(db)
    produit_epargne = _produit_epargne(db)
    compte = _compte_epargne(db, agence, tier_id, produit_epargne)
    demande_id = _demande_approuvee(db, agence, tier_id, produit_credit, montant_demande=180000)
    responsable = _entete(db, agence, "RESPONSABLE_AGENCE")

    reponse = client.post(
        f"/credit/demandes/{demande_id}/decaissement",
        json={"mode": "epargne", "compte_epargne_id": str(compte.id)},
        headers=responsable,
    )
    numero_dossier = reponse.json()["application_number"]

    mouvement = db.execute(
        select(SavingsMovement).where(SavingsMovement.account_id == compte.id)
    ).scalar_one()
    assert mouvement.operation_type == "decaissement_credit"
    assert mouvement.sens == "credit"
    assert mouvement.amount == 180000
    assert mouvement.balance_after == 180000
    assert numero_dossier in mouvement.label


# --- Ownership : le compte doit appartenir à CE tiers, être ouvert -------------------------


def test_decaissement_epargne_compte_dun_autre_tiers_refuse(
    client: TestClient, db: Session
) -> None:
    agence = _agence(db, "DEA4")
    tier_id = _tier(db, agence)
    autre_tier_id = _tier(db, agence)
    produit_credit = _produit_credit(db)
    produit_epargne = _produit_epargne(db)
    compte_dautrui = _compte_epargne(db, agence, autre_tier_id, produit_epargne)
    demande_id = _demande_approuvee(db, agence, tier_id, produit_credit)
    responsable = _entete(db, agence, "RESPONSABLE_AGENCE")

    reponse = client.post(
        f"/credit/demandes/{demande_id}/decaissement",
        json={"mode": "epargne", "compte_epargne_id": str(compte_dautrui.id)},
        headers=responsable,
    )

    assert reponse.status_code == 422
    assert "n'appartient pas" in reponse.json()["detail"].lower()


def test_decaissement_epargne_compte_ferme_refuse(client: TestClient, db: Session) -> None:
    agence = _agence(db, "DEA5")
    tier_id = _tier(db, agence)
    produit_credit = _produit_credit(db)
    produit_epargne = _produit_epargne(db)
    compte = _compte_epargne(db, agence, tier_id, produit_epargne)
    epargne_service.cloturer_compte(db, compte, par=None)
    demande_id = _demande_approuvee(db, agence, tier_id, produit_credit)
    responsable = _entete(db, agence, "RESPONSABLE_AGENCE")

    reponse = client.post(
        f"/credit/demandes/{demande_id}/decaissement",
        json={"mode": "epargne", "compte_epargne_id": str(compte.id)},
        headers=responsable,
    )

    assert reponse.status_code == 422
    assert "fermé" in reponse.json()["detail"].lower()


# --- Cohérence du corps de la requête -------------------------------------------------------


def test_decaissement_epargne_sans_compte_choisi_refuse(client: TestClient, db: Session) -> None:
    agence = _agence(db, "DEA6")
    tier_id = _tier(db, agence)
    produit_credit = _produit_credit(db)
    demande_id = _demande_approuvee(db, agence, tier_id, produit_credit)
    responsable = _entete(db, agence, "RESPONSABLE_AGENCE")

    reponse = client.post(
        f"/credit/demandes/{demande_id}/decaissement",
        json={"mode": "epargne"},
        headers=responsable,
    )

    assert reponse.status_code == 422


def test_decaissement_caisse_avec_compte_epargne_id_refuse(client: TestClient, db: Session) -> None:
    agence = _agence(db, "DEA7")
    tier_id = _tier(db, agence)
    produit_credit = _produit_credit(db)
    produit_epargne = _produit_epargne(db)
    compte = _compte_epargne(db, agence, tier_id, produit_epargne)
    demande_id = _demande_approuvee(db, agence, tier_id, produit_credit)
    responsable = _entete(db, agence, "RESPONSABLE_AGENCE")

    reponse = client.post(
        f"/credit/demandes/{demande_id}/decaissement",
        json={"mode": "caisse", "compte_epargne_id": str(compte.id)},
        headers=responsable,
    )

    assert reponse.status_code == 422


# --- Le rapprochement concorde toujours -----------------------------------------------------


def test_rapprochement_concorde_apres_decaissement_sur_compte(
    client: TestClient, db: Session
) -> None:
    """Le collectif (251111/251121) est PARTAGÉ par d'autres comptes déjà présents dans la
    base de dev : on compare AVANT/APRÈS (delta), pas des valeurs absolues — même discipline
    que le rayon d'effet sur base de dev partagée déjà rencontré sur ce projet."""
    agence = _agence(db, "DEA8")
    tier_id = _tier(db, agence)
    produit_credit = _produit_credit(db)
    produit_epargne = _produit_epargne(db)
    compte = _compte_epargne(db, agence, tier_id, produit_epargne)
    demande_id = _demande_approuvee(db, agence, tier_id, produit_credit, montant_demande=400000)
    responsable = _entete(db, agence, "RESPONSABLE_AGENCE")

    avant = rapprocher(db, compte.compte_collectif_id)
    assert avant.concordant is True  # déjà concordant avant (sinon ce ne serait pas notre bug)

    reponse = client.post(
        f"/credit/demandes/{demande_id}/decaissement",
        json={"mode": "epargne", "compte_epargne_id": str(compte.id)},
        headers=responsable,
    )
    assert reponse.status_code == 200

    apres = rapprocher(db, compte.compte_collectif_id)
    assert apres.concordant is True
    assert apres.auxiliaire - avant.auxiliaire == 400000
    assert apres.general - avant.general == 400000


# --- Transaction unique : rien ne bouge, PAS MÊME LE SOLDE ÉPARGNE ---------------------------


def test_decaissement_epargne_echeancier_impossible_ne_laisse_rien_persister(
    client: TestClient, db: Session
) -> None:
    agence = _agence(db, "DEA9")
    tier_id = _tier(db, agence)
    produit_credit = _produit_credit(
        db, methode_amortissement="capital_constant", regle_arrondi="plus_proche"
    )
    produit_epargne = _produit_epargne(db)
    compte = _compte_epargne(db, agence, tier_id, produit_epargne)
    # Même cas pathologique que CR2/CR3 : montant=5, durée=10 -> impossible.
    demande_id = _demande_approuvee(
        db, agence, tier_id, produit_credit, montant_demande=5, montant_decide=5,
        duree_echeances=10,
    )
    responsable = _entete(db, agence, "RESPONSABLE_AGENCE")

    reponse = client.post(
        f"/credit/demandes/{demande_id}/decaissement",
        json={"mode": "epargne", "compte_epargne_id": str(compte.id)},
        headers=responsable,
    )

    assert reponse.status_code == 422

    solde = db.execute(
        text("SELECT balance FROM epargne.accounts WHERE id = :c"), {"c": compte.id}
    ).scalar_one()
    assert solde == 0  # le solde n'a PAS bougé

    aucun_mouvement = db.execute(
        select(SavingsMovement).where(SavingsMovement.account_id == compte.id)
    ).first()
    assert aucun_mouvement is None

    demande = db.get(Application, demande_id)
    assert demande.status == "approuve"
    assert demande.mode_decaissement == "caisse"  # jamais touché
    assert demande.compte_destination_id is None

    aucune_echeance = db.execute(
        select(Installment).where(Installment.application_id == demande_id)
    ).first()
    assert aucune_echeance is None
