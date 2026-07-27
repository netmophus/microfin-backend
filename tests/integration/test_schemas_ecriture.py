"""Le moteur du pont comptable (E1) : poser une opération d'épargne en écriture équilibrée.

  - dépôt  -> D 5721 Caisse / C 3111 Épargne (pièce validée, équilibrée) ;
  - retrait -> l'inverse ;
  - REFUS PROPRE si un rattachement manque (produit sans compte d'épargne, agence sans caisse) :
    rien n'est écrit, message clair.

Les garde-fous comptables (équilibre, compte de saisie) sont ceux du service C2 : ils mordent
ici parce que le moteur pose une VRAIE pièce.
"""

import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import engine
from app.modules.epargne import service
from app.modules.epargne.models import Product, SavingsAccount
from app.modules.epargne.operations import (
    TYPE_DEPOT,
    TYPE_RETRAIT,
    RattachementManquantError,
    poser_ecriture_operation,
)
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


def _compte_id(db: Session, numero: str) -> uuid.UUID:
    return db.execute(
        text("SELECT id FROM comptabilite.accounts WHERE account_number = :n"), {"n": numero}
    ).scalar_one()


def _tier_actif(db: Session, agency_id: uuid.UUID, suffixe: str) -> uuid.UUID:
    return db.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES (:num, 'individual', :ag, 'actif') RETURNING id"
        ),
        {"num": f"M-ECR-{suffixe}", "ag": agency_id},
    ).scalar_one()


def _agence(db: Session, suffixe: str, compte_caisse_id: uuid.UUID | None) -> Agency:
    ag = Agency(code=f"AGE-{suffixe}", name=f"Agence {suffixe}", compte_caisse_id=compte_caisse_id)
    db.add(ag)
    db.flush()
    return ag


def _produit(db: Session, suffixe: str, compte_epargne_id: uuid.UUID | None) -> Product:
    p = Product(
        code=f"PE{suffixe}", name="Produit", type="a_vue", compte_epargne_id=compte_epargne_id
    )
    db.add(p)
    db.flush()
    return p


def _ouvrir(db: Session, agence: Agency, produit: Product) -> SavingsAccount:
    tier_id = _tier_actif(db, agence.id, produit.code[-2:])
    return service.ouvrir_compte(
        db, tier_id=tier_id, product_id=produit.id, agency_id=agence.id, par=None
    )


def _lignes(db: Session, entry_id: uuid.UUID) -> set[tuple[str, str, int]]:
    return {
        (num, side, amount)
        for num, side, amount in db.execute(
            text(
                "SELECT a.account_number, l.side, l.amount FROM comptabilite.journal_lines l "
                "JOIN comptabilite.accounts a ON a.id = l.account_id WHERE l.entry_id = :e"
            ),
            {"e": entry_id},
        )
    }


# --- Le chemin nominal --------------------------------------------------------------


def test_un_depot_pose_debit_caisse_credit_epargne(db: Session) -> None:
    agence = _agence(db, "D1", _compte_id(db, "5721"))
    produit = _produit(db, "D1", _compte_id(db, "3111"))
    compte = _ouvrir(db, agence, produit)

    piece = poser_ecriture_operation(db, compte, TYPE_DEPOT, 10000, par=None)

    assert piece.status == "validee"
    assert _lignes(db, piece.id) == {("5721", "D", 10000), ("3111", "C", 10000)}


def test_un_retrait_pose_debit_epargne_credit_caisse(db: Session) -> None:
    agence = _agence(db, "R1", _compte_id(db, "5721"))
    produit = _produit(db, "R1", _compte_id(db, "3111"))
    compte = _ouvrir(db, agence, produit)

    piece = poser_ecriture_operation(db, compte, TYPE_RETRAIT, 2500, par=None)

    assert _lignes(db, piece.id) == {("3111", "D", 2500), ("5721", "C", 2500)}


# --- Le refus propre si un rattachement manque --------------------------------------


def test_refus_propre_si_produit_sans_compte_epargne(db: Session) -> None:
    agence = _agence(db, "X1", _compte_id(db, "5721"))
    produit = _produit(db, "X1", None)  # pas de compte d'épargne rattaché
    compte = _ouvrir(db, agence, produit)

    with pytest.raises(RattachementManquantError) as exc:
        poser_ecriture_operation(db, compte, TYPE_DEPOT, 10000, par=None)
    assert "compte d'épargne" in str(exc.value)
    # Rien écrit : aucune pièce.
    assert db.execute(text("SELECT count(*) FROM comptabilite.journal_entries")).scalar_one() == 0


def test_refus_propre_si_agence_sans_caisse(db: Session) -> None:
    agence = _agence(db, "X2", None)  # pas de caisse rattachée
    produit = _produit(db, "X2", _compte_id(db, "3111"))
    compte = _ouvrir(db, agence, produit)

    with pytest.raises(RattachementManquantError) as exc:
        poser_ecriture_operation(db, compte, TYPE_DEPOT, 10000, par=None)
    assert "caisse" in str(exc.value)
    assert db.execute(text("SELECT count(*) FROM comptabilite.journal_entries")).scalar_one() == 0
