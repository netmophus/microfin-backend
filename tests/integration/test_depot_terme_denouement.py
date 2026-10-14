"""Dépôt à terme, lot 3 : dénouement à l'échéance et retrait anticipé avec pénalité.

Les journées comptables sont pilotées par le test (VRAI service, 2024 : très loin de la date du
poste) : une échéance se franchit en ouvrant la journée suivante, jamais en lisant l'horloge.
Ce fichier n'a PAS de fixture autouse : il ouvre lui-même ses journées.
"""

import uuid
from collections.abc import Generator
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import engine
from app.modules.caisse.models import Poste, PosteAssignation
from app.modules.caisse.service import AucuneSessionOuverteError, fermer_session, ouvrir_session
from app.modules.comptabilite import journee
from app.modules.comptabilite.models import Exercice
from app.modules.epargne.guichet import (
    CompteClotureError,
    EcheanceAtteinteError,
    EcheanceNonAtteinteError,
    apercu_denouement,
    denouer_depot_a_terme,
    fermer_compte,
    ouvrir_depot_a_terme,
    retirer_par_anticipation,
)
from app.modules.epargne.interets import PeriodeIncompatibleError, verser_interets
from app.modules.epargne.models import Product, SavingsAccount
from app.modules.epargne.rapprochement import rapprocher
from app.modules.epargne.service import DepotATermeBloqueError, verifier_coherence_solde
from app.modules.epargne.terme import interets_du_terme, penalite_sur_interets
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant

pytestmark = pytest.mark.integration

CAPITAL = 1_000_000
TAUX_BP = 500
PENALITE_BP = 5000
FONDS_CAISSE = 5_000_000

JOUR_OUVERTURE = date(2024, 3, 5)  # mardi
ECHEANCE = date(2024, 6, 5)  # 3 mois plus tard : 92 jours
JOUR_ANTICIPE = date(2024, 4, 16)  # 42 jours après l'ouverture
JOUR_TARDIF = date(2024, 6, 12)


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


def _cid(db: Session, numero: str) -> uuid.UUID:
    return db.execute(
        text("SELECT id FROM comptabilite.accounts WHERE account_number = :n"), {"n": numero}
    ).scalar_one()


class Cadre:
    """Une agence, un caissier avec son poste, un membre actif, un produit DAT (3 à 24 mois)."""

    def __init__(self, db: Session, suffixe: str) -> None:
        self.db = db
        db.add(
            Exercice(
                code=f"DT{suffixe}", label="Exercice 2024",
                date_debut=date(2024, 1, 1), date_fin=date(2024, 12, 31),
            )
        )
        self.agence = Agency(
            code=f"DT-{suffixe}", name="Agence", compte_caisse_id=_cid(db, "101111")
        )
        db.add(self.agence)
        db.flush()
        uid = db.execute(text("SELECT id FROM security.users LIMIT 1")).scalar_one()
        self.caissier = UtilisateurCourant(
            user_id=uid, roles=("CAISSIER",),
            permissions=frozenset(
                {"epargne.operation.deposit", "epargne.operation.withdraw", "epargne.dat.open"}
            ),
            primary_agency_id=self.agence.id, agency_id=self.agence.id, voit_tout=True,
        )
        self.poste = Poste(
            agency_id=self.agence.id, code="01", libelle="Caisse",
            compte_caisse_id=self.agence.compte_caisse_id,
        )
        db.add(self.poste)
        db.flush()
        db.add(PosteAssignation(poste_id=self.poste.id, user_id=uid))
        db.flush()
        self.tier_id = db.execute(
            text(
                "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
                "VALUES (:n, 'individual', :a, 'actif') RETURNING id"
            ),
            {"n": f"M-DT-{suffixe}", "a": self.agence.id},
        ).scalar_one()
        self.produit = Product(
            code=f"DT{suffixe}", name="Dépôt à terme", type="terme",
            compte_epargne_id=_cid(db, "252111"), compte_charge_interet_id=_cid(db, "60252"),
            taux_bp=TAUX_BP, base_jours=365, regle_arrondi="plus_proche",
            methode_calcul_solde="fin_periode", duree_min_mois=3, duree_max_mois=24,
            penalite_anticipee_bp=PENALITE_BP,
        )
        db.add(self.produit)
        db.flush()
        self.session_id: uuid.UUID | None = None
        self.encaisse = 0  # capitaux placés pendant la session en cours

    def ouvrir_journee(self, jour: date, *, session: bool = True) -> None:
        """Ouvre la journée `jour` (VRAI service) et, par défaut, la caisse du caissier."""
        journee.ouvrir_journee(self.db, jour, None)
        if session:
            self.session_id = ouvrir_session(
                self.db, self.caissier, poste_id=self.poste.id, fonds_initial=FONDS_CAISSE
            ).id

    def passer_a(self, jour: date, *, session: bool = True) -> None:
        """Ferme la caisse et clôture la journée en cours, puis ouvre `jour`."""
        assert self.session_id is not None
        fermer_session(
            self.db, self.caissier, self.session_id, montant_reel=FONDS_CAISSE + self.encaisse,
            motif="test de datation",
        )
        self.encaisse = 0
        journee.cloturer_journee(self.db, None)
        self.ouvrir_journee(jour, session=session)

    def placer(self, mois: int = 3, montant: int = CAPITAL) -> SavingsAccount:
        resultat = ouvrir_depot_a_terme(
            self.db, self.caissier, tier_id=self.tier_id, agency_id=self.agence.id,
            product_id=self.produit.id, term_months=mois, montant=montant,
        )
        self.encaisse += montant
        return resultat.compte

    def mouvements(self, compte: SavingsAccount) -> list[tuple[str, str, int, int]]:
        """Dans l'ordre métier (capital, intérêts, sortie) : tout le test tient dans UNE
        transaction, où `created_at` ne départage rien de fiable."""
        return [
            tuple(ligne)
            for ligne in self.db.execute(
                text(
                    "SELECT sens, operation_type, amount, balance_after FROM epargne.movements "
                    "WHERE account_id = :a ORDER BY CASE operation_type WHEN 'depot' THEN 0 "
                    "WHEN 'interet' THEN 1 ELSE 2 END"
                ),
                {"a": compte.id},
            ).all()
        ]

    def lignes_piece(self, numero: str) -> list[tuple[str, str, int]]:
        """(compte, sens, montant) des lignes de la pièce `numero`."""
        return [
            tuple(ligne)
            for ligne in self.db.execute(
                text(
                    "SELECT a.account_number, l.side, l.amount "
                    "FROM comptabilite.journal_lines l "
                    "JOIN comptabilite.journal_entries e ON e.id = l.entry_id "
                    "JOIN comptabilite.accounts a ON a.id = l.account_id "
                    "WHERE e.entry_number = :n ORDER BY l.line_number"
                ),
                {"n": numero},
            ).all()
        ]


def _interets_echeance() -> int:
    return interets_du_terme(CAPITAL, TAUX_BP, (ECHEANCE - JOUR_OUVERTURE).days, 365, "plus_proche")


def _interets_courus(jour: date) -> int:
    return interets_du_terme(CAPITAL, TAUX_BP, (jour - JOUR_OUVERTURE).days, 365, "plus_proche")


# --- Dénouement à l'échéance ---------------------------------------------------------------


def test_le_denouement_paie_capital_et_interets_de_toute_la_duree(db: Session) -> None:
    c = Cadre(db, "N1")
    c.ouvrir_journee(JOUR_OUVERTURE)
    compte = c.placer()
    assert compte.maturity_date == ECHEANCE
    c.passer_a(ECHEANCE)
    interets = _interets_echeance()
    assert interets > 0

    recu = denouer_depot_a_terme(db, c.caissier, compte.id)

    assert recu.mode == "echeance"
    assert (recu.capital, recu.interets_courus, recu.penalite) == (CAPITAL, interets, 0)
    assert recu.total_paye == CAPITAL + interets
    assert recu.entry_number is not None
    assert str(CAPITAL + interets).replace(",", "") in recu.message.replace(" ", "")
    db.refresh(compte)
    assert (compte.status, compte.balance) == ("cloture", 0)
    assert c.mouvements(compte) == [
        ("credit", "depot", CAPITAL, CAPITAL),
        ("credit", "interet", interets, CAPITAL + interets),
        ("debit", "denouement", CAPITAL + interets, 0),
    ]
    # Sortie de caisse : D 252111 / C caisse pour le total ; charge : D 60252 / C 252111.
    lignes = c.lignes_piece(recu.entry_number)
    assert lignes == [("252111", "D", CAPITAL + interets), ("101111", "C", CAPITAL + interets)]
    archive = db.execute(
        text("SELECT montant, methode, jours, detail FROM epargne.interet_calculs "
             "WHERE account_id = :a AND periode = 'DENOUEMENT'"),
        {"a": compte.id},
    ).one()
    assert (archive.montant, archive.methode, archive.jours) == (interets, "terme", 92)
    assert archive.detail["mode"] == "echeance"
    assert verifier_coherence_solde(db, compte.id).coherent
    assert rapprocher(db, _cid(db, "252111")).concordant


def test_un_denouement_tardif_ne_rapporte_rien_de_plus(db: Session) -> None:
    c = Cadre(db, "N2")
    c.ouvrir_journee(JOUR_OUVERTURE)
    compte = c.placer()
    c.passer_a(JOUR_TARDIF)

    recu = denouer_depot_a_terme(db, c.caissier, compte.id)

    assert recu.interets_courus == _interets_echeance()  # arrêtés à l'échéance, pas au 12/06
    assert recu.total_paye == CAPITAL + _interets_echeance()


def test_pas_de_denouement_avant_l_echeance(db: Session) -> None:
    c = Cadre(db, "N3")
    c.ouvrir_journee(JOUR_OUVERTURE)
    compte = c.placer()
    c.passer_a(JOUR_ANTICIPE)

    with pytest.raises(EcheanceNonAtteinteError, match="n'arrive à échéance que le 05/06/2024"):
        denouer_depot_a_terme(db, c.caissier, compte.id)

    db.refresh(compte)
    assert (compte.status, compte.balance) == ("actif", CAPITAL)


def test_un_depot_solde_ne_se_denoue_pas_deux_fois(db: Session) -> None:
    c = Cadre(db, "N4")
    c.ouvrir_journee(JOUR_OUVERTURE)
    compte = c.placer()
    c.passer_a(ECHEANCE)
    denouer_depot_a_terme(db, c.caissier, compte.id)

    with pytest.raises(CompteClotureError):
        denouer_depot_a_terme(db, c.caissier, compte.id)
    assert len(c.mouvements(compte)) == 3  # aucun mouvement de plus


def test_le_denouement_exige_une_session_de_caisse(db: Session) -> None:
    c = Cadre(db, "N5")
    c.ouvrir_journee(JOUR_OUVERTURE)
    compte = c.placer()
    c.passer_a(ECHEANCE, session=False)

    with pytest.raises(AucuneSessionOuverteError):
        denouer_depot_a_terme(db, c.caissier, compte.id)
    db.refresh(compte)
    assert compte.balance == CAPITAL


# --- Retrait anticipé -----------------------------------------------------------------------


def test_le_retrait_anticipe_retient_la_penalite_sur_les_interets_courus(db: Session) -> None:
    c = Cadre(db, "A1")
    c.ouvrir_journee(JOUR_OUVERTURE)
    compte = c.placer()
    c.passer_a(JOUR_ANTICIPE)
    courus = _interets_courus(JOUR_ANTICIPE)
    penalite = penalite_sur_interets(courus, PENALITE_BP)
    assert 0 < penalite < courus

    recu = retirer_par_anticipation(db, c.caissier, compte.id)

    assert recu.mode == "anticipe"
    assert (recu.interets_courus, recu.penalite) == (courus, penalite)
    assert recu.interets_nets == courus - penalite
    assert recu.total_paye == CAPITAL + courus - penalite
    assert recu.total_paye >= CAPITAL  # le capital n'est jamais entamé
    assert "moins la pénalité de" in recu.message
    db.refresh(compte)
    assert (compte.status, compte.balance) == ("cloture", 0)
    assert c.mouvements(compte)[-1] == ("debit", "retrait_anticipe", recu.total_paye, 0)
    assert c.lignes_piece(recu.entry_number or "") == [
        ("252111", "D", recu.total_paye), ("101111", "C", recu.total_paye),
    ]
    archive = db.execute(
        text("SELECT montant, jours, detail FROM epargne.interet_calculs "
             "WHERE account_id = :a AND periode = 'DENOUEMENT'"),
        {"a": compte.id},
    ).one()
    assert (archive.montant, archive.jours) == (courus - penalite, 42)
    assert archive.detail["mode"] == "anticipe"
    assert (archive.detail["penalite"], archive.detail["interets_courus"]) == (penalite, courus)
    assert rapprocher(db, _cid(db, "252111")).concordant


def test_une_penalite_totale_rend_le_capital_et_rien_d_autre(db: Session) -> None:
    c = Cadre(db, "A2")
    c.produit.penalite_anticipee_bp = 10_000
    db.flush()
    c.ouvrir_journee(JOUR_OUVERTURE)
    compte = c.placer()
    c.passer_a(JOUR_ANTICIPE)

    recu = retirer_par_anticipation(db, c.caissier, compte.id)

    assert (recu.interets_nets, recu.total_paye) == (0, CAPITAL)
    assert [m[1] for m in c.mouvements(compte)] == ["depot", "retrait_anticipe"]  # pas d'intérêt


def test_le_retrait_le_jour_meme_rend_le_capital(db: Session) -> None:
    c = Cadre(db, "A3")
    c.ouvrir_journee(JOUR_OUVERTURE)
    compte = c.placer()

    recu = retirer_par_anticipation(db, c.caissier, compte.id)

    assert (recu.interets_courus, recu.total_paye) == (0, CAPITAL)


def test_pas_de_retrait_anticipe_a_partir_de_l_echeance(db: Session) -> None:
    c = Cadre(db, "A4")
    c.ouvrir_journee(JOUR_OUVERTURE)
    compte = c.placer()
    c.passer_a(ECHEANCE)

    with pytest.raises(EcheanceAtteinteError, match="dénouement à l'échéance, sans pénalité"):
        retirer_par_anticipation(db, c.caissier, compte.id)
    db.refresh(compte)
    assert compte.balance == CAPITAL


# --- Conditions figées, aperçu, gardes ---------------------------------------------------------


def test_les_conditions_du_contrat_priment_sur_le_produit(db: Session) -> None:
    c = Cadre(db, "F1")
    c.ouvrir_journee(JOUR_OUVERTURE)
    compte = c.placer()
    # Le produit change après l'ouverture : taux, pénalité, base et arrondi. Rien ne bouge.
    c.produit.taux_bp = 0
    c.produit.penalite_anticipee_bp = 0
    c.produit.base_jours = 360
    c.produit.regle_arrondi = "plancher"
    db.flush()
    c.passer_a(JOUR_ANTICIPE)
    courus = _interets_courus(JOUR_ANTICIPE)

    recu = retirer_par_anticipation(db, c.caissier, compte.id)

    assert recu.interets_courus == courus
    assert recu.penalite == penalite_sur_interets(courus, PENALITE_BP)


def test_l_apercu_annonce_exactement_ce_que_l_execution_paie(db: Session) -> None:
    c = Cadre(db, "F2")
    c.ouvrir_journee(JOUR_OUVERTURE)
    compte = c.placer()
    c.passer_a(JOUR_ANTICIPE)

    apercu = apercu_denouement(db, c.caissier, compte.id)
    db.refresh(compte)
    assert compte.status == "actif"  # lecture seule : rien n'a bougé
    recu = retirer_par_anticipation(db, c.caissier, compte.id)

    assert apercu.mode == recu.mode == "anticipe"
    assert apercu.total_paye == recu.total_paye
    assert apercu.message == recu.message


def test_un_depot_a_terme_ne_se_ferme_pas_comme_un_compte_ordinaire(db: Session) -> None:
    c = Cadre(db, "G1")
    c.ouvrir_journee(JOUR_OUVERTURE)
    compte = c.placer()

    with pytest.raises(DepotATermeBloqueError, match="dénouement à l'échéance"):
        fermer_compte(db, c.caissier, compte.id)
    db.refresh(compte)
    assert (compte.status, compte.balance) == ("actif", CAPITAL)


def test_le_versement_periodique_d_interets_refuse_un_produit_a_terme(db: Session) -> None:
    c = Cadre(db, "G2")
    c.ouvrir_journee(JOUR_OUVERTURE)
    c.placer()

    with pytest.raises(PeriodeIncompatibleError, match="payés à son échéance"):
        verser_interets(
            db, produit_id=c.produit.id, periode="2024-02",
            debut=date(2024, 2, 1), fin=date(2024, 2, 29),
        )
