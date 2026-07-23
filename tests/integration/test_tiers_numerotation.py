"""Numérotation des tiers (T1b) — le garde-fou principal du bloc.

Le test central est celui du VERROU. On ne l'éprouve pas à coups de threads qui espèrent une
collision — ça clignote. On FABRIQUE l'entrelacement avec deux connexions réelles : la
première alloue un numéro et ne commit pas (elle tient le verrou de ligne) ; la seconde, avec
un statement_timeout court, tente le même (prefix, année) et DOIT bloquer jusqu'au timeout.
Le blocage lui-même est la preuve que le verrou est là. Déterministe : si le verrou manquait,
la seconde rendrait un numéro immédiatement et le test échouerait en NE voyant PAS le blocage.
"""

import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.database import engine
from app.modules.tiers.numbering import (
    prefixe_pour_type,
    prochain_numero,
    prochain_numero_pour_annee,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def db() -> Generator[Session, None, None]:
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(
        bind=connection,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
    )
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def _prefixe_de_test() -> str:
    """Préfixe unique par test (hors des vrais M/P/G), pour une isolation totale."""
    return f"T{uuid.uuid4().hex[:6]}"


def test_numerotation_sequentielle_sans_trou(db: Session) -> None:
    prefixe = _prefixe_de_test()
    numeros = [prochain_numero_pour_annee(db, prefixe, 2999) for _ in range(5)]
    assert numeros == [f"{prefixe}-2999-{i:07d}" for i in range(1, 6)]


def test_format_du_numero(db: Session) -> None:
    prefixe = _prefixe_de_test()
    numero = prochain_numero_pour_annee(db, prefixe, 2026)
    # Compteur zéro-paddé sur 7 chiffres : le premier de l'année est ...-0000001.
    assert numero == f"{prefixe}-2026-0000001"


def test_changement_d_annee_repart_a_un(db: Session) -> None:
    prefixe = _prefixe_de_test()
    assert prochain_numero_pour_annee(db, prefixe, 2026).endswith("-0000001")
    assert prochain_numero_pour_annee(db, prefixe, 2026).endswith("-0000002")
    # Année neuve : aucune ligne (prefix, 2027) encore -> la séquence repart à 1.
    assert prochain_numero_pour_annee(db, prefixe, 2027) == f"{prefixe}-2027-0000001"
    assert prochain_numero_pour_annee(db, prefixe, 2027).endswith("-0000002")


def test_annee_par_defaut_vient_de_now_en_utc(db: Session) -> None:
    prefixe = _prefixe_de_test()
    annee_base = db.execute(
        text("SELECT EXTRACT(YEAR FROM NOW() AT TIME ZONE 'UTC')::int")
    ).scalar_one()
    numero = prochain_numero(db, prefixe)
    # Le millésime du numéro est celui de NOW() côté base (le même que created_at d'une fiche).
    assert numero.split("-")[1] == str(annee_base)


def test_prefixes_par_type() -> None:
    assert prefixe_pour_type("individual") == "M"
    assert prefixe_pour_type("legal_entity") == "P"
    assert prefixe_pour_type("group") == "G"


def test_le_verrou_bloque_un_second_appel_concurrent() -> None:
    """LA PREUVE DU VERROU — le second appelant bloque, prouvé par le timeout.

    Deux connexions réelles, entrelacement fabriqué. Ne passe PAS par la fixture db : il faut
    deux transactions parallèles indépendantes, pas des savepoints d'une même transaction.
    """
    prefixe = _prefixe_de_test()
    annee = 2999
    connexion_1 = engine.connect()
    connexion_2 = engine.connect()
    try:
        transaction_1 = connexion_1.begin()
        # Connexion 1 alloue et NE COMMIT PAS : elle tient le verrou de ligne (prefix, annee).
        valeur_1 = connexion_1.execute(
            text(
                "INSERT INTO tiers.numbering_sequences (prefix, year, last_value) "
                "VALUES (:p, :a, 1) "
                "ON CONFLICT (prefix, year) DO UPDATE "
                "SET last_value = tiers.numbering_sequences.last_value + 1 "
                "RETURNING last_value"
            ),
            {"p": prefixe, "a": annee},
        ).scalar_one()
        assert valeur_1 == 1

        transaction_2 = connexion_2.begin()
        connexion_2.execute(text("SET LOCAL statement_timeout = '500ms'"))
        # Connexion 2 vise le MÊME (prefix, annee) : elle DOIT bloquer sur le verrou tenu par
        # la connexion 1, jusqu'à ce que le statement_timeout l'annule.
        with pytest.raises(OperationalError):
            connexion_2.execute(
                text(
                    "INSERT INTO tiers.numbering_sequences (prefix, year, last_value) "
                    "VALUES (:p, :a, 1) "
                    "ON CONFLICT (prefix, year) DO UPDATE "
                    "SET last_value = tiers.numbering_sequences.last_value + 1 "
                    "RETURNING last_value"
                ),
                {"p": prefixe, "a": annee},
            )
        transaction_2.rollback()

        # On relâche la connexion 1 sans rien persister : les deux transactions sont annulées.
        transaction_1.rollback()
    finally:
        connexion_2.close()
        connexion_1.close()
