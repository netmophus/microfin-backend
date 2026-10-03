"""Seed des ratios prudentiels RCSFD (lot P2.1.c) — preuve chiffrée sur les comptes RÉELS du
plan RCSFD (importés par le bootstrap de test, voir conftest.py) que :
  - la provision (299, via sa feuille 2991) se DÉDUIT vraiment du risque brut (292) — pas
    seulement neutralisée à zéro comme l'aurait fait un préfixe large recouvrant son propre
    contra (`19`/`29` corrigés en `191..194`/`291..294` disjoints, voir
    app/cli/seed_conformite.py) ;
  - un sous-compte « rattaché » à sens opposé (1136, Dettes rattachées créditrices nichées
    sous `11` qui est débiteur) est bien NEUTRALISÉ (contribution nette = 0), pas additionné ;
  - `evaluer_tous` tourne sans planter après seed, sur une base sans aucune activité réelle
    (dénominateurs nuls -> NON_CALCULABLE, jamais une exception).
"""

import uuid
from collections.abc import Generator
from datetime import date

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.cli.seed_conformite import RATIOS, executer_seed_conformite
from app.core.database import engine
from app.modules.comptabilite import ecritures, journee
from app.modules.comptabilite.ecritures import LigneSaisie
from app.modules.comptabilite.models import Account, Journal
from app.modules.conformite.moteur import STATUT_NON_CALCULABLE, agregat_valeur, evaluer_tous

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


@pytest.fixture(autouse=True)
def _journee_ouverte(request: pytest.FixtureRequest) -> None:
    if "db" not in request.fixturenames:
        return
    db = request.getfixturevalue("db")
    journee.ouvrir_journee(db, journee.prochaine_date_ouvree(db), None)


def _id_compte(db: Session, numero: str) -> uuid.UUID:
    return db.execute(select(Account.id).where(Account.account_number == numero)).scalar_one()


def _journal_id(db: Session, code: str) -> uuid.UUID:
    return db.execute(select(Journal.id).where(Journal.code == code)).scalar_one()


def _valider_od(db: Session, lignes: list[LigneSaisie], entry_date: date) -> None:
    entry = ecritures.creer_brouillon(
        db,
        journal_id=_journal_id(db, "OD"),
        entry_date=entry_date,
        description="Mouvement de test (seed conformité)",
        lignes=lignes,
        par=None,
    )
    ecritures.valider(db, entry, None)


def test_provision_199_299_se_deduit_vraiment_pas_seulement_neutralisee(db: Session) -> None:
    """Risque brut (292, classe 29) 10 000, provision (2991, sous 299) 3 000 -> net attendu
    7 000. Si la correction 19/199 et 29/299 n'avait pas été appliquée (préfixe large '29'
    recouvrant '299'), la provision se serait neutralisée à elle-même et le résultat aurait
    été 10 000 (brut seul), pas 7 000."""
    executer_seed_conformite(db)

    caisse = _id_compte(db, "101111")
    souffrance = _id_compte(db, "292")
    provision = _id_compte(db, "2991")

    _valider_od(
        db, [LigneSaisie(souffrance, "D", 10_000), LigneSaisie(caisse, "C", 10_000)], AUJOURDHUI
    )
    _valider_od(
        db, [LigneSaisie(caisse, "D", 3_000), LigneSaisie(provision, "C", 3_000)], AUJOURDHUI
    )

    assert agregat_valeur(db, "RISQUES_PORTES", AUJOURDHUI) == 7_000


def test_rattache_sens_oppose_est_neutralise_pas_additionne(db: Session) -> None:
    """1136 (Dettes rattachées, créditrices) niché sous 11 (débiteur) : poser un mouvement
    dessus ne doit PAS faire bouger RISQUES_PORTES — il matche à la fois '11' (+1) et sa
    propre ligne corrective '1136' (-1), contribution nette = 0."""
    executer_seed_conformite(db)

    caisse = _id_compte(db, "101111")
    dette_rattachee = _id_compte(db, "1136")

    avant = agregat_valeur(db, "RISQUES_PORTES", AUJOURDHUI)

    _valider_od(
        db, [LigneSaisie(caisse, "D", 500), LigneSaisie(dette_rattachee, "C", 500)], AUJOURDHUI
    )

    assert agregat_valeur(db, "RISQUES_PORTES", AUJOURDHUI) == avant


def test_rattache_ressources_est_neutralise(db: Session) -> None:
    """Même preuve côté RESSOURCES : 25117 (Créances rattachées, débitrices) niché sous 25
    (créditeur) ne doit pas faire bouger RESSOURCES."""
    executer_seed_conformite(db)

    caisse = _id_compte(db, "101111")
    creance_rattachee = _id_compte(db, "25117")

    avant = agregat_valeur(db, "RESSOURCES", AUJOURDHUI)

    _valider_od(
        db, [LigneSaisie(creance_rattachee, "D", 300), LigneSaisie(caisse, "C", 300)], AUJOURDHUI
    )

    assert agregat_valeur(db, "RESSOURCES", AUJOURDHUI) == avant


def test_evaluer_tous_apres_seed_ne_plante_pas(db: Session) -> None:
    """Base sans aucune activité réelle (juste le seed) : les 2 ratios actifs doivent
    renvoyer un résultat — NON_CALCULABLE est attendu (dénominateurs nuls), jamais une
    exception, jamais un ratio inactif dans la liste."""
    executer_seed_conformite(db)

    resultats = evaluer_tous(db, AUJOURDHUI)

    assert [r.code for r in resultats] == [
        "RATIO_1_COUVERTURE_RISQUES",
        "RATIO_5_DIVISION_RISQUES",
    ]
    for resultat in resultats:
        assert resultat.statut == STATUT_NON_CALCULABLE
        assert resultat.conforme is None


def test_seed_est_idempotent(db: Session) -> None:
    """Un second passage ne duplique rien (codes UNIQUE)."""
    executer_seed_conformite(db)
    nb_ratios_avant = db.execute(
        text("SELECT count(*) FROM conformite.ratio_prudentiel")
    ).scalar_one()

    executer_seed_conformite(db)
    nb_ratios_apres = db.execute(
        text("SELECT count(*) FROM conformite.ratio_prudentiel")
    ).scalar_one()

    assert nb_ratios_avant == nb_ratios_apres == len(RATIOS)
