"""Rapprochement collectif (251111) ↔ auxiliaire (soldes épargne) — vu concorder ET DÉTECTER.

Fondations posées en E1 : la fonction existe et le compte général est identifiable. On la prouve
ici avec des opérations réelles côté COMPTABILITÉ (le moteur pose D 101111 / C 251111) et le solde
auxiliaire tenu en regard :
  - les deux concordent au franc ;
  - si on FAUSSE un solde à la main, le rapprochement CRIE (écart non nul).

C'est ce qu'un contrôleur BCEAO vérifie en premier ; on le détecte nous-mêmes. La vérification
de bout en bout (le dépôt qui produit ensemble le mouvement ET la pièce) mordra en E3.
"""

import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import engine
from app.modules.epargne import service
from app.modules.epargne.models import Product, SavingsAccount
from app.modules.epargne.operations import TYPE_DEPOT, poser_ecriture_operation
from app.modules.epargne.rapprochement import rapprocher
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


def _ouvrir(
    db: Session, agency_id: uuid.UUID, product_id: uuid.UUID, suffixe: str
) -> SavingsAccount:
    tier_id = db.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES (:num, 'individual', :ag, 'actif') RETURNING id"
        ),
        {"num": f"M-RAP-{suffixe}", "ag": agency_id},
    ).scalar_one()
    return service.ouvrir_compte(
        db, tier_id=tier_id, product_id=product_id, agency_id=agency_id, par=None
    )


def _deposer(db: Session, compte: SavingsAccount, montant: int) -> None:
    """Simule ce que fera E3 : la pièce comptable ET la mise à jour du solde auxiliaire."""
    poser_ecriture_operation(db, compte, TYPE_DEPOT, montant, par=None)
    db.execute(
        text("UPDATE epargne.accounts SET balance = balance + :m WHERE id = :a"),
        {"m": montant, "a": compte.id},
    )


@pytest.fixture
def cadre(db: Session) -> tuple[uuid.UUID, SavingsAccount, SavingsAccount]:
    """Un compte général 251111, une agence à caisse, deux comptes d'épargne.

    Rend (251111, c1, c2)."""
    compte_251111 = _compte_id(db, "251111")
    agence = Agency(code="AGE-RAP", name="Agence rappro", compte_caisse_id=_compte_id(db, "101111"))
    produit = Product(code="PRAP", name="Épargne", type="a_vue", compte_epargne_id=compte_251111)
    db.add_all([agence, produit])
    db.flush()
    return (
        compte_251111,
        _ouvrir(db, agence.id, produit.id, "1"),
        _ouvrir(db, agence.id, produit.id, "2"),
    )


def test_le_rapprochement_concorde_apres_operations(
    db: Session, cadre: tuple[uuid.UUID, SavingsAccount, SavingsAccount]
) -> None:
    compte_251111, c1, c2 = cadre
    base = rapprocher(db, compte_251111)  # d'autres comptes rattachés à 251111 peuvent exister
    _deposer(db, c1, 1000)
    _deposer(db, c2, 500)

    resultat = rapprocher(db, compte_251111)

    assert resultat.concordant is True
    assert resultat.auxiliaire - base.auxiliaire == 1500
    assert resultat.general - base.general == 1500


def test_le_rapprochement_detecte_un_solde_fausse(
    db: Session, cadre: tuple[uuid.UUID, SavingsAccount, SavingsAccount]
) -> None:
    compte_251111, c1, c2 = cadre
    _deposer(db, c1, 1000)
    _deposer(db, c2, 500)
    assert rapprocher(db, compte_251111).concordant is True

    # On FAUSSE un solde auxiliaire à la main : le général (les écritures) ne bouge pas.
    db.execute(
        text("UPDATE epargne.accounts SET balance = balance + 200 WHERE id = :a"), {"a": c1.id}
    )

    resultat = rapprocher(db, compte_251111)
    assert resultat.concordant is False
    # L'écart introduit vaut exactement la falsification, quelle que soit la base concordante.
    assert resultat.ecart == 200
