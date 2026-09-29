"""Chantier coffre-fort/caisses, sous-chantier 1, Bloc 3 — `seed_comptabilite.
seed_niveaux_caisse_dev` : propose coffre/principale pour LE siège de dev, jamais le réseau,
jamais écrasé.

Ce que ces tests protègent :
  - Coffre ET principale sont rattachés pour l'agence donnée, à des comptes NEUFS créés sous
    1011 (comme 101111) — jamais en dehors de cette rubrique.
  - REJOUABLE sans jamais écraser un rattachement déjà posé — qu'il pointe vers un compte réel
    OU qu'il ait été explicitement VIDÉ (compte_caisse_id NULL) via l'écran (Bloc 2) : dans les
    deux cas, une ligne existante est un choix déjà fait, jamais un oubli à combler.
  - Les comptes de démo créés passent le garde-fou `comptabilite.comptes.compte_caisse_valide`
    (Bloc 1) — la preuve qu'ils sont bien sous 1011, pas une promesse en l'air.
  - Une agence sans AUCUN niveau paramétré : les 2 sont créés en un seul appel (résultat 2).
"""

import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.cli.seed_comptabilite import COMPTES_DEMO_NIVEAUX, seed_niveaux_caisse_dev
from app.core.database import engine
from app.modules.comptabilite.comptes import compte_caisse_valide
from app.modules.parameters.models import Agency

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
def agence(db: Session) -> Agency:
    agence = Agency(code=f"AG-{uuid.uuid4().hex[:6]}", name="Agence de test")
    db.add(agence)
    db.flush()
    return agence


def _niveaux(db: Session, agency_id: uuid.UUID) -> dict[str, uuid.UUID | None]:
    lignes = db.execute(
        text(
            "SELECT niveau, compte_caisse_id FROM caisse.niveaux_caisse WHERE agency_id = :a"
        ),
        {"a": agency_id},
    ).all()
    resultat: dict[str, uuid.UUID | None] = {}
    for niveau, compte_id in lignes:
        resultat[niveau] = compte_id
    return resultat


def _compte_id(db: Session, numero: str) -> uuid.UUID:
    return db.execute(
        text("SELECT id FROM comptabilite.accounts WHERE account_number = :n"), {"n": numero}
    ).scalar_one()


def test_cree_coffre_et_principale_pour_lagence(db: Session, agence: Agency) -> None:
    rattaches = seed_niveaux_caisse_dev(db, agence.id)

    assert rattaches == 2
    niveaux = _niveaux(db, agence.id)
    assert set(niveaux) == {"coffre", "principale"}
    assert niveaux["coffre"] == _compte_id(db, COMPTES_DEMO_NIVEAUX["coffre"].numero)
    assert niveaux["principale"] == _compte_id(db, COMPTES_DEMO_NIVEAUX["principale"].numero)


def test_rejouable_sans_effet_si_deja_parametre(db: Session, agence: Agency) -> None:
    # Premier passage : les 2 niveaux se rattachent aux comptes de démo.
    premier = seed_niveaux_caisse_dev(db, agence.id)
    assert premier == 2

    # Second passage : plus rien à faire, rien ne bouge.
    second = seed_niveaux_caisse_dev(db, agence.id)
    assert second == 0
    assert _niveaux(db, agence.id) == {
        "coffre": _compte_id(db, COMPTES_DEMO_NIVEAUX["coffre"].numero),
        "principale": _compte_id(db, COMPTES_DEMO_NIVEAUX["principale"].numero),
    }


def test_ne_remplace_pas_un_rattachement_deja_choisi_a_lecran(
    db: Session, agence: Agency
) -> None:
    # Un comptable a déjà rattaché « coffre » à un compte réel (101111) via le Bloc 2, AVANT
    # que le seed de dev ne soit rejoué.
    db.execute(
        text(
            "INSERT INTO caisse.niveaux_caisse (agency_id, niveau, compte_caisse_id) "
            "SELECT :a, 'coffre', id FROM comptabilite.accounts WHERE account_number = '101111'"
        ),
        {"a": agence.id},
    )

    rattaches = seed_niveaux_caisse_dev(db, agence.id)

    assert rattaches == 1  # seule « principale » était encore vierge
    niveaux = _niveaux(db, agence.id)
    assert niveaux["coffre"] == _compte_id(db, "101111")  # inchangé, jamais écrasé
    assert niveaux["principale"] == _compte_id(db, COMPTES_DEMO_NIVEAUX["principale"].numero)


def test_ne_remplace_pas_un_niveau_explicitement_vide(db: Session, agence: Agency) -> None:
    # Un comptable a explicitement VIDÉ « principale » (compte_caisse_id NULL) — un choix, pas
    # un oubli : le seed ne doit PAS y voir une occasion de proposer un défaut.
    db.execute(
        text(
            "INSERT INTO caisse.niveaux_caisse (agency_id, niveau, compte_caisse_id) "
            "VALUES (:a, 'principale', NULL)"
        ),
        {"a": agence.id},
    )

    rattaches = seed_niveaux_caisse_dev(db, agence.id)

    assert rattaches == 1  # seul « coffre » était vierge
    niveaux = _niveaux(db, agence.id)
    assert niveaux["principale"] is None  # toujours vidé, jamais réécrit
    assert niveaux["coffre"] == _compte_id(db, COMPTES_DEMO_NIVEAUX["coffre"].numero)


def test_les_comptes_demo_passent_le_garde_fou_1011(db: Session, agence: Agency) -> None:
    seed_niveaux_caisse_dev(db, agence.id)

    for compte in COMPTES_DEMO_NIVEAUX.values():
        valide = compte_caisse_valide(db, compte.numero)
        assert valide.account_number == compte.numero


def test_les_comptes_demo_sont_crees_sous_1011_marques_provisoires(
    db: Session, agence: Agency
) -> None:
    seed_niveaux_caisse_dev(db, agence.id)

    parent_1011 = _compte_id(db, "1011")
    for compte in COMPTES_DEMO_NIVEAUX.values():
        ligne = db.execute(
            text(
                "SELECT parent_id, is_system, is_provisional, is_posting "
                "FROM comptabilite.accounts WHERE account_number = :n"
            ),
            {"n": compte.numero},
        ).one()
        assert ligne.parent_id == parent_1011
        assert ligne.is_system is False
        assert ligne.is_provisional is True
        assert ligne.is_posting is True


def test_un_compte_deja_present_au_meme_numero_nest_pas_ecrase(
    db: Session, agence: Agency
) -> None:
    # Un numéro de démo qu'une IMF (hypothétiquement) aurait déjà utilisé pour AUTRE CHOSE ne
    # doit jamais être réécrit — ON CONFLICT DO NOTHING, pas un upsert. `ON CONFLICT DO NOTHING`
    # ici aussi côté test (pas un simple INSERT) : la base de dev RÉELLE peut déjà porter ce
    # numéro (un seed-dev appliqué pour de vrai avant ce test) — le test reste correct dans les
    # deux cas, sans dépendre de l'état préalable de la base partagée.
    numero = COMPTES_DEMO_NIVEAUX["principale"].numero
    parent_1011 = _compte_id(db, "1011")
    db.execute(
        text(
            "INSERT INTO comptabilite.accounts "
            "(account_number, name, account_class, parent_id, normal_side, is_posting) "
            "VALUES (:n, 'Compte deja existant', 1, :p, 'D', TRUE) "
            "ON CONFLICT (account_number) DO NOTHING"
        ),
        {"n": numero, "p": parent_1011},
    )
    nom_avant = db.execute(
        text("SELECT name FROM comptabilite.accounts WHERE account_number = :n"), {"n": numero}
    ).scalar_one()

    seed_niveaux_caisse_dev(db, agence.id)

    nom_apres = db.execute(
        text("SELECT name FROM comptabilite.accounts WHERE account_number = :n"), {"n": numero}
    ).scalar_one()
    assert nom_apres == nom_avant
