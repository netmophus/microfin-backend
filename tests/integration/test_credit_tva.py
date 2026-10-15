"""Crédit — TVA sur les intérêts (migration 0069).

- compte d'État 332100 « TVA collectée à reverser » présent dans le plan, avec son mapping ;
- paramètre produit `taux_tva_bp` (défaut 19 % à la création par l'API, 0 = exonéré) et compte
  TVA rattaché, exigé à la VALIDATION seulement si le taux est > 0 ;
- échéancier : TVA par échéance = arrondi(taux x intérêts), total TTC = capital + intérêts +
  TVA, capital et intérêts identiques à un produit exonéré ;
- remboursement plein / partiel : D caisse / C crédit / C 7021 / C 332100 ; la somme des TVA
  des versements partiels d'une échéance égale exactement la TVA de l'échéance ;
- solde anticipé : TVA sur les intérêts courus, jamais sur le capital ;
- produit exonéré : aucune ligne de TVA, comportement inchangé.
"""

import uuid
from collections.abc import Generator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.caisse.models import CaisseSession, Poste, PosteAssignation
from app.modules.caisse.service import ouvrir_session
from app.modules.comptabilite import journee
from app.modules.comptabilite.models import Account
from app.modules.credit.decaissement import RattachementManquantError, decaisser, generer_apercu
from app.modules.credit.demandes import creer_demande, decider
from app.modules.credit.echeancier import (
    calculer_tva,
    generer_echeancier,
    repartir_bloc_interets,
)
from app.modules.credit.gestion_produits import ComptesNonRattachesError, valider_produit
from app.modules.credit.models import Application, Installment, Product, Repayment
from app.modules.credit.remboursement import (
    encours_actuel,
    rembourser,
    solder_par_anticipation,
)
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.security.jwt import creer_access_token
from app.modules.security.models import Role, User, UserRole
from app.modules.security.password import hasher_mot_de_passe

pytestmark = pytest.mark.integration

TAUX_TVA_BP = 1900


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


def _tier(db: Session, agence: Agency) -> uuid.UUID:
    tier_id = db.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES (:n, 'individual', :a, 'actif') RETURNING id"
        ),
        {"n": f"M-TVA-{uuid.uuid4().hex[:6]}", "a": agence.id},
    ).scalar_one()
    nat = db.execute(text("SELECT id FROM parameters.countries LIMIT 1")).scalar_one()
    db.execute(
        text(
            "INSERT INTO tiers.individual_profiles "
            "(tier_id, last_name, first_name, birth_date, gender, nationality_id) "
            "VALUES (:t, 'Toure', 'Aminata', '1985-01-01', 'F', :nat)"
        ),
        {"t": tier_id, "nat": nat},
    )
    return tier_id


def _produit(
    db: Session,
    *,
    taux_tva_bp: int = TAUX_TVA_BP,
    avec_compte_tva: bool = True,
    regle_arrondi: str = "plus_proche",
) -> Product:
    produit = Product(
        code=f"TVA{uuid.uuid4().hex[:5]}",
        name="Crédit test TVA",
        compte_credit_membre_id=_cid(db, "202211"),
        compte_credit_client_id=_cid(db, "202221"),
        compte_produits_interets_id=_cid(db, "7021"),
        compte_tva_id=_cid(db, "332100") if avec_compte_tva else None,
        taux_bp=1200,
        taux_tva_bp=taux_tva_bp,
        methode_amortissement="capital_constant",
        periodicite="mensuelle",
        regle_arrondi=regle_arrondi,
    )
    db.add(produit)
    db.flush()
    return produit


def _entete(db: Session, agence: Agency, role_code: str) -> dict[str, str]:
    role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
    suffixe = uuid.uuid4().hex[:8]
    user = User(
        matricule=f"MAT-{suffixe}",
        email=f"{suffixe}@ex.com",
        username=f"u{suffixe}",
        password_hash=hasher_mot_de_passe("Motdepasse!123"),
        last_name="T",
        first_name="A",
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


def _ouvrir_session_caisse(db: Session, agence: Agency) -> uuid.UUID:
    uid = db.execute(text("SELECT id FROM security.users LIMIT 1")).scalar_one()
    deja_ouverte = db.execute(
        select(CaisseSession.id).where(
            CaisseSession.caissier_id == uid, CaisseSession.status == "ouverte"
        )
    ).first()
    if deja_ouverte is None:
        poste = Poste(
            agency_id=agence.id,
            code="01",
            libelle="Caisse principale",
            compte_caisse_id=agence.compte_caisse_id,
        )
        db.add(poste)
        db.flush()
        db.add(PosteAssignation(poste_id=poste.id, user_id=uid))
        db.flush()
        ouvrir_session(
            db,
            UtilisateurCourant(
                user_id=uid,
                roles=(),
                permissions=frozenset(),
                primary_agency_id=agence.id,
                agency_id=agence.id,
                voit_tout=True,
            ),
            poste_id=poste.id,
            fonds_initial=0,
        )
    return uid


def _credit_decaisse(
    db: Session,
    agence: Agency,
    produit: Product,
    *,
    montant: int = 300_000,
    duree_echeances: int = 3,
    entry_date: date = date(2026, 1, 5),
) -> Application:
    demande = creer_demande(
        db,
        tier_id=_tier(db, agence),
        agency_id=agence.id,
        product_id=produit.id,
        montant_demande=montant,
        duree_echeances=duree_echeances,
        objet="Test TVA",
        par=None,
    )
    decider(db, demande, decision="approuve", montant_decide=montant, motif="OK", par=None)
    par = _ouvrir_session_caisse(db, agence)
    decaisser(db, demande, par=par, entry_date=entry_date)
    demande.disbursed_at = datetime(entry_date.year, entry_date.month, entry_date.day, tzinfo=UTC)
    db.commit()
    return demande


def _installments(db: Session, demande: Application) -> list[Installment]:
    return list(
        db.execute(
            select(Installment)
            .where(Installment.application_id == demande.id)
            .order_by(Installment.numero)
        ).scalars()
    )


def _solde_compte(db: Session, account_id: uuid.UUID) -> int:
    lignes = db.execute(
        text(
            "SELECT side, amount FROM comptabilite.journal_lines "
            "JOIN comptabilite.journal_entries je ON je.id = entry_id "
            "WHERE account_id = :a AND je.status = 'validee'"
        ),
        {"a": account_id},
    ).all()
    return sum(m if s == "D" else -m for s, m in lignes)


# --- Compte 332100 ---------------------------------------------------------------------------


def test_le_compte_332100_existe_avec_sa_structure_et_son_mapping(db: Session) -> None:
    compte = db.execute(select(Account).where(Account.account_number == "332100")).scalar_one()
    parent = db.get(Account, compte.parent_id)

    assert compte.name == "État, TVA collectée à reverser"
    assert (compte.account_class, compte.normal_side, compte.is_posting) == (3, "C", True)
    assert parent is not None and parent.account_number == "332"
    mapping = db.execute(
        text(
            "SELECT etat, masse FROM comptabilite.financial_statement_mapping WHERE account_id = :a"
        ),
        {"a": compte.id},
    ).one()
    assert (mapping.etat, mapping.masse) == ("BILAN", "PASSIF")


# --- Calcul pur ------------------------------------------------------------------------------


def _echeancier(taux_tva_bp: int, regle: str = "plus_proche") -> list:  # type: ignore[type-arg]
    return generer_echeancier(
        montant=300_000,
        taux_bp=1200,
        duree_echeances=3,
        periodicite="mensuelle",
        methode_amortissement="capital_constant",
        regle_arrondi=regle,
        taux_tva_bp=taux_tva_bp,
    )


def test_echeancier_avec_tva_est_coherent_et_ne_change_ni_capital_ni_interets() -> None:
    exonere = _echeancier(0)
    avec_tva = _echeancier(TAUX_TVA_BP)

    assert [(e.capital, e.interets, e.capital_restant_du) for e in avec_tva] == [
        (e.capital, e.interets, e.capital_restant_du) for e in exonere
    ]
    assert all(e.tva == 0 and e.total == e.capital + e.interets for e in exonere)
    for e in avec_tva:
        assert e.tva == int((Decimal(TAUX_TVA_BP) * e.interets / 10000).quantize(Decimal(1)))
        assert e.total == e.capital + e.interets + e.tva
    # 3 000 / 2 000 / 1 000 F d'intérêts -> 570 / 380 / 190 F de TVA.
    assert [e.tva for e in avec_tva] == [570, 380, 190]
    assert sum(e.capital for e in avec_tva) == 300_000


def test_arrondi_de_la_tva_suit_la_regle_du_produit() -> None:
    assert calculer_tva(1_001, 1900, "plus_proche") == 190  # 190,19
    assert calculer_tva(1_003, 1900, "plus_proche") == 191  # 190,57
    assert calculer_tva(1_003, 1900, "plancher") == 190
    assert calculer_tva(0, 1900, "plus_proche") == 0
    assert calculer_tva(1_000, 0, "plus_proche") == 0


@pytest.mark.parametrize(
    "decoupage", [[3570], [1, 3569], [2000, 1570], [1, 1, 1, 3567], [999] * 3 + [573]]
)
def test_les_tva_des_versements_partiels_somment_exactement_a_la_tva_de_l_echeance(
    decoupage: list[int],
) -> None:
    interets, tva = 3_000, 570
    cumul, tva_cumulee, interets_cumules = 0, 0, 0
    for versement in decoupage:
        avant = repartir_bloc_interets(cumul, interets, tva, "plus_proche")
        cumul += versement
        apres = repartir_bloc_interets(cumul, interets, tva, "plus_proche")
        assert apres[0] >= avant[0] and apres[1] >= avant[1]  # jamais de part négative
        interets_cumules += apres[0] - avant[0]
        tva_cumulee += apres[1] - avant[1]
    assert (interets_cumules, tva_cumulee) == (interets, tva)


# --- Paramètre produit + validation ----------------------------------------------------------


def test_validation_refusee_si_taux_tva_positif_sans_compte_tva(db: Session) -> None:
    produit = _produit(db, avec_compte_tva=False)

    with pytest.raises(ComptesNonRattachesError, match="TVA"):
        valider_produit(db, produit, par=None)
    assert produit.is_provisional is True


def test_validation_acceptee_avec_compte_tva_ou_si_exonere(db: Session) -> None:
    avec_compte = _produit(db)
    exonere = _produit(db, taux_tva_bp=0, avec_compte_tva=False)

    valider_produit(db, avec_compte, par=None)
    valider_produit(db, exonere, par=None)

    assert avec_compte.is_provisional is False and exonere.is_provisional is False


def test_un_produit_existant_prend_zero_et_un_nouveau_19_pour_cent_par_defaut(
    client: TestClient, db: Session
) -> None:
    ancien = Product(code=f"ANC{uuid.uuid4().hex[:5]}", name="Produit sans TVA")
    db.add(ancien)
    db.flush()
    assert ancien.taux_tva_bp == 0  # défaut base : exonéré, ne casse rien

    agence = _agence(db, "TVA0")
    admin = _entete(db, agence, "ADMIN_FONCTIONNEL")
    reponse = client.post(
        "/credit/produits",
        json={"code": f"N{uuid.uuid4().hex[:6]}", "name": "Nouveau"},
        headers=admin,
    )
    assert reponse.status_code in (200, 201), reponse.text
    assert reponse.json()["taux_tva_bp"] == 1900

    exonere = client.post(
        "/credit/produits",
        json={"code": f"E{uuid.uuid4().hex[:6]}", "name": "Exonéré", "taux_tva_bp": 0},
        headers=admin,
    )
    assert exonere.json()["taux_tva_bp"] == 0
    hors_bornes = client.post(
        "/credit/produits",
        json={"code": f"H{uuid.uuid4().hex[:6]}", "name": "Hors bornes", "taux_tva_bp": 10001},
        headers=admin,
    )
    assert hors_bornes.status_code == 422


# --- Décaissement + échéancier persisté + aperçu ---------------------------------------------


def test_decaissement_persiste_la_tva_par_echeance_et_le_total_ttc(db: Session) -> None:
    agence = _agence(db, "TVA1")
    demande = _credit_decaisse(db, agence, _produit(db))

    echeances = _installments(db, demande)

    assert [e.tva for e in echeances] == [570, 380, 190]
    assert [e.total for e in echeances] == [103_570, 102_380, 101_190]
    assert sum(e.capital for e in echeances) == 300_000
    assert all(e.total == e.capital + e.interets + e.tva for e in echeances)


def test_decaissement_refuse_si_taux_tva_positif_sans_compte_tva(db: Session) -> None:
    agence = _agence(db, "TVA2")
    produit = _produit(db, avec_compte_tva=False)
    demande = creer_demande(
        db,
        tier_id=_tier(db, agence),
        agency_id=agence.id,
        product_id=produit.id,
        montant_demande=300_000,
        duree_echeances=3,
        objet="Test",
        par=None,
    )
    decider(db, demande, decision="approuve", montant_decide=300_000, motif="OK", par=None)
    par = _ouvrir_session_caisse(db, agence)

    with pytest.raises(RattachementManquantError, match="TVA"):
        decaisser(db, demande, par=par, entry_date=date(2026, 1, 5))


def test_l_apercu_renvoie_la_tva_par_ligne_et_les_memes_montants_que_le_decaissement(
    client: TestClient, db: Session
) -> None:
    agence = _agence(db, "TVA3")
    produit = _produit(db)
    demande = creer_demande(
        db,
        tier_id=_tier(db, agence),
        agency_id=agence.id,
        product_id=produit.id,
        montant_demande=300_000,
        duree_echeances=3,
        objet="Aperçu",
        par=None,
    )
    db.commit()
    responsable = _entete(db, agence, "RESPONSABLE_AGENCE")

    # En instruction (simulation avant décision) : TVA incluse, rien d'écrit.
    reponse = client.get(f"/credit/demandes/{demande.id}/echeancier-apercu", headers=responsable)
    assert reponse.status_code == 200, reponse.text
    lignes = reponse.json()
    assert [ligne["tva"] for ligne in lignes] == [570, 380, 190]
    assert [ligne["total"] for ligne in lignes] == [103_570, 102_380, 101_190]
    assert _installments(db, demande) == []

    decider(db, demande, decision="approuve", montant_decide=300_000, motif="OK", par=None)
    db.commit()
    apres = generer_apercu(db, demande)
    assert [(e.capital, e.interets, e.tva, e.total) for e in apres] == [
        (ligne["capital"], ligne["interets"], ligne["tva"], ligne["total"]) for ligne in lignes
    ]


# --- Remboursement ---------------------------------------------------------------------------


def test_remboursement_plein_ventile_capital_interets_et_tva(db: Session) -> None:
    agence = _agence(db, "TVA4")
    produit = _produit(db)
    demande = _credit_decaisse(db, agence, produit)
    par = _ouvrir_session_caisse(db, agence)
    caisse, interets, tva = (
        agence.compte_caisse_id,
        produit.compte_produits_interets_id,
        produit.compte_tva_id,
    )
    assert caisse is not None and interets is not None and tva is not None
    credit = demande.compte_credit_id
    assert credit is not None
    avant = [_solde_compte(db, c) for c in (caisse, credit, interets, tva)]

    resultat = rembourser(db, demande, montant=103_570, par=par)

    assert (resultat.montant_capital, resultat.montant_interets, resultat.montant_tva) == (
        100_000,
        3_000,
        570,
    )
    assert resultat.echeance_soldee is True
    apres = [_solde_compte(db, c) for c in (caisse, credit, interets, tva)]
    # D caisse 103 570 / C crédit 100 000 / C 7021 3 000 / C 332100 570
    assert [a - b for a, b in zip(apres, avant, strict=True)] == [103_570, -100_000, -3_000, -570]
    registre = db.execute(
        select(Repayment).where(Repayment.application_id == demande.id)
    ).scalar_one()
    assert (registre.montant_capital, registre.montant_interets, registre.montant_tva) == (
        100_000,
        3_000,
        570,
    )
    assert registre.montant_total == 103_570


def test_remboursement_partiel_la_tva_suit_les_interets_et_somme_a_la_tva_de_l_echeance(
    db: Session,
) -> None:
    agence = _agence(db, "TVA5")
    produit = _produit(db)
    demande = _credit_decaisse(db, agence, produit)
    par = _ouvrir_session_caisse(db, agence)
    tva_id = produit.compte_tva_id
    assert tva_id is not None
    avant_tva = _solde_compte(db, tva_id)

    # Bloc « intérêts + TVA » = 3 570 F, réglé avant le capital. 2 000 F ne couvrent que le bloc.
    premier = rembourser(db, demande, montant=2_000, par=par)
    assert (premier.montant_capital, premier.montant_interets, premier.montant_tva) == (
        0,
        1_681,
        319,
    )
    assert premier.echeance_soldee is False

    # Reste du bloc (1 570 F) + 50 000 F de capital : la dernière fraction absorbe l'arrondi.
    second = rembourser(db, demande, montant=1_570 + 50_000, par=par)
    assert (second.montant_interets, second.montant_tva, second.montant_capital) == (
        1_319,
        251,
        50_000,
    )
    assert premier.montant_interets + second.montant_interets == 3_000
    assert premier.montant_tva + second.montant_tva == 570
    assert encours_actuel(db, demande.id) == 300_000 - 50_000

    dernier = rembourser(db, demande, montant=50_000, par=par)
    assert (dernier.montant_capital, dernier.montant_tva) == (50_000, 0)
    assert dernier.echeance_soldee is True

    # Tout ce qui a été collecté sur l'échéance : 570 F de TVA au compte d'État.
    assert _solde_compte(db, tva_id) - avant_tva == -570
    total_tva = db.execute(
        text("SELECT SUM(montant_tva) FROM credit.repayments WHERE application_id = :a"),
        {"a": demande.id},
    ).scalar_one()
    assert total_tva == 570


def test_remboursement_refuse_si_tva_a_collecter_sans_compte_tva(db: Session) -> None:
    agence = _agence(db, "TVA6")
    produit = _produit(db)
    demande = _credit_decaisse(db, agence, produit)
    par = _ouvrir_session_caisse(db, agence)
    produit.compte_tva_id = None  # rattachement retiré après coup
    db.flush()

    with pytest.raises(RattachementManquantError, match="TVA"):
        rembourser(db, demande, montant=103_570, par=par)


def test_produit_exonere_aucune_tva_et_aucune_ligne_332100(db: Session) -> None:
    agence = _agence(db, "TVA7")
    produit = _produit(db, taux_tva_bp=0, avec_compte_tva=False)
    demande = _credit_decaisse(db, agence, produit)
    par = _ouvrir_session_caisse(db, agence)
    tva_id = _cid(db, "332100")
    avant = _solde_compte(db, tva_id)

    assert [e.tva for e in _installments(db, demande)] == [0, 0, 0]
    resultat = rembourser(db, demande, montant=103_000, par=par)

    assert (resultat.montant_capital, resultat.montant_interets, resultat.montant_tva) == (
        100_000,
        3_000,
        0,
    )
    assert _solde_compte(db, tva_id) == avant
    lignes_tva = db.execute(
        text(
            "SELECT count(*) FROM comptabilite.journal_lines jl "
            "WHERE jl.entry_id = :e AND jl.account_id = :a"
        ),
        {"e": resultat.entry_id, "a": tva_id},
    ).scalar_one()
    assert lignes_tva == 0


# --- Solde anticipé --------------------------------------------------------------------------


def test_solde_anticipe_tva_sur_les_interets_courus_jamais_sur_le_capital(db: Session) -> None:
    agence = _agence(db, "TVA8")
    produit = _produit(db)
    demande = _credit_decaisse(db, agence, produit, entry_date=date(2026, 1, 5))
    par = _ouvrir_session_caisse(db, agence)
    caisse, interets, tva = (
        agence.compte_caisse_id,
        produit.compte_produits_interets_id,
        produit.compte_tva_id,
    )
    assert caisse is not None and interets is not None and tva is not None
    credit = demande.compte_credit_id
    assert credit is not None
    avant = [_solde_compte(db, c) for c in (caisse, credit, interets, tva)]

    resultat = solder_par_anticipation(db, demande, par=par, entry_date=date(2026, 1, 20))

    # 15 jours à 12 % sur 300 000 F = 1 500 F d'intérêts ; TVA 19 % = 285 F ; capital intact.
    assert resultat.capital_regle == 300_000
    assert resultat.interets_courus == 1_500
    assert resultat.tva_courue == 285
    assert resultat.montant_total == 301_785
    apres = [_solde_compte(db, c) for c in (caisse, credit, interets, tva)]
    assert [a - b for a, b in zip(apres, avant, strict=True)] == [301_785, -300_000, -1_500, -285]


def test_solde_anticipe_exonere_inchange(db: Session) -> None:
    agence = _agence(db, "TVA9")
    produit = _produit(db, taux_tva_bp=0, avec_compte_tva=False)
    demande = _credit_decaisse(db, agence, produit, entry_date=date(2026, 1, 5))
    par = _ouvrir_session_caisse(db, agence)

    resultat = solder_par_anticipation(db, demande, par=par, entry_date=date(2026, 1, 20))

    assert (resultat.interets_courus, resultat.tva_courue, resultat.montant_total) == (
        1_500,
        0,
        301_500,
    )
