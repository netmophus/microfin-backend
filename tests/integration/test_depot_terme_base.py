"""Dépôt à terme, lot 1 : calculs purs, paramètres produit, et dernier rempart en base (triggers).

Les triggers sont éprouvés en SQL direct, sans passer par le service : c'est le but d'un dernier
rempart — il tient même quand la règle métier est contournée.
"""

import uuid
from collections.abc import Generator
from datetime import date

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.core.database import engine
from app.modules.epargne.gestion_produits import (
    TypeProduitFigeError,
    creer_produit,
    modifier_produit,
)
from app.modules.epargne.models import Product, SavingsAccount
from app.modules.epargne.operations import TYPE_DEPOT, poser_ecriture_operation
from app.modules.epargne.schemas import CreationProduitEpargne
from app.modules.epargne.terme import (
    ajouter_mois,
    interets_du_terme,
    jours_courus,
    penalite_sur_interets,
)
from app.modules.parameters.models import Agency

# --- Calculs purs --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("depart", "mois", "attendu"),
    [
        (date(2026, 1, 15), 3, date(2026, 4, 15)),
        (date(2026, 1, 31), 1, date(2026, 2, 28)),
        (date(2024, 1, 31), 1, date(2024, 2, 29)),  # bissextile
        (date(2026, 8, 31), 6, date(2027, 2, 28)),
        (date(2026, 10, 14), 12, date(2027, 10, 14)),
        (date(2026, 12, 31), 24, date(2028, 12, 31)),
        (date(2024, 2, 29), 12, date(2025, 2, 28)),
    ],
)
def test_ajouter_mois_borne_en_fin_de_mois(depart: date, mois: int, attendu: date) -> None:
    assert ajouter_mois(depart, mois) == attendu


def test_les_interets_s_arretent_a_l_echeance() -> None:
    debut, echeance = date(2026, 1, 1), date(2026, 7, 1)
    assert jours_courus(debut, date(2026, 4, 1), echeance) == 90
    assert jours_courus(debut, echeance, echeance) == 181
    # Dénouement tardif : rien ne court après l'échéance.
    assert jours_courus(debut, date(2026, 7, 9), echeance) == 181
    # Le jour même du placement : zéro jour, zéro intérêt.
    assert jours_courus(debut, debut, echeance) == 0


def test_interets_du_terme_decimal_arrondi_au_franc() -> None:
    # 1 000 000 F à 5 % sur 181 jours, base 365 : 24 794,52 -> 24 795 (plus proche).
    assert interets_du_terme(1_000_000, 500, 181, 365, "plus_proche") == 24_795
    assert interets_du_terme(1_000_000, 500, 181, 365, "plancher") == 24_794
    assert interets_du_terme(1_000_000, 500, 0, 365, "plus_proche") == 0


@pytest.mark.parametrize(
    ("courus", "penalite_bp", "attendu"),
    [(18_000, 5000, 9_000), (18_000, 0, 0), (18_000, 10_000, 18_000), (7, 3333, 2), (0, 5000, 0)],
)
def test_penalite_sur_les_interets_courus_jamais_sur_le_capital(
    courus: int, penalite_bp: int, attendu: int
) -> None:
    penalite = penalite_sur_interets(courus, penalite_bp)
    assert penalite == attendu
    assert penalite <= courus  # le capital n'est jamais entamé


# --- Paramètres produit --------------------------------------------------------------


def test_le_schema_produit_refuse_des_durees_incoherentes() -> None:
    base = {"code": "DATX", "name": "DAT", "type": "terme"}
    CreationProduitEpargne(**base, duree_min_mois=3, duree_max_mois=24, penalite_anticipee_bp=5000)
    with pytest.raises(ValidationError, match="ensemble"):
        CreationProduitEpargne(**base, duree_min_mois=3)
    with pytest.raises(ValidationError, match="ensemble"):
        CreationProduitEpargne(**base, duree_min_mois=24, duree_max_mois=3)
    with pytest.raises(ValidationError, match="dépôts à terme"):
        CreationProduitEpargne(
            code="AV", name="À vue", type="a_vue", duree_min_mois=3, duree_max_mois=6
        )
    with pytest.raises(ValidationError, match="dépôts à terme"):
        CreationProduitEpargne(code="AV", name="À vue", type="a_vue", penalite_anticipee_bp=100)


# --- Base : fixtures -----------------------------------------------------------------


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


def _cid(db: Session, numero: str) -> uuid.UUID:
    return db.execute(
        text("SELECT id FROM comptabilite.accounts WHERE account_number = :n"), {"n": numero}
    ).scalar_one()


def _produit(db: Session, type_produit: str) -> Product:
    produit = Product(
        code=f"T{uuid.uuid4().hex[:8]}", name="Produit", type=type_produit,
        compte_epargne_id=_cid(db, "252111" if type_produit == "terme" else "251111"),
        compte_charge_interet_id=_cid(db, "60252" if type_produit == "terme" else "602511"),
        taux_bp=500, base_jours=365, methode_calcul_solde="fin_periode",
    )
    db.add(produit)
    db.flush()
    return produit


def _tier_et_agence(db: Session) -> tuple[uuid.UUID, uuid.UUID]:
    agence = Agency(
        code=f"DT-{uuid.uuid4().hex[:6]}", name="Agence", compte_caisse_id=_cid(db, "101111")
    )
    db.add(agence)
    db.flush()
    tier_id = db.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES (:n, 'individual', :a, 'actif') RETURNING id"
        ),
        {"n": f"M-DT-{uuid.uuid4().hex[:8]}", "a": agence.id},
    ).scalar_one()
    return tier_id, agence.id


CONDITIONS = {
    "term_months": 6,
    "term_start_date": date(2026, 1, 1),
    "maturity_date": date(2026, 7, 1),
    "principal": 1_000_000,
    "taux_bp_contrat": 500,
    "penalite_bp_contrat": 5000,
    "base_jours_contrat": 365,
    "regle_arrondi_contrat": "plus_proche",
}


def _compte(db: Session, produit: Product, **conditions: object) -> SavingsAccount:
    tier_id, agence_id = _tier_et_agence(db)
    compte = SavingsAccount(
        account_number=f"DT{uuid.uuid4().hex[:10]}", product_id=produit.id, tier_id=tier_id,
        agency_id=agence_id, status="actif", balance=0, **conditions,
    )
    db.add(compte)
    db.flush()
    return compte


def _mouvement(
    db: Session, compte: SavingsAccount, sens: str, type_operation: str, montant: int,
    apres: int, valeur: date,
) -> None:
    piece = poser_ecriture_operation(
        db, compte, TYPE_DEPOT, 1_000, None, entry_date=valeur
    ).id
    db.execute(
        text(
            "INSERT INTO epargne.movements (account_id, sens, amount, balance_after, "
            "operation_type, journal_entry_id, date_valeur) "
            "VALUES (:a, :s, :m, :b, :t, :p, :d)"
        ),
        {"a": compte.id, "s": sens, "m": montant, "b": apres, "t": type_operation,
         "p": piece, "d": valeur},
    )


def _refuse(db: Session, fragment: str) -> "pytest.RaisesExc[DBAPIError]":
    return pytest.raises((DBAPIError, IntegrityError), match=fragment)


# --- Base : conditions du compte -----------------------------------------------------


@pytest.mark.integration
def test_un_compte_terme_sans_conditions_est_refuse_par_la_base(db: Session) -> None:
    produit = _produit(db, "terme")
    with _refuse(db, "conditions du depot"):
        _compte(db, produit)


@pytest.mark.integration
def test_des_conditions_de_terme_sur_un_compte_a_vue_sont_refusees(db: Session) -> None:
    produit = _produit(db, "a_vue")
    with _refuse(db, "conditions du depot"):
        _compte(db, produit, **CONDITIONS)


@pytest.mark.integration
def test_conditions_partielles_refusees_par_le_check(db: Session) -> None:
    produit = _produit(db, "terme")
    with _refuse(db, "ck_accounts_conditions_terme"):
        _compte(db, produit, term_months=6)


@pytest.mark.integration
def test_les_conditions_d_un_compte_terme_sont_immuables(db: Session) -> None:
    compte = _compte(db, _produit(db, "terme"), **CONDITIONS)
    with _refuse(db, "immuables"):
        db.execute(
            text("UPDATE epargne.accounts SET maturity_date = :d WHERE id = :i"),
            {"d": date(2026, 2, 1), "i": compte.id},
        )


@pytest.mark.integration
def test_le_type_d_un_produit_est_fige_des_qu_il_porte_des_comptes(db: Session) -> None:
    produit = _produit(db, "a_vue")
    _compte(db, produit)
    with pytest.raises(TypeProduitFigeError, match="type ne peut plus"):
        modifier_produit(
            db, produit, name="X", type="terme", taux_bp=0, periodicite="annuelle",
            methode_calcul_solde="fin_periode", base_jours=360, regle_arrondi="plus_proche",
            solde_minimum_remunere=0, motif="essai", par=None,
        )
    with _refuse(db, "type ne change plus"):
        db.execute(
            text("UPDATE epargne.products SET type = 'terme' WHERE id = :i"), {"i": produit.id}
        )


@pytest.mark.integration
def test_un_produit_terme_se_cree_avec_ses_bornes_et_sa_penalite(db: Session) -> None:
    produit = creer_produit(
        db, code=f"D{uuid.uuid4().hex[:8]}", name="DAT", type="terme", currency="XOF",
        taux_bp=500, periodicite="annuelle", methode_calcul_solde="fin_periode",
        base_jours=365, regle_arrondi="plus_proche", solde_minimum_remunere=0,
        duree_min_mois=3, duree_max_mois=24, penalite_anticipee_bp=5000, par=None,
    )
    db.refresh(produit)
    assert (produit.duree_min_mois, produit.duree_max_mois) == (3, 24)
    assert produit.penalite_anticipee_bp == 5000


@pytest.mark.integration
def test_la_base_refuse_des_durees_incoherentes(db: Session) -> None:
    produit = _produit(db, "terme")
    with _refuse(db, "ck_products_duree_terme"):
        db.execute(
            text("UPDATE epargne.products SET duree_min_mois = 24, duree_max_mois = 3 "
                 "WHERE id = :i"),
            {"i": produit.id},
        )


# --- Base : mouvements d'un compte à terme -------------------------------------------


@pytest.mark.integration
def test_le_depot_du_capital_est_le_seul_credit_et_une_seule_fois(db: Session) -> None:
    compte = _compte(db, _produit(db, "terme"), **CONDITIONS)
    _mouvement(db, compte, "credit", "depot", 1_000_000, 1_000_000, date(2026, 1, 1))
    with _refuse(db, "capital se place une seule fois"):
        _mouvement(db, compte, "credit", "depot", 5_000, 1_005_000, date(2026, 1, 2))


@pytest.mark.integration
def test_un_credit_d_un_autre_type_est_refuse(db: Session) -> None:
    compte = _compte(db, _produit(db, "terme"), **CONDITIONS)
    with _refuse(db, "bloque jusqu"):
        _mouvement(db, compte, "credit", "decaissement", 5_000, 5_000, date(2026, 1, 1))


@pytest.mark.integration
def test_un_retrait_ordinaire_est_refuse_avant_et_apres_l_echeance(db: Session) -> None:
    compte = _compte(db, _produit(db, "terme"), **CONDITIONS)
    _mouvement(db, compte, "credit", "depot", 1_000_000, 1_000_000, date(2026, 1, 1))
    for jour in (date(2026, 3, 1), date(2026, 8, 1)):
        with _refuse(db, "bloque jusqu"), db.begin_nested():
            _mouvement(db, compte, "debit", "retrait", 1_000, 999_000, jour)


@pytest.mark.integration
def test_le_denouement_exige_l_echeance_et_la_totalite(db: Session) -> None:
    compte = _compte(db, _produit(db, "terme"), **CONDITIONS)
    _mouvement(db, compte, "credit", "depot", 1_000_000, 1_000_000, date(2026, 1, 1))
    with _refuse(db, "avant l'echeance"), db.begin_nested():
        _mouvement(db, compte, "debit", "denouement", 1_000_000, 0, date(2026, 6, 30))
    with _refuse(db, "partiel"), db.begin_nested():
        _mouvement(db, compte, "debit", "denouement", 500_000, 500_000, date(2026, 7, 1))
    _mouvement(db, compte, "debit", "denouement", 1_000_000, 0, date(2026, 7, 1))  # accepté


@pytest.mark.integration
def test_le_retrait_anticipe_n_est_possible_qu_avant_l_echeance(db: Session) -> None:
    compte = _compte(db, _produit(db, "terme"), **CONDITIONS)
    _mouvement(db, compte, "credit", "depot", 1_000_000, 1_000_000, date(2026, 1, 1))
    with _refuse(db, "retrait anticipe impossible"), db.begin_nested():
        _mouvement(db, compte, "debit", "retrait_anticipe", 1_000_000, 0, date(2026, 7, 1))
    _mouvement(db, compte, "debit", "retrait_anticipe", 1_000_000, 0, date(2026, 6, 30))


@pytest.mark.integration
def test_un_seul_versement_d_interets_sur_un_compte_terme(db: Session) -> None:
    compte = _compte(db, _produit(db, "terme"), **CONDITIONS)
    _mouvement(db, compte, "credit", "depot", 1_000_000, 1_000_000, date(2026, 1, 1))
    _mouvement(db, compte, "credit", "interet", 24_795, 1_024_795, date(2026, 7, 1))
    with _refuse(db, "un seul versement"):
        _mouvement(db, compte, "credit", "interet", 100, 1_024_895, date(2026, 7, 1))


@pytest.mark.integration
def test_un_compte_a_vue_n_est_pas_concerne_par_le_blocage(db: Session) -> None:
    compte = _compte(db, _produit(db, "a_vue"))
    _mouvement(db, compte, "credit", "depot", 10_000, 10_000, date(2026, 1, 1))
    _mouvement(db, compte, "debit", "retrait", 4_000, 6_000, date(2026, 1, 2))
