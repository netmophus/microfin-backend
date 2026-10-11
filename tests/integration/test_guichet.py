"""Opérations de guichet (E3) : dépôt / retrait — chaque garde-fou VU MORDRE.

Garde-fous métier (fixture transactionnelle) : plancher, découvert, gel si membre suspendu,
compte fermé, montant invalide, cloisonnement, rapprochement de bout en bout.
Concurrence et atomicité (vraies connexions, déterministe) dans test_guichet_concurrence.py.
"""

import uuid
from collections.abc import Generator
from dataclasses import dataclass

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import engine
from app.modules.caisse.models import CaisseSession, Poste, PosteAssignation
from app.modules.caisse.service import ouvrir_session
from app.modules.comptabilite import journee
from app.modules.epargne import service
from app.modules.epargne.guichet import (
    CompteClotureError,
    CompteIntrouvableError,
    MontantInvalideError,
    OperationGeleeError,
    PremierDepotInsuffisantError,
    SoldeInsuffisantError,
    deposer,
    fermer_compte,
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


@pytest.fixture(autouse=True)
def _journee_ouverte(request: pytest.FixtureRequest) -> None:
    """Chantier P1bis lot 2 : `ouvrir_session` exige désormais une journée comptable ouverte
    — ouverte ici, via le VRAI service (jamais une ligne SQL à la main), pour les tests qui
    utilisent la fixture `db` partagée (directement ou via `client`). PAS pour les tests qui
    gèrent leurs propres connexions `SessionLocal()` (ex. simulation de panne/interruption,
    `test_fermeture.py::test_fermeture_interrompue_ne_laisse_rien`) : forcer `db` pour eux ouvre
    une seconde connexion qui GARDE le verrou consultatif du chaînage d'audit pendant toute la
    durée du test (la transaction du fixture `db` ne se termine qu'à son teardown) — si cette
    seconde connexion propre au test a elle-même besoin de ce verrou (via `ecrire_audit`), c'est
    un INTERBLOCAGE certain entre les deux connexions du même test. `request.fixturenames` dit
    si `db` est déjà dans la fermeture de fixtures du test SANS la créer elle-même."""
    if "db" not in request.fixturenames:
        return
    db = request.getfixturevalue("db")
    journee.ouvrir_journee(db, journee.prochaine_date_ouvree(db), None)


def _compte_id(db: Session, numero: str) -> uuid.UUID:
    return db.execute(
        text("SELECT id FROM comptabilite.accounts WHERE account_number = :n"), {"n": numero}
    ).scalar_one()


def _caissier(db: Session, agency_id: uuid.UUID) -> UtilisateurCourant:
    """Idempotent : n'ouvre qu'UNE session (Bloc C4) pour cet uid — la réutilise sinon (même
    acteur ré-appelé dans un scénario hors périmètre, `test_compte_hors_perimetre_...`)."""
    uid = db.execute(text("SELECT id FROM security.users LIMIT 1")).scalar_one()
    courant = UtilisateurCourant(
        user_id=uid,
        roles=("CAISSIER",),
        permissions=frozenset({"epargne.operation.deposit", "epargne.operation.withdraw"}),
        primary_agency_id=agency_id,
        agency_id=agency_id,
        voit_tout=False,  # cloisonné à son agence
    )
    deja_ouverte = db.execute(
        select(CaisseSession.id).where(
            CaisseSession.caissier_id == uid, CaisseSession.status == "ouverte"
        )
    ).first()
    if deja_ouverte is None:
        compte_caisse_id = db.execute(
            select(Agency.compte_caisse_id).where(Agency.id == agency_id)
        ).scalar_one()
        poste = Poste(
            agency_id=agency_id, code="01", libelle="Caisse principale",
            compte_caisse_id=compte_caisse_id,
        )
        db.add(poste)
        db.flush()
        db.add(PosteAssignation(poste_id=poste.id, user_id=uid))
        db.flush()
        ouvrir_session(db, courant, poste_id=poste.id, fonds_initial=0)
    return courant


@dataclass
class Cadre:
    agence: Agency
    produit: Product
    compte: SavingsAccount
    caissier: UtilisateurCourant


def _cadre(
    db: Session,
    suffixe: str,
    *,
    min_balance: int = 0,
    decouvert: int = 0,
    depot_initial_minimum: int = 0,
) -> Cadre:
    agence = Agency(code=f"AGG-{suffixe}", name="Agence", compte_caisse_id=_compte_id(db, "101111"))
    produit = Product(
        code=f"PG{suffixe}", name="Épargne", type="a_vue",
        compte_epargne_id=_compte_id(db, "251111"), min_balance=min_balance,
        decouvert_autorise=decouvert, depot_initial_minimum=depot_initial_minimum,
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


def test_la_base_refuse_un_decouvert_non_nul_sur_epargne(db: Session) -> None:
    """Conformité SFD (migration 0049) : un SFD ne tient pas de comptes courants, l'épargne
    n'autorise JAMAIS de découvert. `decouvert_autorise` reste en base (mécanique du calcul du
    disponible dans guichet.py/prelevement.py, inchangée) mais un CHECK verrouille sa valeur à
    0 — y compris pour une insertion directe en base qui contournerait l'API (extra="forbid"
    sur les schémas de création/modification, voir schemas.py). Avant 0049, cette même
    insertion réussissait (voir historique du test) ; c'était précisément le trou à combler."""
    produit = Product(
        code="PDEC", name="Épargne (tentative découvert)", type="a_vue",
        compte_epargne_id=_compte_id(db, "251111"), decouvert_autorise=2000,
    )
    db.add(produit)
    with pytest.raises(IntegrityError, match="decouvert_nul_epargne"):
        db.flush()


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


# --- Paramètres produit : dépôt initial minimum et solde plancher ---------------------------


def _nb_mouvements_et_pieces(db: Session, compte_id: uuid.UUID) -> tuple[int, int]:
    mouvements = db.execute(
        text("SELECT count(*) FROM epargne.movements WHERE account_id = :a"), {"a": compte_id}
    ).scalar_one()
    pieces = db.execute(text("SELECT count(*) FROM comptabilite.journal_entries")).scalar_one()
    return mouvements, pieces


def test_premier_depot_sous_le_minimum_refuse_et_rien_ecrit(db: Session) -> None:
    c = _cadre(db, "D1", depot_initial_minimum=5000)
    avant = _nb_mouvements_et_pieces(db, c.compte.id)

    with pytest.raises(PremierDepotInsuffisantError) as erreur:
        deposer(db, c.caissier, c.compte.id, 4999)

    assert str(erreur.value) == (
        "Premier dépôt insuffisant : ce produit exige au moins 5 000 F à l'ouverture."
    )
    db.refresh(c.compte)
    assert c.compte.balance == 0
    assert _nb_mouvements_et_pieces(db, c.compte.id) == avant


def test_premier_depot_egal_au_minimum_accepte(db: Session) -> None:
    c = _cadre(db, "D2", depot_initial_minimum=5000)

    assert deposer(db, c.caissier, c.compte.id, 5000).nouveau_solde == 5000


def test_second_depot_sous_le_minimum_accepte(db: Session) -> None:
    c = _cadre(db, "D3", depot_initial_minimum=5000)
    deposer(db, c.caissier, c.compte.id, 5000)

    assert deposer(db, c.caissier, c.compte.id, 100).nouveau_solde == 5100  # règle du 1er seul


def test_le_minimum_ne_revient_pas_quand_le_solde_repasse_a_zero(db: Session) -> None:
    """« Jamais mouvementé » se lit dans l'historique, pas dans le solde : un compte vidé puis
    rechargé n'est plus un compte neuf."""
    c = _cadre(db, "D4", depot_initial_minimum=5000)
    deposer(db, c.caissier, c.compte.id, 5000)
    retirer(db, c.caissier, c.compte.id, 5000)

    assert deposer(db, c.caissier, c.compte.id, 100).nouveau_solde == 100


def test_retrait_qui_franchit_le_plancher_refuse_avec_le_message_du_produit(db: Session) -> None:
    c = _cadre(db, "PL1", min_balance=2000)
    deposer(db, c.caissier, c.compte.id, 10000)
    avant = _nb_mouvements_et_pieces(db, c.compte.id)

    with pytest.raises(SoldeInsuffisantError) as erreur:
        retirer(db, c.caissier, c.compte.id, 8001)  # laisserait 1 999 F < 2 000 F

    assert str(erreur.value) == (
        "Retrait refusé : le solde ne peut pas descendre sous 2 000 F sur ce produit."
    )
    assert _nb_mouvements_et_pieces(db, c.compte.id) == avant


def test_retrait_qui_laisse_exactement_le_plancher_accepte(db: Session) -> None:
    c = _cadre(db, "PL2", min_balance=2000)
    deposer(db, c.caissier, c.compte.id, 10000)

    assert retirer(db, c.caissier, c.compte.id, 8000).nouveau_solde == 2000


def test_la_cloture_n_est_pas_soumise_au_plancher(db: Session) -> None:
    c = _cadre(db, "PL3", min_balance=2000)
    deposer(db, c.caissier, c.compte.id, 10000)

    fermer_compte(db, c.caissier, c.compte.id, motif="départ du membre")

    db.refresh(c.compte)
    assert (c.compte.status, c.compte.balance) == ("cloture", 0)


def test_produit_a_zero_zero_se_comporte_comme_avant(db: Session) -> None:
    c = _cadre(db, "Z0")  # depot_initial_minimum = 0, min_balance = 0

    assert deposer(db, c.caissier, c.compte.id, 1).nouveau_solde == 1  # 1 F suffit
    assert retirer(db, c.caissier, c.compte.id, 1).nouveau_solde == 0  # jusqu'à zéro
    with pytest.raises(SoldeInsuffisantError, match="Retrait impossible"):
        retirer(db, c.caissier, c.compte.id, 1)
