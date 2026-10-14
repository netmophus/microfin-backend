"""Dépôt à terme, lot 2 : ouverture + mise de fonds en UNE transaction, durée choisie, échéance
dérivée de la journée comptable, conditions figées au contrat.

Aucune date du jour ici : tout part de la journée comptable ouverte par la fixture.
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
from app.modules.caisse.models import Poste, PosteAssignation
from app.modules.caisse.service import ouvrir_session
from app.modules.comptabilite import journee
from app.modules.epargne import service
from app.modules.epargne.guichet import OuvertureDepotATermeRefuseeError, ouvrir_depot_a_terme
from app.modules.epargne.models import Product, SavingsAccount
from app.modules.epargne.rapprochement import rapprocher
from app.modules.epargne.terme import ajouter_mois, interets_du_terme
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.security.jwt import creer_access_token, decoder_access_token
from app.modules.security.models import Role, User, UserRole
from app.modules.security.password import hasher_mot_de_passe

pytestmark = pytest.mark.integration

MONTANT = 1_000_000
TAUX_BP = 500
PENALITE_BP = 5000


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
    """Une journée comptable ouverte (VRAI service) : c'est d'elle que partent les échéances."""
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


def _membre(db: Session, agence: Agency, statut: str = "actif") -> uuid.UUID:
    return db.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES (:n, 'individual', :a, :s) RETURNING id"
        ),
        {"n": f"M-DT-{uuid.uuid4().hex[:8]}", "a": agence.id, "s": statut},
    ).scalar_one()


def _produit_dat(
    db: Session, *, bornes: tuple[int, int] | None = (3, 24), type_produit: str = "terme"
) -> Product:
    est_terme = type_produit == "terme"
    produit = Product(
        code=f"DT{uuid.uuid4().hex[:6]}", name="Dépôt à terme", type=type_produit,
        compte_epargne_id=_cid(db, "252111" if est_terme else "251111"),
        compte_charge_interet_id=_cid(db, "60252" if est_terme else "602511"),
        taux_bp=TAUX_BP, base_jours=365, regle_arrondi="plus_proche",
        methode_calcul_solde="fin_periode",
        duree_min_mois=bornes[0] if bornes and est_terme else None,
        duree_max_mois=bornes[1] if bornes and est_terme else None,
        penalite_anticipee_bp=PENALITE_BP if est_terme else 0,
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


def _ouvrir_session_caisse(
    db: Session, agence: Agency, entete: dict[str, str], code_poste: str = "01"
) -> None:
    jeton = entete["Authorization"].removeprefix("Bearer ")
    user_id = decoder_access_token(jeton).sub
    poste = Poste(
        agency_id=agence.id, code=code_poste, libelle="Caisse principale",
        compte_caisse_id=agence.compte_caisse_id,
    )
    db.add(poste)
    db.flush()
    db.add(PosteAssignation(poste_id=poste.id, user_id=user_id))
    db.flush()
    ouvrir_session(
        db,
        UtilisateurCourant(
            user_id=user_id, roles=(), permissions=frozenset(),
            primary_agency_id=agence.id, agency_id=agence.id, voit_tout=True,
        ),
        poste_id=poste.id, fonds_initial=0,
    )


class Cadre:
    """Une agence, un membre actif, un produit DAT, un CAISSIER (qui ouvre au guichet, avec sa
    session de caisse) et un responsable (retrait anticipé, avec la sienne)."""

    def __init__(self, db: Session, code: str, *, session: bool = True) -> None:
        self.db = db
        self.agence = _agence(db, code)
        self.tier_id = _membre(db, self.agence)
        self.produit = _produit_dat(db)
        self.caissier = _entete(db, self.agence, "CAISSIER")
        self.responsable = _entete(db, self.agence, "RESPONSABLE_AGENCE")
        if session:
            _ouvrir_session_caisse(db, self.agence, self.caissier, "01")
        _ouvrir_session_caisse(db, self.agence, self.responsable, "02")
        courant = journee.date_comptable_courante(db)
        assert courant is not None
        self.journee: date = courant

    def corps(self, mois: int = 12, montant: int = MONTANT) -> dict[str, object]:
        return {"product_id": str(self.produit.id), "term_months": mois, "montant": montant}

    def ouvrir(self, client: TestClient, **kw: int) -> object:
        return client.post(
            f"/tiers/{self.tier_id}/depots-a-terme", json=self.corps(**kw),
            headers=self.caissier,
        )


# --- Ouverture ------------------------------------------------------------------------


def test_l_ouverture_place_le_capital_et_derive_l_echeance_de_la_journee(
    client: TestClient, db: Session
) -> None:
    c = Cadre(db, "DT-O1")

    reponse = c.ouvrir(client, mois=12)

    assert reponse.status_code == 201, reponse.text
    corps = reponse.json()
    assert corps["compte"]["balance"] == MONTANT
    assert corps["compte"]["term_months"] == 12
    echeance = ajouter_mois(c.journee, 12)
    assert corps["compte"]["maturity_date"] == echeance.isoformat()
    jours = (echeance - c.journee).days
    attendu = interets_du_terme(MONTANT, TAUX_BP, jours, 365, "plus_proche")
    assert corps["interets_a_l_echeance"] == attendu > 0
    assert corps["entry_number"] is not None

    compte = db.get(SavingsAccount, uuid.UUID(corps["compte"]["id"]))
    assert compte is not None
    db.refresh(compte)
    assert compte.term_start_date == c.journee  # la journée comptable, pas la date du poste
    assert compte.principal == MONTANT
    mouvements = db.execute(
        text("SELECT sens, amount, operation_type, date_valeur FROM epargne.movements "
             "WHERE account_id = :a"),
        {"a": compte.id},
    ).all()
    assert mouvements == [("credit", MONTANT, "depot", c.journee)]
    assert service.verifier_coherence_solde(db, compte.id).coherent
    assert rapprocher(db, _cid(db, "252111")).concordant


def test_les_conditions_sont_figees_au_contrat(client: TestClient, db: Session) -> None:
    c = Cadre(db, "DT-O2")
    reponse = c.ouvrir(client, mois=6)
    assert reponse.status_code == 201, reponse.text
    compte_id = uuid.UUID(reponse.json()["compte"]["id"])

    # Le produit change ensuite : le DAT en cours ne bouge pas (calcul rejouable).
    c.produit.taux_bp = 900
    c.produit.penalite_anticipee_bp = 10_000
    c.produit.base_jours = 360
    c.produit.regle_arrondi = "plancher"
    db.flush()

    ligne = db.execute(
        text(
            "SELECT taux_bp_contrat, penalite_bp_contrat, base_jours_contrat, "
            "regle_arrondi_contrat FROM epargne.accounts WHERE id = :i"
        ),
        {"i": compte_id},
    ).one()
    assert tuple(ligne) == (TAUX_BP, PENALITE_BP, 365, "plus_proche")


@pytest.mark.parametrize("mois", [2, 25])
def test_une_duree_hors_des_bornes_du_produit_est_refusee(
    client: TestClient, db: Session, mois: int
) -> None:
    c = Cadre(db, f"DT-D{mois}")

    reponse = c.ouvrir(client, mois=mois)

    assert reponse.status_code == 422
    assert "de 3 à 24 mois" in reponse.json()["detail"]
    assert db.execute(
        text("SELECT count(*) FROM epargne.accounts WHERE tier_id = :t"), {"t": c.tier_id}
    ).scalar_one() == 0


def test_un_produit_sans_durees_parametrees_est_refuse(
    client: TestClient, db: Session
) -> None:
    c = Cadre(db, "DT-O3")
    c.produit = _produit_dat(db, bornes=None)

    reponse = c.ouvrir(client)

    assert reponse.status_code == 422
    assert "pas encore paramétrées" in reponse.json()["detail"]


@pytest.mark.parametrize("montant", [0, -5])
def test_un_capital_nul_ou_negatif_est_refuse(
    client: TestClient, db: Session, montant: int
) -> None:
    c = Cadre(db, f"DT-O4{-montant}")

    reponse = c.ouvrir(client, montant=montant)

    assert reponse.status_code == 422
    assert "capital supérieur à zéro" in reponse.json()["detail"]


def test_un_produit_a_vue_n_est_pas_un_depot_a_terme(client: TestClient, db: Session) -> None:
    c = Cadre(db, "DT-O5")
    c.produit = _produit_dat(db, type_produit="a_vue")

    reponse = c.ouvrir(client)

    assert reponse.status_code == 422
    assert "n'est pas un dépôt à terme" in reponse.json()["detail"]


def test_sans_session_de_caisse_rien_n_est_cree(client: TestClient, db: Session) -> None:
    c = Cadre(db, "DT-O6", session=False)

    reponse = c.ouvrir(client)

    assert reponse.status_code == 422
    assert db.execute(
        text("SELECT count(*) FROM epargne.accounts WHERE tier_id = :t"), {"t": c.tier_id}
    ).scalar_one() == 0


def test_un_prospect_ne_peut_pas_placer_un_depot_a_terme(
    client: TestClient, db: Session
) -> None:
    c = Cadre(db, "DT-O7")
    c.tier_id = _membre(db, c.agence, statut="prospect")

    reponse = c.ouvrir(client)

    assert reponse.status_code == 422
    assert "n'est pas actif" in reponse.json()["detail"]


def test_un_membre_hors_perimetre_est_introuvable(client: TestClient, db: Session) -> None:
    c = Cadre(db, "DT-O8")
    autre = _agence(db, "DT-O8B")
    c.tier_id = _membre(db, autre)

    assert c.ouvrir(client).status_code == 404


def test_l_ouverture_ordinaire_refuse_un_produit_a_terme(client: TestClient, db: Session) -> None:
    c = Cadre(db, "DT-O9")

    reponse = client.post(
        f"/tiers/{c.tier_id}/comptes-epargne", json={"product_id": str(c.produit.id)},
        headers=c.responsable,
    )

    assert reponse.status_code == 422
    assert "ouverture d'un dépôt à terme" in reponse.json()["detail"]


# --- Autorisation : il faut les DEUX droits (refus négatifs) --------------------------


@pytest.mark.parametrize("role", ["CHARGE_CLIENTELE", "RESPONSABLE_AGENCE"])
def test_sans_la_permission_dediee_l_ouverture_est_refusee(
    client: TestClient, db: Session, role: str
) -> None:
    """Le chargé de clientèle ouvre des comptes mais n'encaisse pas ; le responsable d'agence a
    account.open ET deposit, et même une session de caisse : sans epargne.dat.open, refus."""
    c = Cadre(db, f"DT-A{role[:3]}")
    entete = _entete(db, c.agence, role)
    _ouvrir_session_caisse(db, c.agence, entete, "03")

    reponse = client.post(
        f"/tiers/{c.tier_id}/depots-a-terme", json=c.corps(), headers=entete
    )

    assert reponse.status_code == 403
    assert db.execute(
        text("SELECT count(*) FROM epargne.accounts WHERE tier_id = :t"), {"t": c.tier_id}
    ).scalar_one() == 0


def test_le_caissier_n_ouvre_toujours_pas_de_compte_ordinaire(
    client: TestClient, db: Session
) -> None:
    c = Cadre(db, "DT-A5")
    produit_vue = _produit_dat(db, type_produit="a_vue")

    reponse = client.post(
        f"/tiers/{c.tier_id}/comptes-epargne", json={"product_id": str(produit_vue.id)},
        headers=c.caissier,
    )

    assert reponse.status_code == 403


def test_le_service_revérifie_la_permission_d_ouverture(db: Session) -> None:
    """Défense en profondeur : même appelé hors route, le service refuse sans epargne.dat.open."""
    c = Cadre(db, "DT-A6")
    uid = db.execute(text("SELECT id FROM security.users LIMIT 1")).scalar_one()
    sans_droit = UtilisateurCourant(
        user_id=uid, roles=("CAISSIER",),
        permissions=frozenset({"epargne.operation.deposit"}),
        primary_agency_id=c.agence.id, agency_id=c.agence.id, voit_tout=True,
    )

    with pytest.raises(OuvertureDepotATermeRefuseeError, match="pas le droit d'ouvrir"):
        ouvrir_depot_a_terme(
            db, sans_droit, tier_id=c.tier_id, agency_id=c.agence.id,
            product_id=c.produit.id, term_months=6, montant=MONTANT,
        )


def test_le_capital_entre_dans_la_caisse_de_la_session_du_caissier(
    client: TestClient, db: Session
) -> None:
    c = Cadre(db, "DT-A7")
    compte_caisse = db.execute(
        text("SELECT compte_caisse_id FROM caisse.postes WHERE agency_id = :a AND code = '01'"),
        {"a": c.agence.id},
    ).scalar_one()

    reponse = c.ouvrir(client)

    assert reponse.status_code == 201, reponse.text
    lignes = db.execute(
        text(
            "SELECT a.id, l.side, l.amount FROM comptabilite.journal_lines l "
            "JOIN comptabilite.journal_entries e ON e.id = l.entry_id "
            "JOIN comptabilite.accounts a ON a.id = l.account_id "
            "WHERE e.entry_number = :n"
        ),
        {"n": reponse.json()["entry_number"]},
    ).all()
    assert (compte_caisse, "D", MONTANT) in [tuple(ligne) for ligne in lignes]
    assert (_cid(db, "252111"), "C", MONTANT) in [tuple(ligne) for ligne in lignes]


def test_la_simulation_est_reservee_a_qui_peut_ouvrir_un_depot_a_terme(
    client: TestClient, db: Session
) -> None:
    c = Cadre(db, "DT-A2")
    chargee = _entete(db, c.agence, "CHARGE_CLIENTELE")  # account.open, mais pas dat.open

    assert client.post(
        "/epargne/depots-a-terme/simulation", json=c.corps(), headers=chargee
    ).status_code == 403
    assert client.post(
        "/epargne/depots-a-terme/simulation", json=c.corps(), headers=c.caissier
    ).status_code == 200


# --- Simulation ---------------------------------------------------------------------


def test_la_simulation_donne_l_echeance_et_les_interets_sans_rien_ecrire(
    client: TestClient, db: Session
) -> None:
    c = Cadre(db, "DT-S1")
    avant = db.execute(text("SELECT count(*) FROM epargne.accounts")).scalar_one()

    reponse = client.post(
        "/epargne/depots-a-terme/simulation", json=c.corps(mois=6), headers=c.caissier
    )

    assert reponse.status_code == 200, reponse.text
    corps = reponse.json()
    echeance = ajouter_mois(c.journee, 6)
    assert corps["maturity_date"] == echeance.isoformat()
    assert corps["journee_comptable"] == c.journee.isoformat()
    jours = (echeance - c.journee).days
    interets = interets_du_terme(MONTANT, TAUX_BP, jours, 365, "plus_proche")
    assert corps["interets_a_l_echeance"] == interets
    assert corps["total_a_l_echeance"] == MONTANT + interets
    assert (corps["duree_min_mois"], corps["duree_max_mois"]) == (3, 24)
    assert db.execute(text("SELECT count(*) FROM epargne.accounts")).scalar_one() == avant


def test_la_simulation_applique_les_memes_refus_que_l_ouverture(
    client: TestClient, db: Session
) -> None:
    c = Cadre(db, "DT-S2")

    reponse = client.post(
        "/epargne/depots-a-terme/simulation", json=c.corps(mois=1), headers=c.caissier
    )

    assert reponse.status_code == 422
    assert "de 3 à 24 mois" in reponse.json()["detail"]


# --- Le blocage s'applique dès l'ouverture ---------------------------------------------


def test_un_depot_a_terme_refuse_depot_et_retrait_ordinaires(
    client: TestClient, db: Session
) -> None:
    c = Cadre(db, "DT-B1")
    compte_id = c.ouvrir(client).json()["compte"]["id"]

    for route in ("depot", "retrait"):
        reponse = client.post(
            f"/epargne/comptes/{compte_id}/{route}", json={"montant": 1000},
            headers=c.responsable,
        )
        assert reponse.status_code == 422, route
        assert "dépôt à terme, bloqué jusqu'au" in reponse.json()["detail"]


# --- Dénouement et retrait anticipé : routes et autorisation --------------------------------


def _placer(client: TestClient, c: Cadre) -> str:
    reponse = c.ouvrir(client)
    assert reponse.status_code == 201, reponse.text
    return str(reponse.json()["compte"]["id"])


def test_le_caissier_ne_peut_pas_solder_avant_l_echeance_avec_penalite(
    client: TestClient, db: Session
) -> None:
    c = Cadre(db, "DT-R1")
    compte_id = _placer(client, c)
    caissier = _entete(db, c.agence, "CAISSIER")  # pas epargne.dat.retrait_anticipe

    reponse = client.post(f"/epargne/comptes/{compte_id}/retrait-anticipe", headers=caissier)

    assert reponse.status_code == 403
    solde = db.execute(
        text("SELECT balance FROM epargne.accounts WHERE id = :i"), {"i": compte_id}
    ).scalar_one()
    assert solde == MONTANT


def test_le_responsable_solde_par_anticipation_et_le_message_dit_le_detail(
    client: TestClient, db: Session
) -> None:
    c = Cadre(db, "DT-R2")
    compte_id = _placer(client, c)

    apercu = client.get(f"/epargne/comptes/{compte_id}/denouement/apercu", headers=c.responsable)
    assert apercu.status_code == 200, apercu.text
    assert apercu.json()["mode"] == "anticipe"

    reponse = client.post(f"/epargne/comptes/{compte_id}/retrait-anticipe", headers=c.responsable)

    assert reponse.status_code == 200, reponse.text
    corps = reponse.json()
    assert corps["mode"] == "anticipe"
    assert corps["total_paye"] == MONTANT  # le jour même : aucun intérêt couru
    assert corps["message"] == apercu.json()["message"]
    assert corps["message"].startswith("Retrait anticipé : capital 1 000 000 F")
    statut = db.execute(
        text("SELECT status, balance FROM epargne.accounts WHERE id = :i"), {"i": compte_id}
    ).one()
    assert tuple(statut) == ("cloture", 0)


def test_le_denouement_avant_l_echeance_est_refuse_en_422(
    client: TestClient, db: Session
) -> None:
    c = Cadre(db, "DT-R3")
    compte_id = _placer(client, c)

    reponse = client.post(f"/epargne/comptes/{compte_id}/denouement", headers=c.responsable)

    assert reponse.status_code == 422
    assert "n'arrive à échéance que le" in reponse.json()["detail"]


def test_un_compte_hors_perimetre_est_introuvable_pour_le_denouement(
    client: TestClient, db: Session
) -> None:
    c = Cadre(db, "DT-R4")
    compte_id = _placer(client, c)
    agence_autre = _agence(db, "DT-R4B")
    autre = _entete(db, agence_autre, "RESPONSABLE_AGENCE")
    _ouvrir_session_caisse(db, agence_autre, autre)  # la session précède le contrôle de périmètre

    assert client.get(
        f"/epargne/comptes/{compte_id}/denouement/apercu", headers=autre
    ).status_code == 404
    assert client.post(
        f"/epargne/comptes/{compte_id}/retrait-anticipe", headers=autre
    ).status_code == 404


def test_la_fermeture_ordinaire_est_refusee_sur_un_depot_a_terme(
    client: TestClient, db: Session
) -> None:
    c = Cadre(db, "DT-R5")
    compte_id = _placer(client, c)

    reponse = client.post(f"/epargne/comptes/{compte_id}/fermeture", headers=c.responsable)

    assert reponse.status_code == 422
    assert "dénouement à l'échéance" in reponse.json()["detail"]


def test_la_recherche_du_guichet_signale_l_echeance(client: TestClient, db: Session) -> None:
    c = Cadre(db, "DT-R6")
    compte_id = _placer(client, c)
    numero = db.execute(
        text("SELECT account_number FROM epargne.accounts WHERE id = :i"), {"i": compte_id}
    ).scalar_one()

    reponse = client.get(
        "/epargne/recherche-comptes", params={"q": numero}, headers=c.responsable
    )

    assert reponse.status_code == 200
    ligne = reponse.json()[0]
    assert ligne["product_type"] == "terme"
    assert ligne["maturity_date"] == ajouter_mois(c.journee, 12).isoformat()
    assert ligne["principal"] == MONTANT


def test_l_ecran_de_versement_des_interets_ne_propose_pas_les_produits_a_terme(
    client: TestClient, db: Session
) -> None:
    c = Cadre(db, "DT-R7")
    direction = _entete(db, c.agence, "DIRECTION_GENERALE")

    reponse = client.get("/epargne/interets/produits", headers=direction)

    assert reponse.status_code == 200
    assert str(c.produit.id) not in [p["id"] for p in reponse.json()]


def test_la_liste_des_produits_donne_les_bornes_de_duree_du_terme(
    client: TestClient, db: Session
) -> None:
    c = Cadre(db, "DT-L1")

    reponse = client.get("/epargne/produits", headers=c.responsable)

    assert reponse.status_code == 200
    ligne = next(p for p in reponse.json() if p["id"] == str(c.produit.id))
    assert (ligne["duree_min_mois"], ligne["duree_max_mois"]) == (3, 24)
