"""Héritage du mapping des états financiers à la création d'un compte, et rattrapage des orphelins.

Un compte créé à l'écran n'a pas de ligne dans `financial_statement_mapping` (le seed ne lit que
le CSV) : il sortirait du bilan. Règle : il hérite de la ligne de son parent si le parent en a une
(`gere_manuellement = FALSE`) ; sinon, aucune ligne n'est devinée.
"""

from collections.abc import Generator

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.cli.seed_financial_statement_mapping import executer_seed_mapping_etats
from app.core.database import engine
from app.modules.comptabilite import comptes, etats_financiers
from app.modules.comptabilite.models import Account, FinancialStatementMapping

pytestmark = pytest.mark.integration

PARENT_MAPPE = "101111"  # Caisse (agence) -> « Valeurs en caisse »
POSTE_PARENT = "Valeurs en caisse"
ENFANT = "10111199"


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


def _creer(db: Session, numero: str, parent: str | None) -> Account:
    return comptes.creer(
        db,
        account_number=numero,
        name=f"Compte {numero}",
        short_name=None,
        account_class=int(numero[0]),
        parent_number=parent,
        normal_side="D",
        is_posting=True,
        notes=None,
        par=None,
    )


def _mapping(db: Session, compte: Account) -> FinancialStatementMapping | None:
    return db.get(FinancialStatementMapping, compte.id)


def _mention_audit(db: Session, compte: Account) -> str:
    return db.execute(
        text(
            "SELECT new_values->>'mapping' FROM audit.audit_logs "
            "WHERE action = 'compta.plan.created' AND resource_id = :r"
        ),
        {"r": compte.id},
    ).scalar_one()


def test_creation_sous_un_parent_mappe_herite_de_sa_ligne(db: Session) -> None:
    parent = db.execute(select(Account).where(Account.account_number == PARENT_MAPPE)).scalar_one()
    modele = db.get(FinancialStatementMapping, parent.id)
    assert modele is not None

    enfant = _creer(db, ENFANT, PARENT_MAPPE)

    ligne = _mapping(db, enfant)
    assert ligne is not None
    assert (ligne.etat, ligne.masse, ligne.poste_libelle, ligne.poste_ordre) == (
        modele.etat,
        modele.masse,
        modele.poste_libelle,
        modele.poste_ordre,
    )
    assert ligne.gere_manuellement is False
    assert _mention_audit(db, enfant) == f"mapping hérité de {PARENT_MAPPE} : {POSTE_PARENT}"


def test_creation_sous_un_parent_non_mappe_ne_cree_aucune_ligne(db: Session) -> None:
    parent = _creer(db, "1T9000", None)  # compte neuf : aucun mapping
    assert _mapping(db, parent) is None

    enfant = _creer(db, "1T900001", "1T9000")

    assert _mapping(db, enfant) is None
    assert _mention_audit(db, enfant) == "sans mapping (parent non mappé)"


def test_creation_sans_parent_ne_cree_aucune_ligne(db: Session) -> None:
    compte = _creer(db, "1T9100", None)

    assert _mapping(db, compte) is None
    assert _mention_audit(db, compte) == "sans mapping (parent non mappé)"


def test_le_seed_reecrit_la_ligne_heritee_si_le_csv_la_contient(db: Session, tmp_path) -> None:
    enfant = _creer(db, ENFANT, PARENT_MAPPE)
    csv = tmp_path / "mapping.csv"
    csv.write_text(
        "account_number;etat;masse;poste_libelle\n"
        f"{ENFANT};BILAN;ACTIF;Poste du CSV\n",
        encoding="utf-8",
    )

    executer_seed_mapping_etats(db, csv)

    ligne = _mapping(db, enfant)
    assert ligne is not None
    db.refresh(ligne)
    assert ligne.poste_libelle == "Poste du CSV"  # FALSE respecté : le seed garde la main


def test_le_patch_ecran_sur_la_ligne_heritee_la_verrouille_contre_le_seed(
    db: Session, tmp_path
) -> None:
    enfant = _creer(db, ENFANT, PARENT_MAPPE)

    etats_financiers.modifier_mapping(
        db, enfant.id, etat="BILAN", masse="ACTIF", poste_libelle="Ajusté", poste_ordre=5, par=None
    )

    ligne = _mapping(db, enfant)
    assert ligne is not None and ligne.gere_manuellement is True
    csv = tmp_path / "mapping.csv"
    csv.write_text(
        f"account_number;etat;masse;poste_libelle\n{ENFANT};BILAN;ACTIF;Poste du CSV\n",
        encoding="utf-8",
    )
    executer_seed_mapping_etats(db, csv)
    db.refresh(ligne)
    assert ligne.poste_libelle == "Ajusté"


def _creer_orphelin(db: Session, numero: str, parent: Account) -> Account:
    """Compte rattaché à `parent` SANS passer par creer() : l'état d'une base d'avant la règle."""
    compte = Account(
        account_number=numero,
        name=f"Orphelin {numero}",
        account_class=int(numero[0]),
        parent_id=parent.id,
        normal_side="D",
        is_posting=True,
        is_system=False,
        is_provisional=False,
    )
    db.add(compte)
    db.flush()
    return compte


def test_rattrapage_cree_les_lignes_manquantes_et_signale_les_ignores(db: Session) -> None:
    parent = db.execute(select(Account).where(Account.account_number == PARENT_MAPPE)).scalar_one()
    orphelin = _creer_orphelin(db, ENFANT, parent)
    petit_orphelin = _creer_orphelin(db, ENFANT + "1", orphelin)  # chaîne : parent mappé en cours
    sans_parent_mappe = _creer(db, "1T9200", None)
    enfant_non_mappe = _creer_orphelin(db, "1T920001", sans_parent_mappe)

    rapport = etats_financiers.rattraper_mapping_orphelins(db)

    crees = {numero for numero, _, _ in rapport.crees}
    assert {ENFANT, ENFANT + "1"} <= crees
    assert (ENFANT, PARENT_MAPPE, POSTE_PARENT) in rapport.crees
    ignores = dict(rapport.ignores)
    assert ignores["1T920001"] == "parent 1T9200 non mappé"
    assert _mapping(db, orphelin) is not None and _mapping(db, petit_orphelin) is not None
    assert _mapping(db, enfant_non_mappe) is None
    # Idempotent : plus rien à créer au second passage.
    assert etats_financiers.rattraper_mapping_orphelins(db).crees == []
