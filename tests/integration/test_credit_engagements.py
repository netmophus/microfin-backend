"""Désactivation d'un tiers bloquée par un crédit décaissé — garde-fou vu mordre (CR3+CR4).

Branché DÈS le décaissement (pas attendu jusqu'aux remboursements) : tant qu'un crédit
'decaisse' a au moins une échéance 'a_echoir', le tiers ne peut pas être désactivé. Une fois
TOUTES les échéances payées (CR4), le blocage tombe — la boucle complète, comme Épargne et
Parts. Et le méta-test qui prouve que le vérificateur de crédit est bien ENREGISTRÉ à
l'assemblage de l'app (sinon le garde-fou serait vert mais inerte) — même patron que
test_desactivation_engagements.py (Épargne)."""

import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine
from app.core.engagements import verificateurs_enregistres
from app.modules.audit.service import CONTEXTE_VIDE
from app.modules.credit.decaissement import decaisser
from app.modules.credit.demandes import creer_demande, decider
from app.modules.credit.engagements import enregistrer, verifier_engagements_credit
from app.modules.credit.models import Installment, Product
from app.modules.credit.remboursement import rembourser
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.tiers.cycle_de_vie import EngagementsOuvertsError, executer_transition

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _brancher_verificateur() -> None:
    # Idempotent : garantit que le crédit est enregistré quel que soit l'ordre des tests.
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


def _cid(db: Session, numero: str) -> uuid.UUID:
    return db.execute(
        text("SELECT id FROM comptabilite.accounts WHERE account_number = :n"), {"n": numero}
    ).scalar_one()


def _cadre(db: Session, suffixe: str) -> tuple[uuid.UUID, Product, uuid.UUID]:
    """Rend (agency_id, produit crédit, tier ACTIF)."""
    agency_id = db.execute(
        text("SELECT id FROM parameters.agencies WHERE compte_caisse_id IS NOT NULL LIMIT 1")
    ).scalar_one()
    produit = Product(
        code=f"ENG{suffixe}",
        name="Crédit test engagement",
        compte_credit_membre_id=_cid(db, "202211"),
        compte_credit_client_id=_cid(db, "202221"),
        compte_produits_interets_id=_cid(db, "7021"),
        taux_bp=1200,
    )
    db.add(produit)
    db.flush()

    tier_id = db.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES (:n, 'individual', :a, 'actif') RETURNING id"
        ),
        {"n": f"M-ENG-{suffixe}", "a": agency_id},
    ).scalar_one()
    nationalite = db.execute(text("SELECT id FROM parameters.countries LIMIT 1")).scalar_one()
    db.execute(
        text(
            "INSERT INTO tiers.individual_profiles "
            "(tier_id, last_name, first_name, birth_date, gender, nationality_id) "
            "VALUES (:t, 'Test', 'Engagement', '1990-01-01', 'M', :nat)"
        ),
        {"t": tier_id, "nat": nationalite},
    )
    return agency_id, produit, tier_id


def _decaisser_credit(db: Session, agency_id: uuid.UUID, produit: Product, tier_id: uuid.UUID):
    demande = creer_demande(
        db, tier_id=tier_id, agency_id=agency_id, product_id=produit.id,
        montant_demande=200000, duree_echeances=6, objet="Test", par=None,
    )
    decider(
        db, demande, decision="approuve", montant_decide=200000, motif="OK", par=None
    )
    decaisser(db, demande, par=None, contexte=CONTEXTE_VIDE)
    db.flush()
    return demande


def _desactiver(db: Session, courant: UtilisateurCourant, tier_id: uuid.UUID):
    return executer_transition(db, courant, tier_id, "deactivate", CONTEXTE_VIDE, motif="test")


def _solder_toutes_les_echeances(db: Session, demande) -> None:
    while True:
        echeance = db.execute(
            select(Installment)
            .where(Installment.application_id == demande.id, Installment.status == "a_echoir")
            .order_by(Installment.numero)
            .limit(1)
        ).scalar_one_or_none()
        if echeance is None:
            return
        rembourser(db, demande, montant=echeance.total, par=None, contexte=CONTEXTE_VIDE)
        db.flush()


# --- Le crédit décaissé bloque -----------------------------------------------------------


def test_desactivation_refusee_si_credit_decaisse(db: Session) -> None:
    agency_id, produit, tier_id = _cadre(db, "A")
    demande = _decaisser_credit(db, agency_id, produit, tier_id)

    with pytest.raises(EngagementsOuvertsError) as exc:
        _desactiver(db, _acteur(db, agency_id), tier_id)

    (engagement,) = exc.value.engagements
    assert demande.application_number in engagement.libelle
    assert "décaissé" in engagement.libelle.lower() or "crédit" in engagement.libelle.lower()
    assert engagement.domaine == "credit"


def test_desactivation_autorisee_une_fois_le_credit_solde(db: Session) -> None:
    """La boucle complète : décaissé -> NON désactivable -> toutes les échéances payées ->
    désactivable. Comme Épargne (compte fermé) et Parts (capital remboursé)."""
    agency_id, produit, tier_id = _cadre(db, "E")
    demande = _decaisser_credit(db, agency_id, produit, tier_id)

    with pytest.raises(EngagementsOuvertsError):
        _desactiver(db, _acteur(db, agency_id), tier_id)

    _solder_toutes_les_echeances(db, demande)

    tier = _desactiver(db, _acteur(db, agency_id), tier_id)
    assert tier.status == "desactive"


def test_desactivation_refusee_si_echeance_partiellement_payee(db: Session) -> None:
    """CR5b, garde-fou (b) : une échéance PARTIELLEMENT payée doit continuer à bloquer la
    désactivation — son statut n'est ni 'a_echoir' ni 'paye'. UNE SEULE échéance (duree=1) :
    si la sélection ne teste que status = 'a_echoir', plus AUCUNE ligne ne correspond une fois
    le versement partiel posé (elle est 'partiellement_paye'), et la désactivation serait
    acceptée à tort — le point précis que la sélection status != 'paye' corrige."""
    agency_id, produit, tier_id = _cadre(db, "PART")
    demande = creer_demande(
        db, tier_id=tier_id, agency_id=agency_id, product_id=produit.id,
        montant_demande=100000, duree_echeances=1, objet="Test partiel", par=None,
    )
    decider(db, demande, decision="approuve", montant_decide=100000, motif="OK", par=None)
    decaisser(db, demande, par=None, contexte=CONTEXTE_VIDE)
    db.flush()

    seule = db.execute(
        select(Installment).where(Installment.application_id == demande.id)
    ).scalar_one()
    rembourser(db, demande, montant=seule.total // 2, par=None, contexte=CONTEXTE_VIDE)
    db.flush()

    with pytest.raises(EngagementsOuvertsError):
        _desactiver(db, _acteur(db, agency_id), tier_id)


def test_desactivation_autorisee_si_aucun_credit_decaisse(db: Session) -> None:
    agency_id, _produit, tier_id = _cadre(db, "B")
    # Demande créée mais jamais décidée ni décaissée : aucun engagement.

    tier = _desactiver(db, _acteur(db, agency_id), tier_id)

    assert tier.status == "desactive"


# --- Non-inertie : le vérificateur est bien branché à l'app -------------------------------


def test_le_verificateur_credit_est_enregistre_a_lassemblage_de_lapp() -> None:
    import app.main  # noqa: F401 — l'import a pour effet d'enregistrer les vérificateurs

    assert verifier_engagements_credit in verificateurs_enregistres()
