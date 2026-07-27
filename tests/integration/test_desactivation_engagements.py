"""Désactivation d'un tiers bloquée par ses engagements d'épargne — garde-fous vus mordre.

L'interaction Épargne -> désactivation, promise de longue date : on ne fait pas SORTIR de
l'annuaire un membre dont l'IMF détient encore l'épargne. Trois états d'un compte :
  - solde non nul  -> refus « soldez-le » ;
  - ouvert à zéro  -> refus « fermez-le » (un compte ouvert est une relation active) ;
  - fermé          -> aucun obstacle.

Et le méta-test qui prouve que le vérificateur d'épargne est bien ENREGISTRÉ à l'assemblage de
l'app (sinon le garde-fou serait vert mais inerte).
"""

import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import engine
from app.core.engagements import verificateurs_enregistres
from app.modules.audit.service import CONTEXTE_VIDE
from app.modules.epargne import service as epargne
from app.modules.epargne.engagements import enregistrer, verifier_engagements_epargne
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.tiers.cycle_de_vie import EngagementsOuvertsError, executer_transition

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _brancher_verificateur() -> None:
    # Idempotent : garantit que l'épargne est enregistrée quel que soit l'ordre des tests.
    enregistrer()


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


def _acteur(db: Session, agency_id: uuid.UUID) -> UtilisateurCourant:
    user_id = db.execute(text("SELECT id FROM security.users LIMIT 1")).scalar_one()
    return UtilisateurCourant(
        user_id=user_id,
        roles=("RESPONSABLE_AGENCE",),
        permissions=frozenset({"tiers.deactivate"}),
        primary_agency_id=agency_id,
        agency_id=agency_id,
        voit_tout=True,
    )


def _membre_actif(db: Session, agency_id: uuid.UUID, suffixe: str) -> uuid.UUID:
    """Crée un membre ACTIF bien formé : la ligne parent tiers.tiers ET son profil individuel
    (héritage joint), sinon un flush polymorphe de la fiche buterait sur l'enfant absent."""
    tier_id = db.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES (:num, 'individual', :ag, 'actif') RETURNING id"
        ),
        {"num": f"M-DESAC-{suffixe}", "ag": agency_id},
    ).scalar_one()
    nationalite = db.execute(text("SELECT id FROM parameters.countries LIMIT 1")).scalar_one()
    db.execute(
        text(
            "INSERT INTO tiers.individual_profiles "
            "(tier_id, last_name, first_name, birth_date, gender, nationality_id) "
            "VALUES (:t, 'Test', 'Membre', '1990-01-01', 'M', :nat)"
        ),
        {"t": tier_id, "nat": nationalite},
    )
    return tier_id


def _cadre(db: Session, suffixe: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Rend (agency_id, product_id, tier_id actif)."""
    agency_id = db.execute(text("SELECT id FROM parameters.agencies LIMIT 1")).scalar_one()
    product_id = db.execute(
        text(
            "INSERT INTO epargne.products (code, name, type) "
            "VALUES (:c, 'Produit test', 'a_vue') RETURNING id"
        ),
        {"c": f"TP{suffixe}"},
    ).scalar_one()
    return agency_id, product_id, _membre_actif(db, agency_id, suffixe)


def _ouvrir(db: Session, agency_id: uuid.UUID, product_id: uuid.UUID, tier_id: uuid.UUID):
    return epargne.ouvrir_compte(
        db, tier_id=tier_id, product_id=product_id, agency_id=agency_id, par=None
    )


def _crediter(db: Session, account_id: uuid.UUID, montant: int) -> None:
    db.execute(
        text(
            "INSERT INTO epargne.movements "
            "(account_id, sens, amount, balance_after, operation_type) "
            "VALUES (:a, 'credit', :m, :m, 'depot')"
        ),
        {"a": account_id, "m": montant},
    )
    db.execute(
        text("UPDATE epargne.accounts SET balance = :m WHERE id = :a"),
        {"m": montant, "a": account_id},
    )


def _desactiver(db: Session, courant: UtilisateurCourant, tier_id: uuid.UUID):
    return executer_transition(db, courant, tier_id, "deactivate", CONTEXTE_VIDE, motif="test")


# --- Les trois états ----------------------------------------------------------------


def test_desactivation_refusee_si_compte_a_solde_non_nul(db: Session) -> None:
    agency_id, product_id, tier_id = _cadre(db, "A")
    compte = _ouvrir(db, agency_id, product_id, tier_id)
    _crediter(db, compte.id, 15000)

    with pytest.raises(EngagementsOuvertsError) as exc:
        _desactiver(db, _acteur(db, agency_id), tier_id)

    (engagement,) = exc.value.engagements
    assert compte.account_number in engagement.libelle
    assert "solde de 15000" in engagement.libelle and "soldez" in engagement.libelle.lower()


def test_desactivation_refusee_si_compte_ouvert_vide(db: Session) -> None:
    agency_id, product_id, tier_id = _cadre(db, "B")
    compte = _ouvrir(db, agency_id, product_id, tier_id)  # solde 0, mais OUVERT

    with pytest.raises(EngagementsOuvertsError) as exc:
        _desactiver(db, _acteur(db, agency_id), tier_id)

    (engagement,) = exc.value.engagements
    assert compte.account_number in engagement.libelle
    assert "fermez" in engagement.libelle.lower()


def test_desactivation_autorisee_si_tous_les_comptes_sont_fermes(db: Session) -> None:
    agency_id, product_id, tier_id = _cadre(db, "C")
    compte = _ouvrir(db, agency_id, product_id, tier_id)
    epargne.cloturer_compte(db, compte, par=None)  # solde 0 -> fermeture OK

    tier = _desactiver(db, _acteur(db, agency_id), tier_id)

    assert tier.status == "desactive"


def test_tous_les_comptes_ouverts_sont_listes_dun_coup(db: Session) -> None:
    agency_id, product_id, tier_id = _cadre(db, "D")
    c1 = _ouvrir(db, agency_id, product_id, tier_id)
    c2 = _ouvrir(db, agency_id, product_id, tier_id)

    with pytest.raises(EngagementsOuvertsError) as exc:
        _desactiver(db, _acteur(db, agency_id), tier_id)

    references = {e.reference for e in exc.value.engagements}
    assert references == {c1.account_number, c2.account_number}


# (La fermeture est testée en détail dans test_fermeture.py — restitution, définitif, etc.)


# --- Non-inertie : le vérificateur est bien branché à l'app -------------------------


def test_le_verificateur_epargne_est_enregistre_a_lassemblage_de_lapp() -> None:
    # Importer l'app doit brancher le vérificateur (sinon garde-fou vert mais INERTE).
    import app.main  # noqa: F401 — l'import a pour effet d'enregistrer les vérificateurs

    assert verifier_engagements_epargne in verificateurs_enregistres()
