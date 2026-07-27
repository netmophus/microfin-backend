"""Ouverture d'un compte d'épargne + cohérence du solde — les garde-fous vus mordre.

  - GATE KYC, service ET base : on n'ouvre un compte que pour un membre ACTIF. Un prospect (y
    compris un ancien membre redevenu prospect par réactivation) ou un membre suspendu est
    refusé — au service (erreur claire) ET par le trigger base (dernier rempart).
  - COHÉRENCE DU SOLDE : le cache se vérifie, ne se croit pas. Après N mouvements, cache == Σ ;
    si on FAUSSE le cache à la main, la vérification DÉTECTE l'écart.
  - MOUVEMENT append-only : ni modification ni suppression.
"""

import uuid
from collections.abc import Generator
from dataclasses import dataclass

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, InternalError
from sqlalchemy.orm import Session

from app.core.database import engine
from app.modules.epargne import service
from app.modules.epargne.models import Product, SavingsAccount
from app.modules.epargne.service import MembreNonActifError

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


def _inserer_tier(db: Session, agency_id: uuid.UUID, statut: str, suffixe: str) -> uuid.UUID:
    return db.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES (:num, 'individual', :ag, :st) RETURNING id"
        ),
        {"num": f"M-TEST-{suffixe}", "ag": agency_id, "st": statut},
    ).scalar_one()


@dataclass
class Cadre:
    agency_id: uuid.UUID
    product_id: uuid.UUID
    tier_actif: uuid.UUID
    tier_prospect: uuid.UUID
    tier_suspendu: uuid.UUID


@pytest.fixture
def cadre(db: Session) -> Cadre:
    agency_id = db.execute(text("SELECT id FROM parameters.agencies LIMIT 1")).scalar_one()
    produit = Product(code="TSTP", name="Produit de test", type="a_vue")
    db.add(produit)
    db.flush()
    return Cadre(
        agency_id=agency_id,
        product_id=produit.id,
        tier_actif=_inserer_tier(db, agency_id, "actif", "ACTIF"),
        tier_prospect=_inserer_tier(db, agency_id, "prospect", "PROSPECT"),
        tier_suspendu=_inserer_tier(db, agency_id, "suspendu_temporaire", "SUSP"),
    )


def _ouvrir(db: Session, cadre: Cadre, tier_id: uuid.UUID) -> SavingsAccount:
    return service.ouvrir_compte(
        db, tier_id=tier_id, product_id=cadre.product_id, agency_id=cadre.agency_id, par=None
    )


# --- Gate KYC : membre actif seulement ----------------------------------------------


def test_ouvrir_sur_un_membre_actif_reussit(db: Session, cadre: Cadre) -> None:
    compte = _ouvrir(db, cadre, cadre.tier_actif)
    assert compte.status == "actif"
    assert compte.balance == 0
    assert compte.account_number.startswith("EP-")


def test_ouvrir_sur_un_prospect_refuse_au_service(db: Session, cadre: Cadre) -> None:
    with pytest.raises(MembreNonActifError):
        _ouvrir(db, cadre, cadre.tier_prospect)


def test_ouvrir_sur_un_membre_suspendu_refuse_au_service(db: Session, cadre: Cadre) -> None:
    with pytest.raises(MembreNonActifError):
        _ouvrir(db, cadre, cadre.tier_suspendu)


def test_ouvrir_sur_un_prospect_refuse_par_la_base(db: Session, cadre: Cadre) -> None:
    # SQL brut : on court-circuite le service. Le trigger base doit mordre.
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text(
                "INSERT INTO epargne.accounts (account_number, product_id, tier_id, agency_id) "
                "VALUES ('EP-BRUT-0000001', :p, :t, :a)"
            ),
            {"p": cadre.product_id, "t": cadre.tier_prospect, "a": cadre.agency_id},
        )
    assert "actif" in str(exc.value).lower()


# --- Numérotation atomique sans trou ------------------------------------------------


def test_numeros_de_compte_sequentiels(db: Session, cadre: Cadre) -> None:
    # Un membre peut avoir plusieurs comptes ; les numéros se suivent.
    n1 = _ouvrir(db, cadre, cadre.tier_actif).account_number
    n2 = _ouvrir(db, cadre, cadre.tier_actif).account_number
    rang1 = int(n1.rsplit("-", 1)[1])
    rang2 = int(n2.rsplit("-", 1)[1])
    assert rang2 == rang1 + 1


# --- Cohérence du solde : le cache se vérifie ---------------------------------------


def _mouvement_sql(
    db: Session, account_id: uuid.UUID, sens: str, amount: int, balance_after: int
) -> None:
    db.execute(
        text(
            "INSERT INTO epargne.movements "
            "(account_id, sens, amount, balance_after, operation_type) "
            "VALUES (:a, :s, :m, :b, 'depot')"
        ),
        {"a": account_id, "s": sens, "m": amount, "b": balance_after},
    )


def _fixer_cache(db: Session, account_id: uuid.UUID, solde: int) -> None:
    db.execute(
        text("UPDATE epargne.accounts SET balance = :b WHERE id = :a"),
        {"b": solde, "a": account_id},
    )


def test_coherence_ok_apres_plusieurs_mouvements(db: Session, cadre: Cadre) -> None:
    compte = _ouvrir(db, cadre, cadre.tier_actif)
    # credit 1000, credit 500, debit 300 -> solde attendu 1200
    _mouvement_sql(db, compte.id, "credit", 1000, 1000)
    _mouvement_sql(db, compte.id, "credit", 500, 1500)
    _mouvement_sql(db, compte.id, "debit", 300, 1200)
    _fixer_cache(db, compte.id, 1200)

    resultat = service.verifier_coherence_solde(db, compte.id)

    assert resultat.calcule == 1200
    assert resultat.cache == 1200
    assert resultat.coherent is True
    assert resultat.ecart == 0


def test_coherence_detecte_un_cache_fausse(db: Session, cadre: Cadre) -> None:
    compte = _ouvrir(db, cadre, cadre.tier_actif)
    _mouvement_sql(db, compte.id, "credit", 1000, 1000)
    _fixer_cache(db, compte.id, 1000)
    assert service.verifier_coherence_solde(db, compte.id).coherent is True

    # On FAUSSE le cache à la main : la somme des mouvements ne bouge pas.
    _fixer_cache(db, compte.id, 2000)

    resultat = service.verifier_coherence_solde(db, compte.id)
    assert resultat.coherent is False
    assert resultat.calcule == 1000
    assert resultat.cache == 2000
    assert resultat.ecart == 1000


# --- Mouvement append-only ----------------------------------------------------------


def test_un_mouvement_ne_se_modifie_pas(db: Session, cadre: Cadre) -> None:
    compte = _ouvrir(db, cadre, cadre.tier_actif)
    _mouvement_sql(db, compte.id, "credit", 1000, 1000)
    with pytest.raises((IntegrityError, InternalError)) as exc:
        db.execute(
            text("UPDATE epargne.movements SET amount = 999 WHERE account_id = :a"),
            {"a": compte.id},
        )
    assert "immuable" in str(exc.value).lower()


def test_un_mouvement_ne_se_supprime_pas(db: Session, cadre: Cadre) -> None:
    compte = _ouvrir(db, cadre, cadre.tier_actif)
    _mouvement_sql(db, compte.id, "credit", 1000, 1000)
    with pytest.raises((IntegrityError, InternalError)):
        db.execute(
            text("DELETE FROM epargne.movements WHERE account_id = :a"), {"a": compte.id}
        )
