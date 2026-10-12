"""Intérêts d'épargne : les mouvements sont datés de la journée comptable de leur pièce.

Avant ce lot, le moteur datait les mouvements par `created_at::date` (date de SAISIE). Un dépôt
saisi après la fin de la période pour une journée DE la période en était exclu, et un intérêt posté
à la fin de la période N mais versé en N+1 comptait comme un point de N+1.

Ce fichier n'a PAS de fixture autouse : il pilote lui-même les journées comptables (2024, très
loin dans le passé, donc toute saisie réelle — `created_at` — est postérieure à la période).
"""

import uuid
from collections.abc import Generator
from datetime import date
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import engine
from app.modules.caisse.models import Poste, PosteAssignation
from app.modules.caisse.service import fermer_session, ouvrir_session
from app.modules.comptabilite import journee
from app.modules.comptabilite.models import Exercice
from app.modules.epargne import interets, service
from app.modules.epargne.guichet import deposer
from app.modules.epargne.interets import (
    MouvementSansPieceError,
    RapportInterets,
    _trajectoire,
    previsualiser_interets,
    verser_interets,
)
from app.modules.epargne.models import Product, SavingsAccount
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant

pytestmark = pytest.mark.integration

JOUR_SOLDE_INITIAL = date(2024, 2, 28)  # mercredi : le solde d'entrée de la période
JOUR_DEPOT = date(2024, 3, 29)  # vendredi : le dépôt dû, saisi bien après la période
MARS = (date(2024, 3, 1), date(2024, 3, 31))  # 31 jours
AVRIL = (date(2024, 4, 1), date(2024, 4, 30))


@pytest.fixture(autouse=True)
def _mars_et_avril_clos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Le versement ne porte que sur une période close : la journée de référence est fixée
    après la période versée ici, sans dépendre de la date du jour."""
    monkeypatch.setattr(interets, "journee_de_reference", lambda db: date(2024, 6, 15))


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
    """Une agence, un caissier, un poste, et des comptes (un par méthode de calcul)."""

    def __init__(self, db: Session, suffixe: str, methodes: list[str]) -> None:
        db.add(
            Exercice(
                code=f"DC{suffixe}", label="Exercice 2024",
                date_debut=date(2024, 1, 1), date_fin=date(2024, 12, 31),
            )
        )
        self.agence = Agency(
            code=f"DC-{suffixe}", name="Agence", compte_caisse_id=_cid(db, "101111")
        )
        db.add(self.agence)
        db.flush()
        uid = db.execute(text("SELECT id FROM security.users LIMIT 1")).scalar_one()
        self.caissier = UtilisateurCourant(
            user_id=uid, roles=("CAISSIER",),
            permissions=frozenset({"epargne.operation.deposit"}),
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
        self.comptes: dict[str, SavingsAccount] = {}
        for i, methode in enumerate(methodes):
            self.comptes[methode] = self._compte(db, f"{suffixe}{i}", methode)

    def _compte(self, db: Session, code: str, methode: str) -> SavingsAccount:
        # 3 % l'an sur 365 jours, arrondi au plus proche.
        produit = Product(
            code=f"PD{code}", name="Épargne", type="a_vue",
            compte_epargne_id=_cid(db, "251111"), compte_charge_interet_id=_cid(db, "602511"),
            taux_bp=300, base_jours=365, methode_calcul_solde=methode,
            periodicite="mensuelle",
        )
        db.add(produit)
        db.flush()
        tier_id = db.execute(
            text(
                "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
                "VALUES (:n, 'individual', :a, 'actif') RETURNING id"
            ),
            {"n": f"M-DC-{code}", "a": self.agence.id},
        ).scalar_one()
        return service.ouvrir_compte(
            db, tier_id=tier_id, product_id=produit.id, agency_id=self.agence.id, par=None
        )

    def journee_de_depots(
        self, db: Session, jour: date, depots: dict[str, int], *, cloturer: bool = True
    ) -> None:
        """Ouvre la journée `jour` (VRAI service), y dépose, ferme la caisse, clôture."""
        journee.ouvrir_journee(db, jour, None)
        session = ouvrir_session(db, self.caissier, poste_id=self.poste.id, fonds_initial=0)
        for methode, montant in depots.items():
            deposer(db, self.caissier, self.comptes[methode].id, montant)
        fermer_session(
            db, self.caissier, session.id,
            montant_reel=sum(depots.values()), motif="test de datation",
        )
        if cloturer:
            journee.cloturer_journee(db, None)


def _verser(db: Session, c: Cadre, *, periode: str, debut: date, fin: date) -> RapportInterets:
    """Verse la période à TOUS les produits du cadre (un produit par méthode de calcul) : les
    intérêts se versent produit par produit, ce cumul garde les tests lisibles."""
    total = RapportInterets()
    for compte in c.comptes.values():
        rapport = verser_interets(
            db, produit_id=compte.product_id, periode=periode, debut=debut, fin=fin
        )
        total.traites += rapport.traites
        total.ignores += rapport.ignores
        total.credites += rapport.credites
        total.total += rapport.total
        total.en_erreur.extend(rapport.en_erreur)
    return total


def _archive(db: Session, compte: SavingsAccount, periode: str) -> tuple[int, int]:
    """(base_solde, montant) du calcul archivé pour ce compte et cette période."""
    return db.execute(
        text(
            "SELECT base_solde, montant FROM epargne.interet_calculs "
            "WHERE account_id = :a AND periode = :p"
        ),
        {"a": compte.id, "p": periode},
    ).one()


def test_depot_saisi_apres_la_periode_compte_depuis_sa_date_comptable(db: Session) -> None:
    methodes = ["min_periode", "moyen_quotidien", "fin_periode"]
    c = Cadre(db, "A", methodes)
    c.journee_de_depots(db, JOUR_SOLDE_INITIAL, dict.fromkeys(methodes, 100_000))
    # Dépôt de 50 000 dû le vendredi 29/03 ; sa saisie (created_at) est en 2026, très après mars.
    c.journee_de_depots(db, JOUR_DEPOT, dict.fromkeys(methodes, 50_000), cloturer=False)

    _verser(db, c, periode="2024-03", debut=MARS[0], fin=MARS[1])

    assert _archive(db, c.comptes["min_periode"], "2024-03") == (100_000, 255)
    assert _archive(db, c.comptes["moyen_quotidien"], "2024-03") == (104_838, 267)
    assert _archive(db, c.comptes["fin_periode"], "2024-03") == (150_000, 382)


def test_interet_poste_a_la_fin_de_la_periode_est_compte_a_cette_date(db: Session) -> None:
    c = Cadre(db, "B", ["fin_periode"])
    c.journee_de_depots(db, JOUR_SOLDE_INITIAL, {"fin_periode": 100_000})
    c.journee_de_depots(db, JOUR_DEPOT, {"fin_periode": 50_000}, cloturer=False)
    compte = c.comptes["fin_periode"]

    # L'intérêt de mars est posté au 31/03 (entry_date) mais versé — saisi — bien après.
    _verser(db, c, periode="2024-03", debut=MARS[0], fin=MARS[1])
    interet_mars = _archive(db, compte, "2024-03")[1]
    assert interet_mars == 382

    _verser(db, c, periode="2024-04", debut=AVRIL[0], fin=AVRIL[1])

    # Avril part d'un solde qui CONTIENT déjà l'intérêt de mars (daté du 31/03), pas d'un solde
    # qui l'ignorerait parce que sa saisie tombe « après » la période.
    assert _archive(db, compte, "2024-04")[0] == 150_000 + interet_mars


def test_la_base_refuse_un_mouvement_sans_piece(db: Session) -> None:
    c = Cadre(db, "N", ["fin_periode"])
    compte = c.comptes["fin_periode"]

    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(
            text(
                "INSERT INTO epargne.movements "
                "(account_id, sens, amount, balance_after, operation_type, date_valeur) "
                "VALUES (:a, 'credit', 1, 1, 'depot', CURRENT_DATE)"
            ),
            {"a": compte.id},
        )


def test_la_garde_de_la_trajectoire_refuse_un_mouvement_sans_piece() -> None:
    """Défense en profondeur : la colonne est NOT NULL, mais le calcul ne devine jamais une date
    si une ligne sans date lui parvenait quand même (base restaurée, contrainte retirée…)."""
    base = MagicMock()
    base.execute.return_value.all.return_value = [(None, 100)]

    with pytest.raises(MouvementSansPieceError, match="EP-X : calcul impossible"):
        _trajectoire(base, uuid.uuid4(), "EP-X", MARS[0], MARS[1])


def test_un_compte_en_anomalie_est_signale_sans_bloquer_les_autres(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = Cadre(db, "C", ["min_periode", "fin_periode"])
    c.journee_de_depots(
        db, JOUR_DEPOT, {"min_periode": 100_000, "fin_periode": 100_000}, cloturer=False
    )
    fautif, sain = c.comptes["min_periode"], c.comptes["fin_periode"]
    db.commit()  # le rollback du compte en erreur ne doit pas emporter la mise en place

    reel = interets._trajectoire

    def trajectoire(base: Session, account_id: uuid.UUID, numero: str, debut: date, fin: date):
        if account_id == fautif.id:
            raise MouvementSansPieceError(
                f"mouvement sans pièce comptable sur le compte {numero} : calcul impossible"
            )
        return reel(base, account_id, numero, debut, fin)

    monkeypatch.setattr(interets, "_trajectoire", trajectoire)

    apercu_erreurs = [
        erreur
        for compte in c.comptes.values()
        for erreur in previsualiser_interets(
            db, produit_id=compte.product_id, periode="2024-03", debut=MARS[0], fin=MARS[1]
        ).en_erreur
    ]
    rapport = _verser(db, c, periode="2024-03", debut=MARS[0], fin=MARS[1])

    message = (
        f"mouvement sans pièce comptable sur le compte {fautif.account_number} : "
        "calcul impossible"
    )
    assert apercu_erreurs == [message]
    assert rapport.en_erreur == [message]
    assert rapport.traites == 1  # le compte sain a été traité
    nombre = db.execute(
        text("SELECT count(*) FROM epargne.interet_calculs WHERE account_id = :a"),
        {"a": fautif.id},
    ).scalar_one()
    assert nombre == 0  # le compte en erreur n'a rien reçu, rien archivé
    assert _archive(db, sain, "2024-03")[0] == 100_000


def test_la_date_de_valeur_d_un_depot_est_la_date_comptable_de_sa_piece(db: Session) -> None:
    c = Cadre(db, "V", ["fin_periode"])
    c.journee_de_depots(db, JOUR_DEPOT, {"fin_periode": 50_000}, cloturer=False)

    date_valeur, entry_date, saisie = db.execute(
        text(
            "SELECT m.date_valeur, je.entry_date, m.created_at::date "
            "FROM epargne.movements m JOIN comptabilite.journal_entries je "
            "ON je.id = m.journal_entry_id WHERE m.account_id = :a"
        ),
        {"a": c.comptes["fin_periode"].id},
    ).one()

    assert date_valeur == entry_date == JOUR_DEPOT
    assert saisie != JOUR_DEPOT  # la saisie est réelle (2026) : ce n'est pas elle qu'on a retenue
