"""Chantier P1bis, lot 3 — bascule de la datation : preuve que les écritures postées portent la
date de la JOURNÉE COMPTABLE ouverte, JAMAIS la date système, quand les deux divergent.

JOURNEE_DATE (lundi 2024-03-04) est délibérément très loin dans le PASSÉ — avant toute donnée
réelle de cette base (2026) — choisie pour qu'aucune de ces opérations ne puisse jamais
coïncider avec la date système réelle. Chaque test ouvre SA PROPRE journée à cette date via le
VRAI service (`comptabilite.journee.ouvrir_journee`), jamais une ligne SQL à la main, et crée un
exercice comptable qui la couvre (le moteur `creer_brouillon` exige un exercice ouvert pour la
date posée).

Couvre, un par un, EXACTEMENT les points qui basculent dans ce lot : décaissement, remboursement,
opération d'épargne au guichet, contre-passation (OD), transfert de caisse, opération de parts —
et le refus propre quand aucune journée n'est ouverte du tout.
"""

import uuid
from collections.abc import Generator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.caisse.models import (
    CaisseParametres,
    CaissierPrincipal,
    NiveauCaisse,
    Poste,
    PosteAssignation,
)
from app.modules.caisse.service import ouvrir_session
from app.modules.caisse.transferts import initier_transfert, receptionner_transfert
from app.modules.comptabilite import ecritures, journee
from app.modules.comptabilite.ecritures import LigneSaisie
from app.modules.comptabilite.journee import AucuneJourneeOuverteError
from app.modules.comptabilite.models import Account, Exercice, Journal, JournalEntry
from app.modules.credit.decaissement import decaisser
from app.modules.credit.demandes import creer_demande, decider
from app.modules.credit.models import Product as CreditProduct
from app.modules.credit.remboursement import rembourser
from app.modules.epargne import guichet, service
from app.modules.epargne.models import Product as SavingsProduct
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.security.models import User
from app.modules.tiers.parts_operations import TYPE_SOUSCRIPTION_COMPTANT, poser_ecriture_parts

pytestmark = pytest.mark.integration

JOURNEE_DATE = date(2024, 3, 4)  # lundi, très loin de toute donnée réelle (base de dev : 2026)


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
def client(db: Session) -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _cid(db: Session, numero: str) -> uuid.UUID:
    return db.execute(
        text("SELECT id FROM comptabilite.accounts WHERE account_number = :n"), {"n": numero}
    ).scalar_one()


def _ouvrir_journee_passee(db: Session) -> None:
    """Ouvre la journée comptable à JOURNEE_DATE via le VRAI service — jamais une ligne SQL à la
    main. Appelé explicitement par chaque test (ce fichier n'a PAS de fixture autouse : le
    contrôle total de l'état « journée » est le point même de ces tests)."""
    journee.ouvrir_journee(db, JOURNEE_DATE, None)


def _exercice_couvrant(db: Session, code: str) -> Exercice:
    """Un exercice qui couvre JOURNEE_DATE — `creer_brouillon` l'exige pour toute date posée,
    et aucun exercice réel ne couvre 2024 (la base de dev vit sur 2026)."""
    exercice = Exercice(
        code=code,
        label=f"Exercice {code}",
        date_debut=date(JOURNEE_DATE.year, 1, 1),
        date_fin=date(JOURNEE_DATE.year, 12, 31),
    )
    db.add(exercice)
    db.flush()
    return exercice


def _tier(db: Session, agence: Agency, suffixe: str) -> uuid.UUID:
    tier_id = db.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES (:n, 'individual', :a, 'actif') RETURNING id"
        ),
        {"n": f"M-J3-{suffixe}", "a": agence.id},
    ).scalar_one()
    nat = db.execute(text("SELECT id FROM parameters.countries LIMIT 1")).scalar_one()
    db.execute(
        text(
            "INSERT INTO tiers.individual_profiles "
            "(tier_id, last_name, first_name, birth_date, gender, nationality_id) "
            "VALUES (:t, 'Kone', 'Fatou', '1985-01-01', 'F', :nat)"
        ),
        {"t": tier_id, "nat": nat},
    )
    return tier_id


def _poste_assigne_et_session_ouverte(
    db: Session, agence: Agency, user_id: uuid.UUID, code_poste: str = "01"
) -> None:
    """Poste + session de caisse OUVERTE pour `user_id` — exige une journée DÉJÀ ouverte
    (lot 2) : appelé seulement après `_ouvrir_journee_passee`."""
    poste = Poste(
        agency_id=agence.id, code=code_poste, libelle="Caisse test",
        compte_caisse_id=agence.compte_caisse_id,
    )
    db.add(poste)
    db.flush()
    db.add(PosteAssignation(poste_id=poste.id, user_id=user_id))
    db.flush()
    ouvrir_session(
        db,
        UtilisateurCourant(
            user_id=user_id, roles=(), permissions=frozenset(),
            primary_agency_id=agence.id, agency_id=agence.id, voit_tout=True,
        ),
        poste_id=poste.id, fonds_initial=0,
    )


# --- Décaissement et remboursement (même dossier, deux étapes) --------------------------------


def test_decaissement_et_remboursement_portent_la_date_de_la_journee(db: Session) -> None:
    _ouvrir_journee_passee(db)
    _exercice_couvrant(db, "J3CR")

    agence = Agency(code="J3CR", name="Agence J3CR", compte_caisse_id=_cid(db, "101111"))
    db.add(agence)
    db.flush()
    tier_id = _tier(db, agence, "CR")
    produit = CreditProduct(
        code="J3CR", name="Crédit test", compte_credit_membre_id=_cid(db, "202211"),
        compte_credit_client_id=_cid(db, "202221"),
        compte_produits_interets_id=_cid(db, "7021"), taux_bp=1200,
    )
    db.add(produit)
    db.flush()
    demande = creer_demande(
        db, tier_id=tier_id, agency_id=agence.id, product_id=produit.id,
        montant_demande=300_000, duree_echeances=12, objet="Test lot3", par=None,
    )
    decider(db, demande, decision="approuve", montant_decide=300_000, motif="OK", par=None)

    uid = db.execute(text("SELECT id FROM security.users LIMIT 1")).scalar_one()
    _poste_assigne_et_session_ouverte(db, agence, uid)

    decaisser(db, demande, par=uid)

    entree_decaissement = db.execute(
        select(JournalEntry).where(
            JournalEntry.description == f"Décaissement crédit {demande.application_number}"
        )
    ).scalar_one()
    assert entree_decaissement.entry_date == JOURNEE_DATE

    resultat = rembourser(db, demande, montant=10_000, par=uid)
    entree_remboursement = db.get(JournalEntry, resultat.entry_id)
    assert entree_remboursement is not None
    assert entree_remboursement.entry_date == JOURNEE_DATE


# --- Épargne (guichet) --------------------------------------------------------------------------


def test_depot_epargne_porte_la_date_de_la_journee(db: Session) -> None:
    _ouvrir_journee_passee(db)
    _exercice_couvrant(db, "J3EP")

    agence = Agency(code="J3EP", name="Agence J3EP", compte_caisse_id=_cid(db, "101111"))
    db.add(agence)
    db.flush()
    tier_id = _tier(db, agence, "EP")
    produit = SavingsProduct(
        code="J3EP", name="Épargne test", type="a_vue", compte_epargne_id=_cid(db, "251111")
    )
    db.add(produit)
    db.flush()
    compte = service.ouvrir_compte(
        db, tier_id=tier_id, product_id=produit.id, agency_id=agence.id, par=None
    )

    uid = db.execute(text("SELECT id FROM security.users LIMIT 1")).scalar_one()
    _poste_assigne_et_session_ouverte(db, agence, uid)

    courant = UtilisateurCourant(
        user_id=uid, roles=("CAISSIER",), permissions=frozenset({"epargne.operation.deposit"}),
        primary_agency_id=agence.id, agency_id=agence.id, voit_tout=False,
    )
    resultat = guichet.deposer(db, courant, compte.id, 10_000)

    entry = db.execute(
        select(JournalEntry).where(JournalEntry.entry_number == resultat.entry_number)
    ).scalar_one()
    assert entry.entry_date == JOURNEE_DATE


# --- OD / contre-passation ----------------------------------------------------------------------


def test_contre_passation_porte_la_date_de_la_journee(db: Session) -> None:
    exercice = _exercice_couvrant(db, "J3OD")
    journal = Journal(code="J3OD", name="Journal test", type="operations_diverses")
    compte_a = Account(
        account_number="6T910", name="Saisie A", account_class=6, normal_side="D", is_posting=True
    )
    compte_b = Account(
        account_number="6T911", name="Saisie B", account_class=6, normal_side="C", is_posting=True
    )
    db.add_all([journal, compte_a, compte_b])
    db.flush()

    # La pièce ORIGINALE n'a pas besoin d'être datée sur la journée — seule la
    # CONTRE-PASSATION (ce que ce lot bascule) doit porter la date de la journée ouverte.
    entry = ecritures.creer_brouillon(
        db,
        journal_id=journal.id,
        entry_date=date(exercice.date_debut.year, 1, 15),
        description="Pièce originale",
        lignes=[LigneSaisie(compte_a.id, "D", 1_000), LigneSaisie(compte_b.id, "C", 1_000)],
        par=None,
    )
    ecritures.valider(db, entry, par=None)

    _ouvrir_journee_passee(db)
    inverse = ecritures.contre_passer(db, entry, par=None)
    assert inverse.entry_date == JOURNEE_DATE


# --- Transfert de caisse (coffre -> principale, sans poste requis) ------------------------------


def test_transfert_porte_la_date_de_la_journee_a_lenvoi_et_a_la_reception(db: Session) -> None:
    _ouvrir_journee_passee(db)
    _exercice_couvrant(db, "J3TR")

    agence = Agency(code="J3TR", name="Agence J3TR")
    db.add(agence)
    db.flush()
    db.add(NiveauCaisse(agency_id=agence.id, niveau="coffre", compte_caisse_id=_cid(db, "101115")))
    db.add(
        NiveauCaisse(agency_id=agence.id, niveau="principale", compte_caisse_id=_cid(db, "101114"))
    )
    db.flush()
    config = db.execute(select(CaisseParametres).limit(1)).scalar_one()
    config.compte_transit_id = _cid(db, "1141")
    config.compte_ecart_transfert_manquant_id = _cid(db, "6099")
    config.compte_ecart_transfert_excedent_id = _cid(db, "7099")
    db.flush()

    s = uuid.uuid4().hex[:8]
    envoyeur = User(
        matricule=f"MAT-{s}E", email=f"{s}e@ex.com", username=f"e{s}",
        password_hash="x", last_name="E", first_name="J3", primary_agency_id=agence.id,
    )
    receveur = User(
        matricule=f"MAT-{s}R", email=f"{s}r@ex.com", username=f"r{s}",
        password_hash="x", last_name="R", first_name="J3", primary_agency_id=agence.id,
    )
    db.add_all([envoyeur, receveur])
    db.flush()

    permissions_transfert = frozenset(
        {"caisse.transfert.initier", "caisse.transfert.valider", "caisse.coffre.gerer"}
    )

    def _courant(user: User) -> UtilisateurCourant:
        return UtilisateurCourant(
            user_id=user.id, roles=(), permissions=permissions_transfert,
            primary_agency_id=agence.id, agency_id=agence.id, voit_tout=False,
        )

    transfert = initier_transfert(
        db, _courant(envoyeur), niveau_source="coffre", niveau_destination="principale",
        poste_id=None, montant_envoye=50_000, motif="Test lot3",
    )
    entree_envoi = db.get(JournalEntry, transfert.journal_entry_envoi_id)
    assert entree_envoi is not None
    assert entree_envoi.entry_date == JOURNEE_DATE

    db.add(CaissierPrincipal(agency_id=agence.id, user_id=receveur.id))
    db.flush()
    resultat = receptionner_transfert(
        db, _courant(receveur), transfert.id, montant_compte=50_000
    )
    entree_reception = db.get(JournalEntry, resultat.journal_entry_reception_id)
    assert entree_reception is not None
    assert entree_reception.entry_date == JOURNEE_DATE


# --- Opération de parts --------------------------------------------------------------------------


def test_operation_de_parts_porte_la_date_de_la_journee(db: Session) -> None:
    _ouvrir_journee_passee(db)
    _exercice_couvrant(db, "J3PA")

    agence = Agency(code="J3PA", name="Agence J3PA", compte_caisse_id=_cid(db, "101111"))
    db.add(agence)
    db.flush()

    entry = poser_ecriture_parts(
        db,
        TYPE_SOUSCRIPTION_COMPTANT,
        50_000,
        None,
        agency_id=agence.id,
        compte_liberees_id=_cid(db, "571111"),
        compte_non_liberees_id=_cid(db, "571121"),
        libelle="Test lot3 parts",
    )
    assert entry.entry_date == JOURNEE_DATE


# --- Refus propre : aucune journée ouverte du tout ----------------------------------------------


def test_operation_refusee_proprement_si_aucune_journee_nest_ouverte(db: Session) -> None:
    """Aucune journée ouverte (ni par ce test, ni par aucune fixture — ce fichier n'en a pas) :
    toute opération datée doit refuser PROPREMENT, jamais une date `None` qui se propage,
    jamais un succès silencieux sur la date système."""
    assert journee.journee_ouverte(db) is None  # sanity : vraiment aucune, pas un montage raté

    with pytest.raises(AucuneJourneeOuverteError):
        journee.date_comptable_obligatoire(db)

    # Et bout en bout, via un point réel : poser une écriture de parts sans journée ouverte.
    agence = Agency(code="J3NOJ", name="Agence J3NOJ", compte_caisse_id=_cid(db, "101111"))
    db.add(agence)
    db.flush()
    with pytest.raises(AucuneJourneeOuverteError):
        poser_ecriture_parts(
            db,
            TYPE_SOUSCRIPTION_COMPTANT,
            10_000,
            None,
            agency_id=agence.id,
            compte_liberees_id=_cid(db, "571111"),
            compte_non_liberees_id=_cid(db, "571121"),
            libelle="Test lot3 sans journée",
        )
