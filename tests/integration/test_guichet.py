"""Opérations de guichet (E3) : dépôt / retrait — chaque garde-fou VU MORDRE.

Garde-fous métier (fixture transactionnelle) : plancher, découvert, gel si membre suspendu,
compte fermé, montant invalide, cloisonnement, rapprochement de bout en bout.
Concurrence et atomicité (vraies connexions, déterministe) dans test_guichet_concurrence.py.
"""

import uuid
from collections.abc import Generator
from dataclasses import dataclass

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import engine
from app.modules.epargne import service
from app.modules.epargne.guichet import (
    CompteClotureError,
    CompteIntrouvableError,
    MontantInvalideError,
    OperationGeleeError,
    SoldeInsuffisantError,
    deposer,
    retirer,
)
from app.modules.epargne.models import Product, SavingsAccount
from app.modules.epargne.rapprochement import rapprocher
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant

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


def _caissier(db: Session, agency_id: uuid.UUID) -> UtilisateurCourant:
    uid = db.execute(text("SELECT id FROM security.users LIMIT 1")).scalar_one()
    return UtilisateurCourant(
        user_id=uid,
        roles=("CAISSIER",),
        permissions=frozenset({"epargne.operation.deposit", "epargne.operation.withdraw"}),
        primary_agency_id=agency_id,
        agency_id=agency_id,
        voit_tout=False,  # cloisonné à son agence
    )


@dataclass
class Cadre:
    agence: Agency
    produit: Product
    compte: SavingsAccount
    caissier: UtilisateurCourant


def _cadre(db: Session, suffixe: str, *, min_balance: int = 0, decouvert: int = 0) -> Cadre:
    agence = Agency(code=f"AGG-{suffixe}", name="Agence", compte_caisse_id=_compte_id(db, "1011"))
    produit = Product(
        code=f"PG{suffixe}", name="Épargne", type="a_vue",
        compte_epargne_id=_compte_id(db, "251111"), min_balance=min_balance,
        decouvert_autorise=decouvert,
    )
    db.add_all([agence, produit])
    db.flush()
    tier_id = db.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES (:num, 'individual', :ag, 'actif') RETURNING id"
        ),
        {"num": f"M-GUI-{suffixe}", "ag": agence.id},
    ).scalar_one()
    compte = service.ouvrir_compte(
        db, tier_id=tier_id, product_id=produit.id, agency_id=agence.id, par=None
    )
    return Cadre(agence, produit, compte, _caissier(db, agence.id))


# --- Chemin nominal -----------------------------------------------------------------


def test_depot_credite_le_solde_et_pose_la_piece(db: Session) -> None:
    c = _cadre(db, "N1")
    resultat = deposer(db, c.caissier, c.compte.id, 10000)

    assert resultat.nouveau_solde == 10000
    assert resultat.entry_number is not None
    # un mouvement credit relié à une pièce
    lien = db.execute(
        text(
            "SELECT sens, amount, journal_entry_id IS NOT NULL FROM epargne.movements "
            "WHERE account_id = :a"
        ),
        {"a": c.compte.id},
    ).one()
    assert lien == ("credit", 10000, True)


def test_retrait_debite_le_solde(db: Session) -> None:
    c = _cadre(db, "N2")
    deposer(db, c.caissier, c.compte.id, 10000)
    resultat = retirer(db, c.caissier, c.compte.id, 3000)
    assert resultat.nouveau_solde == 7000


# --- Plancher et découvert ----------------------------------------------------------


def test_retrait_superieur_au_solde_refuse(db: Session) -> None:
    c = _cadre(db, "P1")  # à vue standard : plancher 0, découvert 0
    deposer(db, c.caissier, c.compte.id, 1000)
    with pytest.raises(SoldeInsuffisantError):
        retirer(db, c.caissier, c.compte.id, 1500)


def test_le_plancher_solde_minimum_est_respecte(db: Session) -> None:
    c = _cadre(db, "P2", min_balance=500)
    deposer(db, c.caissier, c.compte.id, 1000)
    # disponible = 1000 - 500 + 0 = 500
    with pytest.raises(SoldeInsuffisantError):
        retirer(db, c.caissier, c.compte.id, 600)
    assert retirer(db, c.caissier, c.compte.id, 500).nouveau_solde == 500


def test_le_decouvert_autorise_laisse_passer_sous_zero(db: Session) -> None:
    c = _cadre(db, "P3", decouvert=2000)
    deposer(db, c.caissier, c.compte.id, 1000)
    # disponible = 1000 - 0 + 2000 = 3000
    resultat = retirer(db, c.caissier, c.compte.id, 2500)
    assert resultat.nouveau_solde == -1500


# --- Gel, fermeture, montant, cloisonnement -----------------------------------------


def test_operation_sur_membre_suspendu_refusee(db: Session) -> None:
    c = _cadre(db, "S1")
    db.execute(
        text("UPDATE tiers.tiers SET status = 'suspendu_temporaire' WHERE id = :t"),
        {"t": c.compte.tier_id},
    )
    with pytest.raises(OperationGeleeError):
        deposer(db, c.caissier, c.compte.id, 1000)


def test_operation_sur_compte_ferme_refusee(db: Session) -> None:
    c = _cadre(db, "F1")
    db.execute(
        text("UPDATE epargne.accounts SET status = 'cloture' WHERE id = :a"), {"a": c.compte.id}
    )
    with pytest.raises(CompteClotureError):
        deposer(db, c.caissier, c.compte.id, 1000)


def test_montant_nul_ou_negatif_refuse(db: Session) -> None:
    c = _cadre(db, "M1")
    with pytest.raises(MontantInvalideError):
        deposer(db, c.caissier, c.compte.id, 0)
    with pytest.raises(MontantInvalideError):
        retirer(db, c.caissier, c.compte.id, -5)


def test_compte_hors_perimetre_est_introuvable(db: Session) -> None:
    c = _cadre(db, "C1")
    autre_agence = db.execute(
        text(
            "INSERT INTO parameters.agencies (code, name) "
            "VALUES ('AG-AUTRE', 'Autre') RETURNING id"
        )
    ).scalar_one()
    caissier_autre = _caissier(db, autre_agence)  # cloisonné à une AUTRE agence
    with pytest.raises(CompteIntrouvableError):
        deposer(db, caissier_autre, c.compte.id, 1000)


# --- Rapprochement de bout en bout (E1, effectif) -----------------------------------


def test_rapprochement_concorde_apres_une_serie_doperations(db: Session) -> None:
    c251111 = _compte_id(db, "251111")
    c1 = _cadre(db, "R1")
    # deuxième compte dans la même agence, même produit (rattaché à 251111)
    tier2 = db.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES ('M-GUI-R2', 'individual', :ag, 'actif') RETURNING id"
        ),
        {"ag": c1.agence.id},
    ).scalar_one()
    c2 = service.ouvrir_compte(
        db, tier_id=tier2, product_id=c1.produit.id, agency_id=c1.agence.id, par=None
    )
    # Base AVANT nos opérations (la base de dev peut porter d'autres comptes rattachés à 251111).
    base = rapprocher(db, c251111)

    deposer(db, c1.caissier, c1.compte.id, 1000)
    deposer(db, c1.caissier, c2.id, 500)
    retirer(db, c1.caissier, c1.compte.id, 300)

    resultat = rapprocher(db, c251111)
    assert resultat.concordant is True
    # Nos opérations font bouger auxiliaire ET général du MÊME montant (700 + 500).
    assert resultat.auxiliaire - base.auxiliaire == 1200
    assert resultat.general - base.general == 1200
