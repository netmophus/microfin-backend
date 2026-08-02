"""Rattachement comptable des produits d'épargne (migration 0019 + seed).

Ce bloc est du CÂBLAGE de configuration : le produit connaît son compte de dette du plan. Pas
de garde-fou comptable ici — ceux-là (écriture équilibrée, compte de saisie) mordront en E3 sur
les vraies écritures. On vérifie donc ce qu'on peut affirmer à ce stade : le rattachement pointe
sur le bon compte, un compte de SAISIE au sens CRÉDIT (une dette), et le produit reste provisoire.
"""

from collections.abc import Generator

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.cli.seed_epargne import executer_seed_produits
from app.core.database import engine

pytestmark = pytest.mark.integration

# Rattachement provisoire attendu : produit -> compte MEMBRE du plan.
ATTENDU = {"EAV": "251111", "DAT": "252111", "EPR": "253111"}


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


def test_le_seed_rattache_chaque_produit_au_bon_compte(db: Session) -> None:
    executer_seed_produits(db)

    lignes = db.execute(
        text(
            "SELECT p.code, a.account_number, a.is_posting, a.normal_side, p.is_provisional "
            "FROM epargne.products p "
            "JOIN comptabilite.accounts a ON a.id = p.compte_epargne_id "
            "WHERE p.code IN ('EAV', 'DAT', 'EPR')"
        )
    ).all()

    rattachements = {code: numero for code, numero, *_ in lignes}
    assert rattachements == ATTENDU

    for _code, _numero, is_posting, normal_side, is_provisional in lignes:
        # Un compte de dette : saisie (mouvementable), sens crédit. Et le produit reste provisoire.
        assert is_posting is True
        assert normal_side == "C"
        assert is_provisional is True
