"""Moteur de calcul des ratios prudentiels RCSFD (lot P2.1.b) — preuve de la convention de
signe, des agrégats BALANCE/SPECIAL, de l'évaluation d'un ratio et de la sélection du seuil.

Pas de seed réel ici (lot P2.1.c) : chaque test monte ses propres agrégats/ratios/seuils, sur
des comptes SYNTHÉTIQUES (jamais les comptes réels du plan RCSFD partagés par la base de dev —
même discipline que test_comptabilite_etats_financiers_api.py). Numéros synthétiques au format
« {classe}T{suffixe} » (le chiffre de classe en premier caractère, comme les comptes réels) —
`account_class` est dérivé de ce premier chiffre.

PREUVE CENTRALE (voir le docstring de app/modules/conformite/moteur.py) : `rapports.balance`
normalise déjà le signe par `normal_side` — un compte créditeur à solde normal ressort
POSITIF. `agregat_compte.sens` n'est PAS une correction de polarité, c'est l'appartenance
(+1 ajoute, -1 déduit) à la définition réglementaire de l'agrégat.
"""

import uuid
from collections.abc import Generator
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine
from app.modules.caisse.models import CaisseSession, Poste, PosteAssignation
from app.modules.caisse.service import ouvrir_session
from app.modules.comptabilite import ecritures, journee
from app.modules.comptabilite.ecritures import LigneSaisie
from app.modules.comptabilite.models import Account, Journal
from app.modules.comptabilite.plan import importer
from app.modules.conformite.models import (
    AgregatCompte,
    AgregatPrudentiel,
    ParametreInstitution,
    RatioPrudentiel,
    RatioSeuil,
)
from app.modules.conformite.moteur import (
    STATUT_CONFORME,
    STATUT_NON_CALCULABLE,
    STATUT_NON_CONFORME,
    agregat_valeur,
    evaluer_ratio,
    evaluer_tous,
)
from app.modules.credit.decaissement import decaisser
from app.modules.credit.demandes import creer_demande, decider
from app.modules.credit.models import Product
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant

pytestmark = pytest.mark.integration

AUJOURDHUI = date(2026, 6, 15)  # dans l'exercice ambiant "2026" du bootstrap de test


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
    """Chantier P1bis lot 2 : seul `test_encours_plus_gros_emprunteur_prend_le_max` en a
    réellement besoin (decaisser() en mode 'caisse' exige une journée ouverte) — ouverte ici
    pour tous, même patron anti-interblocage que les autres fichiers (garde sur
    `request.fixturenames`)."""
    if "db" not in request.fixturenames:
        return
    db = request.getfixturevalue("db")
    journee.ouvrir_journee(db, journee.prochaine_date_ouvree(db), None)


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
        description="Mouvement de test (moteur conformité)",
        lignes=lignes,
        par=None,
    )
    ecritures.valider(db, entry, None)


def _agregat(
    db: Session,
    code: str,
    *,
    type_: str = "BALANCE",
    calcul_special: str | None = None,
    nets_de_provisions: bool = False,
) -> AgregatPrudentiel:
    agregat = AgregatPrudentiel(
        code=code,
        libelle=f"Agrégat test {code}",
        type=type_,
        calcul_special=calcul_special,
        nets_de_provisions=nets_de_provisions,
    )
    db.add(agregat)
    db.flush()
    return agregat


def _composition(db: Session, agregat: AgregatPrudentiel, prefixe: str, sens: int) -> None:
    db.add(AgregatCompte(agregat_id=agregat.id, prefixe_compte=prefixe, sens=sens))
    db.flush()


def _ratio(
    db: Session,
    code: str,
    *,
    numerateur: AgregatPrudentiel,
    denominateur: AgregatPrudentiel,
    operateur: str,
    ordre: int,
) -> RatioPrudentiel:
    ratio = RatioPrudentiel(
        code=code,
        libelle=f"Ratio test {code}",
        agregat_numerateur_id=numerateur.id,
        agregat_denominateur_id=denominateur.id,
        operateur=operateur,
        ordre=ordre,
    )
    db.add(ratio)
    db.flush()
    return ratio


def _seuil(db: Session, ratio: RatioPrudentiel, valeur: int, categorie: str | None = None) -> None:
    db.add(RatioSeuil(ratio_id=ratio.id, categorie_sfd=categorie, valeur_seuil=valeur))
    db.flush()


# --- agregat_valeur : BALANCE, la convention de signe --------------------------------------


def test_compte_crediteur_a_solde_normal_ressort_positif(db: Session) -> None:
    """Un compte créditeur (classe 5, fonds propres) avec un solde créditeur NORMAL ressort
    POSITIF — rapports.balance a déjà résolu la polarité, agregat_compte.sens=+1 se contente
    d'additionner."""
    capital = _compte(db, "5T201", normal_side="C")
    contrepartie = _compte(db, "1T201", normal_side="D")
    _valider_od(
        db,
        [LigneSaisie(contrepartie.id, "D", 1_000), LigneSaisie(capital.id, "C", 1_000)],
        AUJOURDHUI,
    )

    agregat = _agregat(db, "FP_TEST_1")
    _composition(db, agregat, "5T201", sens=1)

    assert agregat_valeur(db, "FP_TEST_1", AUJOURDHUI) == 1_000


def test_sens_moins_un_deduit_de_lagregat(db: Session) -> None:
    """Preuve chiffrée complète : fonds propres = capital créditeur 1000 + réserves créditrices
    500 - immobilisations incorporelles (débitrices, solde normal positif) 200 = 1300."""
    capital = _compte(db, "5T211", normal_side="C")
    reserves = _compte(db, "5T212", normal_side="C")
    immos_incorp = _compte(db, "2T211", normal_side="D")
    contrepartie = _compte(db, "1T211", normal_side="D")

    # Deux pièces équilibrées séparément (apport en capital/réserves, puis acquisition de
    # l'immobilisation sur cet apport) plutôt qu'une seule pièce à 4 lignes mal équilibrée.
    _valider_od(
        db,
        [
            LigneSaisie(contrepartie.id, "D", 1_500),
            LigneSaisie(capital.id, "C", 1_000),
            LigneSaisie(reserves.id, "C", 500),
        ],
        AUJOURDHUI,
    )
    _valider_od(
        db, [LigneSaisie(immos_incorp.id, "D", 200), LigneSaisie(contrepartie.id, "C", 200)],
        AUJOURDHUI,
    )

    fonds_propres = _agregat(db, "FP_TEST_2")
    _composition(db, fonds_propres, "5T211", sens=1)
    _composition(db, fonds_propres, "5T212", sens=1)
    _composition(db, fonds_propres, "2T211", sens=-1)

    assert agregat_valeur(db, "FP_TEST_2", AUJOURDHUI) == 1_300


def test_agregat_mixte_actif_passif_sadditionne_simplement(db: Session) -> None:
    """Un agrégat mixte (une ligne sur un compte débiteur, une sur un compte créditeur, toutes
    deux sens=+1) additionne deux valeurs déjà positives dans leur propre repère — aucune
    correction de polarité supplémentaire."""
    actif = _compte(db, "1T221", normal_side="D")
    passif = _compte(db, "5T221", normal_side="C")

    _valider_od(db, [LigneSaisie(actif.id, "D", 100), LigneSaisie(passif.id, "C", 100)], AUJOURDHUI)
    _valider_od(db, [LigneSaisie(actif.id, "D", 50), LigneSaisie(passif.id, "C", 50)], AUJOURDHUI)

    mixte = _agregat(db, "MIXTE_TEST")
    _composition(db, mixte, "1T221", sens=1)
    _composition(db, mixte, "5T221", sens=1)

    assert agregat_valeur(db, "MIXTE_TEST", AUJOURDHUI) == 300


def test_nets_de_provisions_via_sens_moins_un_ordinaire(db: Session) -> None:
    """`nets_de_provisions=True` est une étiquette documentaire — la déduction réelle passe par
    une ligne agregat_compte à sens=-1 ordinaire sur le compte de provision, EXACT MÊME
    mécanisme que n'importe quelle autre déduction. Risques portés nets = encours brut 5000
    (débiteur) - provision 500 (créditrice, solde normal positif) = 4500. Deux pièces
    équilibrées séparément (décaissement fictif, puis dotation de la provision) plutôt qu'une
    seule pièce à 3 lignes mal équilibrée."""
    encours_brut = _compte(db, "2T231", normal_side="D")
    provision = _compte(db, "2T232", normal_side="C")
    contrepartie = _compte(db, "1T231", normal_side="D")

    _valider_od(
        db,
        [LigneSaisie(encours_brut.id, "D", 5_000), LigneSaisie(contrepartie.id, "C", 5_000)],
        AUJOURDHUI,
    )
    _valider_od(
        db,
        [LigneSaisie(contrepartie.id, "D", 500), LigneSaisie(provision.id, "C", 500)],
        AUJOURDHUI,
    )

    risques_nets = _agregat(db, "RISQUES_TEST", nets_de_provisions=True)
    _composition(db, risques_nets, "2T231", sens=1)
    _composition(db, risques_nets, "2T232", sens=-1)

    assert agregat_valeur(db, "RISQUES_TEST", AUJOURDHUI) == 5_000 - 500


# --- evaluer_ratio : opérateurs, dénominateur nul, sélection du seuil -----------------------


def _poser_agregats_num_denom(
    db: Session, *, valeur_num: int, valeur_denom: int
) -> tuple[AgregatPrudentiel, AgregatPrudentiel]:
    suffixe = uuid.uuid4().hex[:4]
    cp_num = _compte(db, f"5T{suffixe}1", normal_side="C")
    cp_denom = _compte(db, f"1T{suffixe}2", normal_side="D")
    contrepartie = _compte(db, f"1T{suffixe}3", normal_side="D")
    _valider_od(
        db,
        [LigneSaisie(contrepartie.id, "D", valeur_num), LigneSaisie(cp_num.id, "C", valeur_num)],
        AUJOURDHUI,
    )
    if valeur_denom:
        _valider_od(
            db,
            [
                LigneSaisie(cp_denom.id, "D", valeur_denom),
                LigneSaisie(contrepartie.id, "C", valeur_denom),
            ],
            AUJOURDHUI,
        )
    num = _agregat(db, f"NUM_{suffixe}")
    _composition(db, num, cp_num.account_number, sens=1)
    denom = _agregat(db, f"DENOM_{suffixe}")
    _composition(db, denom, cp_denom.account_number, sens=1)
    return num, denom


def test_operateur_ge_conforme_et_non_conforme(db: Session) -> None:
    num, denom = _poser_agregats_num_denom(db, valeur_num=20_000, valeur_denom=100_000)  # 20%
    ratio = _ratio(db, "RATIO_GE_TEST", numerateur=num, denominateur=denom, operateur="GE", ordre=1)
    _seuil(db, ratio, 15)  # 20% >= 15% -> conforme

    resultat = evaluer_ratio(db, "RATIO_GE_TEST", AUJOURDHUI)
    assert resultat.valeur_ratio_pct == 20
    assert resultat.conforme is True
    assert resultat.statut == STATUT_CONFORME
    assert resultat.marge == 5  # 20 - 15


def test_operateur_ge_non_conforme(db: Session) -> None:
    num, denom = _poser_agregats_num_denom(db, valeur_num=10_000, valeur_denom=100_000)  # 10%
    ratio = _ratio(
        db, "RATIO_GE_TEST_2", numerateur=num, denominateur=denom, operateur="GE", ordre=2
    )
    _seuil(db, ratio, 15)  # 10% >= 15% -> FAUX

    resultat = evaluer_ratio(db, "RATIO_GE_TEST_2", AUJOURDHUI)
    assert resultat.conforme is False
    assert resultat.statut == STATUT_NON_CONFORME
    assert resultat.marge == -5


def test_operateur_le_conforme_et_non_conforme(db: Session) -> None:
    num, denom = _poser_agregats_num_denom(db, valeur_num=20_000, valeur_denom=100_000)  # 20%
    ratio = _ratio(db, "RATIO_LE_TEST", numerateur=num, denominateur=denom, operateur="LE", ordre=3)
    _seuil(db, ratio, 25)  # 20% <= 25% -> conforme

    resultat = evaluer_ratio(db, "RATIO_LE_TEST", AUJOURDHUI)
    assert resultat.conforme is True
    assert resultat.statut == STATUT_CONFORME
    assert resultat.marge == 5  # 25 - 20


def test_operateur_le_non_conforme(db: Session) -> None:
    num, denom = _poser_agregats_num_denom(db, valeur_num=30_000, valeur_denom=100_000)  # 30%
    ratio = _ratio(
        db, "RATIO_LE_TEST_2", numerateur=num, denominateur=denom, operateur="LE", ordre=4
    )
    _seuil(db, ratio, 25)  # 30% <= 25% -> FAUX

    resultat = evaluer_ratio(db, "RATIO_LE_TEST_2", AUJOURDHUI)
    assert resultat.conforme is False
    assert resultat.statut == STATUT_NON_CONFORME


def test_denominateur_nul_rend_non_calculable_sans_lever(db: Session) -> None:
    num, denom = _poser_agregats_num_denom(db, valeur_num=20_000, valeur_denom=0)
    ratio = _ratio(
        db, "RATIO_DENOM_NUL", numerateur=num, denominateur=denom, operateur="GE", ordre=5
    )
    _seuil(db, ratio, 15)

    resultat = evaluer_ratio(db, "RATIO_DENOM_NUL", AUJOURDHUI)
    assert resultat.statut == STATUT_NON_CALCULABLE
    assert resultat.conforme is None
    assert resultat.valeur_ratio_pct is None
    assert resultat.marge is None


def test_seuil_absent_rend_aussi_non_calculable(db: Session) -> None:
    """Paramétrage incomplet (aucun ratio_seuil posé, ni spécifique ni universel) : même statut
    que dénominateur nul — jamais de division contre une valeur absente."""
    num, denom = _poser_agregats_num_denom(db, valeur_num=20_000, valeur_denom=100_000)
    _ratio(db, "RATIO_SANS_SEUIL", numerateur=num, denominateur=denom, operateur="GE", ordre=6)

    resultat = evaluer_ratio(db, "RATIO_SANS_SEUIL", AUJOURDHUI)
    assert resultat.statut == STATUT_NON_CALCULABLE
    assert resultat.seuil_applicable is None


def test_seuil_specifique_a_la_categorie_prevaut_sur_le_seuil_universel(db: Session) -> None:
    num, denom = _poser_agregats_num_denom(db, valeur_num=12_000, valeur_denom=100_000)  # 12%
    ratio = _ratio(
        db, "RATIO_CATEGORIE", numerateur=num, denominateur=denom, operateur="GE", ordre=7
    )
    _seuil(db, ratio, 15, categorie=None)  # seuil universel : 12% < 15% -> non conforme
    _seuil(db, ratio, 10, categorie="NON_AFFILIE")  # seuil dédié : 12% >= 10% -> conforme
    db.add(ParametreInstitution(categorie_sfd="NON_AFFILIE"))
    db.flush()

    resultat = evaluer_ratio(db, "RATIO_CATEGORIE", AUJOURDHUI)
    assert resultat.categorie_sfd_appliquee == "NON_AFFILIE"
    assert resultat.seuil_applicable == 10
    assert resultat.conforme is True


def test_categorie_sans_seuil_dedie_retombe_sur_le_seuil_universel(db: Session) -> None:
    num, denom = _poser_agregats_num_denom(db, valeur_num=12_000, valeur_denom=100_000)  # 12%
    ratio = _ratio(
        db, "RATIO_CATEGORIE_2", numerateur=num, denominateur=denom, operateur="GE", ordre=8
    )
    _seuil(db, ratio, 15, categorie=None)  # seul seuil posé : universel
    db.add(ParametreInstitution(categorie_sfd="AFFILIE"))  # aucun seuil dédié à AFFILIE
    db.flush()

    resultat = evaluer_ratio(db, "RATIO_CATEGORIE_2", AUJOURDHUI)
    assert resultat.seuil_applicable == 15
    assert resultat.conforme is False  # 12% < 15%


def test_evaluer_tous_respecte_lordre_et_exclut_les_inactifs(db: Session) -> None:
    num, denom = _poser_agregats_num_denom(db, valeur_num=20_000, valeur_denom=100_000)
    second = _ratio(
        db, "RATIO_ORDRE_2", numerateur=num, denominateur=denom, operateur="GE", ordre=20
    )
    _seuil(db, second, 1)
    premier = _ratio(
        db, "RATIO_ORDRE_1", numerateur=num, denominateur=denom, operateur="GE", ordre=10
    )
    _seuil(db, premier, 1)
    inactif = _ratio(
        db, "RATIO_INACTIF", numerateur=num, denominateur=denom, operateur="GE", ordre=5
    )
    inactif.actif = False
    _seuil(db, inactif, 1)
    db.flush()

    resultats = evaluer_tous(db, AUJOURDHUI)

    codes = [r.code for r in resultats]
    assert codes == ["RATIO_ORDRE_1", "RATIO_ORDRE_2"]
    assert "RATIO_INACTIF" not in codes


# --- SPECIAL : dispatch nommé, pas câblé en dur -------------------------------------------


def _agence(db: Session, code: str) -> Agency:
    agence = Agency(
        code=code,
        name=f"Agence {code}",
        compte_caisse_id=db.execute(
            text("SELECT id FROM comptabilite.accounts WHERE account_number = '101111'")
        ).scalar_one(),
    )
    db.add(agence)
    db.flush()
    return agence


def _tier(db: Session, agence: Agency) -> uuid.UUID:
    tier_id = db.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES (:n, 'individual', :a, 'actif') RETURNING id"
        ),
        {"n": f"M-CNF-{uuid.uuid4().hex[:6]}", "a": agence.id},
    ).scalar_one()
    nat = db.execute(text("SELECT id FROM parameters.countries LIMIT 1")).scalar_one()
    db.execute(
        text(
            "INSERT INTO tiers.individual_profiles "
            "(tier_id, last_name, first_name, birth_date, gender, nationality_id) "
            "VALUES (:t, 'Diallo', 'Issa', '1980-01-01', 'M', :nat)"
        ),
        {"t": tier_id, "nat": nat},
    )
    return tier_id


def _cid(db: Session, numero: str) -> uuid.UUID:
    return db.execute(
        text("SELECT id FROM comptabilite.accounts WHERE account_number = :n"), {"n": numero}
    ).scalar_one()


def _produit(db: Session) -> Product:
    produit = Product(
        code=f"CNF{uuid.uuid4().hex[:5]}",
        name="Crédit test conformité",
        compte_credit_membre_id=_cid(db, "202211"),
        compte_credit_client_id=_cid(db, "202221"),
        taux_bp=0,
    )
    db.add(produit)
    db.flush()
    return produit


def _decaisser_pour(
    db: Session, agence: Agency, tier_id: uuid.UUID, produit: Product, montant: int
) -> None:
    demande = creer_demande(
        db,
        tier_id=tier_id,
        agency_id=agence.id,
        product_id=produit.id,
        montant_demande=montant,
        duree_echeances=6,
        objet="Test conformité",
        par=None,
    )
    decider(db, demande, decision="approuve", montant_decide=montant, motif="OK", par=None)
    # security.users LIMIT 1 renvoie TOUJOURS le même uid : poste et session sont donc
    # réutilisés d'un appel à l'autre (deux décaissements dans le même test), jamais recréés —
    # sinon uq_caisse_postes_agency_code / "une session par caissier" refuseraient le second.
    uid = db.execute(text("SELECT id FROM security.users LIMIT 1")).scalar_one()
    poste_id = db.execute(
        select(Poste.id).where(Poste.agency_id == agence.id, Poste.code == "01")
    ).scalar_one_or_none()
    if poste_id is None:
        poste = Poste(
            agency_id=agence.id,
            code="01",
            libelle="Caisse test",
            compte_caisse_id=agence.compte_caisse_id,
        )
        db.add(poste)
        db.flush()
        poste_id = poste.id
        db.add(PosteAssignation(poste_id=poste_id, user_id=uid))
        db.flush()

    session_ouverte = db.execute(
        select(CaisseSession.id).where(
            CaisseSession.caissier_id == uid, CaisseSession.status == "ouverte"
        )
    ).first()
    if session_ouverte is not None:
        decaisser(db, demande, par=uid)
        return
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
        poste_id=poste_id,
        fonds_initial=0,
    )
    decaisser(db, demande, par=uid)


def test_encours_plus_gros_emprunteur_prend_le_max(db: Session) -> None:
    """SPECIAL : group-by tier_id sur l'encours crédit, le MAXIMUM — pas la somme. Deux
    emprunteurs, 300 000 et 700 000 décaissés : l'agrégat doit rendre 700 000, jamais 1 000 000."""
    agence = _agence(db, "CNF1")
    petit_emprunteur = _tier(db, agence)
    gros_emprunteur = _tier(db, agence)
    produit = _produit(db)

    _decaisser_pour(db, agence, petit_emprunteur, produit, 300_000)
    _decaisser_pour(db, agence, gros_emprunteur, produit, 700_000)

    agregat = _agregat(
        db, "PLUS_GROS_EMPRUNTEUR_TEST", type_="SPECIAL", calcul_special="PLUS_GROS_EMPRUNTEUR"
    )

    assert agregat_valeur(db, agregat.code, AUJOURDHUI) == 700_000


# --- P2.0-b1 : un agrégat sur le préfixe d'un brut ne ramasse pas ses contras ------------------

CSV_PLAN_B1 = (
    Path(__file__).resolve().parents[2] / "docs" / "reference" / "plan_comptable_import.csv"
)


@pytest.mark.parametrize(
    ("brut", "contra", "prefixe_brut", "prefixe_piege"),
    [
        ("412100", "412910", "4121", "412"),
        ("441100", "4418", "4411", "441"),
        ("442100", "4429", "4421", "442"),
    ],
)
def test_prefixe_du_brut_ne_ramasse_pas_la_provision_ou_l_amortissement(
    db: Session, brut: str, contra: str, prefixe_brut: str, prefixe_piege: str
) -> None:
    importer(db, str(CSV_PLAN_B1))
    compte_brut = db.execute(select(Account).where(Account.account_number == brut)).scalar_one()
    compte_contra = db.execute(
        select(Account).where(Account.account_number == contra)
    ).scalar_one()
    capital = _compte(db, f"5T{uuid.uuid4().hex[:5]}", normal_side="C")
    # Brut 8 000 (D) financé par un capital ; contra 3 000 (C) en contrepartie du capital.
    _valider_od(
        db, [LigneSaisie(compte_brut.id, "D", 8000), LigneSaisie(capital.id, "C", 8000)], AUJOURDHUI
    )
    _valider_od(
        db,
        [LigneSaisie(capital.id, "D", 3000), LigneSaisie(compte_contra.id, "C", 3000)],
        AUJOURDHUI,
    )
    sur_brut = _agregat(db, f"BRUT_{brut}")
    _composition(db, sur_brut, prefixe_brut, 1)
    sur_regroupement = _agregat(db, f"PIEGE_{brut}")
    _composition(db, sur_regroupement, prefixe_piege, 1)

    # Le préfixe dédié au brut ne voit QUE le brut...
    assert agregat_valeur(db, f"BRUT_{brut}", AUJOURDHUI) == 8000
    # ... alors que le préfixe du regroupement ramasse aussi le contra (solde créditeur normal,
    # donc positif) : 8 000 + 3 000. C'est le piège que les bruts dédiés suppriment.
    assert agregat_valeur(db, f"PIEGE_{brut}", AUJOURDHUI) == 11000
