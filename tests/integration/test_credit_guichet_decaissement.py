"""Guichet de décaissement (crédit en espèces par le CAISSIER) :

  - la recherche (dossiers APPROUVÉS) et l'aperçu d'échéancier sont gardés par
    credit.decaissement.create — le caissier la détient, un rôle sans ce droit est refusé (403),
    même s'il peut lire les dossiers (credit.demande.read) ;
  - un dossier non approuvé ou hors de l'agence est introuvable ;
  - de bout en bout : un caissier avec session ouverte et caisse garnie décaisse, la caisse est
    débitée du montant accordé ;
  - sans session de caisse ouverte : refus 422 en langage clair, rien n'est écrit ;
  - caisse insuffisante : refus 422 (disponible/demandé), rien n'est écrit ; l'égalité passe.
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.caisse.models import Poste, PosteAssignation
from app.modules.caisse.service import ouvrir_session, solde_grand_livre
from app.modules.comptabilite import journee
from app.modules.credit.demandes import creer_demande, decider
from app.modules.credit.models import Application, Installment
from app.modules.credit.models import Product as CreditProduct
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.security.jwt import creer_access_token
from app.modules.security.models import Role, User, UserRole
from app.modules.security.password import hasher_mot_de_passe
from tests.integration.aides_caisse import alimenter_compte

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


@pytest.fixture(autouse=True)
def _journee_ouverte(request: pytest.FixtureRequest) -> None:
    if "db" not in request.fixturenames:
        return
    db = request.getfixturevalue("db")
    journee.ouvrir_journee(db, journee.prochaine_date_ouvree(db), None)


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


def _tier(db: Session, agence: Agency, last_name: str = "Ba", first_name: str = "Kadiatou") -> uuid.UUID:
    tier_id = db.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES (:n, 'individual', :a, 'actif') RETURNING id"
        ),
        {"n": f"M-DEC-{uuid.uuid4().hex[:6]}", "a": agence.id},
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


def _produit(db: Session, taux_tva_bp: int = 0) -> CreditProduct:
    produit = CreditProduct(
        code=f"GD{uuid.uuid4().hex[:6]}",
        name="Crédit test guichet décaissement",
        compte_credit_membre_id=_cid(db, "202211"),
        compte_credit_client_id=_cid(db, "202221"),
        compte_produits_interets_id=_cid(db, "7021"),
        taux_bp=1200,
        taux_tva_bp=taux_tva_bp,
        compte_tva_id=_cid(db, "332100") if taux_tva_bp else None,
    )
    db.add(produit)
    db.flush()
    return produit


def _approuvee(
    db: Session, agence: Agency, produit: CreditProduct, montant: int = 300_000
) -> Application:
    demande = creer_demande(
        db, tier_id=_tier(db, agence), agency_id=agence.id, product_id=produit.id,
        montant_demande=montant, duree_echeances=6, objet="Test guichet décaissement", par=None,
    )
    decider(db, demande, decision="approuve", montant_decide=montant, motif="OK", par=None)
    db.flush()
    return demande


def _utilisateur(db: Session, agence: Agency, role_code: str) -> tuple[User, dict[str, str]]:
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
    return user, {"Authorization": f"Bearer {jeton}"}


def _session_ouverte(db: Session, agence: Agency, user: User, fonds: int | None) -> uuid.UUID:
    """Poste + session OUVERTE pour ce caissier ; `fonds` = montant posé au grand livre sur la
    caisse du poste (None = ne rien poser). Rend le compte de caisse de la session."""
    poste = Poste(
        agency_id=agence.id, code="01", libelle="Guichet", compte_caisse_id=agence.compte_caisse_id
    )
    db.add(poste)
    db.flush()
    db.add(PosteAssignation(poste_id=poste.id, user_id=user.id))
    db.flush()
    session = ouvrir_session(
        db,
        UtilisateurCourant(
            user_id=user.id, roles=(), permissions=frozenset(),
            primary_agency_id=agence.id, agency_id=agence.id, voit_tout=False,
        ),
        poste_id=poste.id, fonds_initial=0,
    )
    if fonds is not None:
        alimenter_compte(db, session.compte_caisse_id, fonds)
    return session.compte_caisse_id


def _nb_lignes(db: Session) -> int:
    return db.execute(text("SELECT count(*) FROM comptabilite.journal_lines")).scalar_one()


# --- Recherche ---------------------------------------------------------------------------------


def test_recherche_caissier_trouve_un_dossier_approuve_par_numero_tiers_et_nom(
    client: TestClient, db: Session
) -> None:
    agence = _agence(db, "GDA1")
    demande = _approuvee(db, agence, _produit(db))
    _, entete = _utilisateur(db, agence, "CAISSIER")
    tier_number = db.execute(
        text("SELECT tier_number FROM tiers.tiers WHERE id = :t"), {"t": demande.tier_id}
    ).scalar_one()

    for q in (demande.application_number, tier_number, "kadiat"):
        reponse = client.get("/credit/recherche-decaissement", params={"q": q}, headers=entete)
        assert reponse.status_code == 200, q
        trouves = [d for d in reponse.json() if d["id"] == str(demande.id)]
        assert len(trouves) == 1, q
        assert trouves[0]["montant_decide"] == 300_000
        assert trouves[0]["duree_echeances"] == 6


def test_recherche_ignore_les_dossiers_non_approuves_et_hors_agence(
    client: TestClient, db: Session
) -> None:
    agence = _agence(db, "GDA2")
    autre = _agence(db, "GDA3")
    produit = _produit(db)
    en_instruction = creer_demande(
        db, tier_id=_tier(db, agence), agency_id=agence.id, product_id=produit.id,
        montant_demande=100_000, duree_echeances=6, objet="x", par=None,
    )
    ailleurs = _approuvee(db, autre, produit)
    _, entete = _utilisateur(db, agence, "CAISSIER")

    for demande in (en_instruction, ailleurs):
        reponse = client.get(
            "/credit/recherche-decaissement",
            params={"q": demande.application_number},
            headers=entete,
        )
        assert reponse.status_code == 200
        assert reponse.json() == []


@pytest.mark.parametrize("role", ["CHARGE_PRET", "MEMBRE_COMITE_CREDIT", "COMPTABLE"])
def test_recherche_refusee_sans_la_permission_de_decaisser(
    client: TestClient, db: Session, role: str
) -> None:
    """Pouvoir LIRE les dossiers (credit.demande.read) ne suffit pas : la recherche du guichet
    est gardée par la permission d'exécution."""
    agence = _agence(db, "GDA4")
    _, entete = _utilisateur(db, agence, role)

    reponse = client.get("/credit/recherche-decaissement", params={"q": "CR"}, headers=entete)
    assert reponse.status_code == 403

    apercu = client.get(
        f"/credit/recherche-decaissement/{uuid.uuid4()}/echeancier-apercu", headers=entete
    )
    assert apercu.status_code == 403


# --- Aperçu ------------------------------------------------------------------------------------


def test_apercu_guichet_porte_la_tva_et_refuse_un_dossier_non_approuve(
    client: TestClient, db: Session
) -> None:
    agence = _agence(db, "GDA5")
    demande = _approuvee(db, agence, _produit(db, taux_tva_bp=1900))
    en_instruction = creer_demande(
        db, tier_id=_tier(db, agence), agency_id=agence.id, product_id=demande.product_id,
        montant_demande=100_000, duree_echeances=6, objet="x", par=None,
    )
    _, entete = _utilisateur(db, agence, "CAISSIER")

    reponse = client.get(
        f"/credit/recherche-decaissement/{demande.id}/echeancier-apercu", headers=entete
    )
    assert reponse.status_code == 200
    lignes = reponse.json()
    assert len(lignes) == 6
    assert all(ligne["tva"] > 0 for ligne in lignes)
    assert all(ligne["total"] == ligne["capital"] + ligne["interets"] + ligne["tva"] for ligne in lignes)

    refuse = client.get(
        f"/credit/recherche-decaissement/{en_instruction.id}/echeancier-apercu", headers=entete
    )
    assert refuse.status_code == 404


# --- Exécution de bout en bout ----------------------------------------------------------------


def test_caissier_decaisse_en_especes_et_la_caisse_est_debitee(
    client: TestClient, db: Session
) -> None:
    agence = _agence(db, "GDA6")
    demande = _approuvee(db, agence, _produit(db), montant=300_000)
    caissier, entete = _utilisateur(db, agence, "CAISSIER")
    compte_caisse = _session_ouverte(db, agence, caissier, fonds=1_000_000)
    avant = solde_grand_livre(db, compte_caisse)

    reponse = client.post(f"/credit/demandes/{demande.id}/decaissement", json={}, headers=entete)

    assert reponse.status_code == 200, reponse.text
    assert reponse.json()["status"] == "decaisse"
    assert reponse.json()["mode_decaissement"] == "caisse"
    assert solde_grand_livre(db, compte_caisse) == avant - 300_000
    echeances = db.execute(
        select(Installment).where(Installment.application_id == demande.id)
    ).scalars().all()
    assert len(echeances) == 6


def test_decaissement_sans_session_ouverte_refuse_422_et_rien_ecrit(
    client: TestClient, db: Session
) -> None:
    agence = _agence(db, "GDA7")
    demande = _approuvee(db, agence, _produit(db))
    _, entete = _utilisateur(db, agence, "CAISSIER")
    lignes_avant = _nb_lignes(db)

    reponse = client.post(f"/credit/demandes/{demande.id}/decaissement", json={}, headers=entete)

    assert reponse.status_code == 422
    assert "session" in reponse.json()["detail"].lower()
    assert _nb_lignes(db) == lignes_avant


def test_decaissement_refuse_si_la_caisse_ne_contient_pas_le_montant(
    client: TestClient, db: Session
) -> None:
    agence = _agence(db, "GDA8")
    caissier, entete = _utilisateur(db, agence, "CAISSIER")
    compte_caisse = _session_ouverte(db, agence, caissier, fonds=100_000)
    disponible = solde_grand_livre(db, compte_caisse)
    demande = _approuvee(db, agence, _produit(db), montant=disponible + 1)

    reponse = client.post(f"/credit/demandes/{demande.id}/decaissement", json={}, headers=entete)

    assert reponse.status_code == 422
    detail = reponse.json()["detail"]
    assert detail.startswith("Solde de caisse insuffisant pour ce décaissement : disponible")
    assert "demande" not in detail and "{" not in detail  # langage humain, pas de symbole brut


def test_decaissement_egal_au_solde_de_la_caisse_accepte(
    client: TestClient, db: Session
) -> None:
    agence = _agence(db, "GDA9")
    caissier, entete = _utilisateur(db, agence, "CAISSIER")
    compte_caisse = _session_ouverte(db, agence, caissier, fonds=100_000)
    disponible = solde_grand_livre(db, compte_caisse)
    demande = _approuvee(db, agence, _produit(db), montant=disponible)

    reponse = client.post(f"/credit/demandes/{demande.id}/decaissement", json={}, headers=entete)

    assert reponse.status_code == 200, reponse.text
    assert solde_grand_livre(db, compte_caisse) == 0
