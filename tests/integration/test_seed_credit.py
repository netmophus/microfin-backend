"""Seed du produit de crédit de démonstration — DONNÉE provisoire, taux non nul à dessein
(voir app/cli/seed_credit.py). Idempotent, comme les autres seeds de données."""

from collections.abc import Generator

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.cli.seed_credit import PRODUITS, executer_seed_produits_credit
from app.core.database import engine

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


def test_seed_installe_le_produit_demo_provisoire_avec_taux_non_nul(db: Session) -> None:
    executer_seed_produits_credit(db)

    ligne = db.execute(
        text(
            "SELECT p.name, p.is_provisional, p.taux_bp, p.periodicite, p.methode_amortissement, "
            "       am.account_number, ac.account_number, ai.account_number "
            "FROM credit.products p "
            "JOIN comptabilite.accounts am ON am.id = p.compte_credit_membre_id "
            "JOIN comptabilite.accounts ac ON ac.id = p.compte_credit_client_id "
            "JOIN comptabilite.accounts ai ON ai.id = p.compte_produits_interets_id "
            "WHERE p.code = 'CCT'"
        )
    ).one()

    _name, is_provisional, taux_bp, periodicite, methode, membre, client, interets = ligne
    assert is_provisional is True
    assert taux_bp > 0  # démonstration : un vrai échéancier doit pouvoir se calculer
    assert membre == "202211"
    assert client == "202221"
    assert interets == "7021"
    assert periodicite == "mensuelle"
    assert methode == "echeance_constante"


def test_seed_est_idempotent(db: Session) -> None:
    premier = executer_seed_produits_credit(db)
    second = executer_seed_produits_credit(db)

    assert premier == second == len(PRODUITS)

    nb = db.execute(
        text("SELECT count(*) FROM credit.products WHERE code = 'CCT'")
    ).scalar_one()
    assert nb == 1
