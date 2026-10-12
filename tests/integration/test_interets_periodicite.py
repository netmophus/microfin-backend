"""Périodicité des produits d'épargne appliquée au versement des intérêts.

`products.periodicite` (mensuelle / trimestrielle / annuelle) était stockée mais jamais lue : le
versement acceptait n'importe quelle période. Désormais la période est DÉRIVÉE de la périodicité
(mois, trimestre ou année civile) et le versement refuse tout le reste.

Aucune date du jour ici : les bornes se calculent d'une date donnée (la journée comptable).
"""

import uuid
from collections.abc import Generator
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import engine
from app.modules.epargne import interets
from app.modules.epargne.interets import (
    PeriodeIncompatibleError,
    ProduitInconnuError,
    derniere_periode_close,
    periode_attendue,
    previsualiser_interets,
    verifier_periode_close,
    verser_interets,
)
from app.modules.epargne.models import Product

# --- Dérivation PURE ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("periodicite", "reference", "attendu"),
    [
        ("mensuelle", date(2026, 3, 17), ("2026-03", date(2026, 3, 1), date(2026, 3, 31))),
        ("mensuelle", date(2026, 3, 1), ("2026-03", date(2026, 3, 1), date(2026, 3, 31))),
        ("mensuelle", date(2026, 3, 31), ("2026-03", date(2026, 3, 1), date(2026, 3, 31))),
        ("mensuelle", date(2024, 2, 10), ("2024-02", date(2024, 2, 1), date(2024, 2, 29))),
        ("mensuelle", date(2026, 2, 10), ("2026-02", date(2026, 2, 1), date(2026, 2, 28))),
        ("trimestrielle", date(2026, 1, 1), ("2026-T1", date(2026, 1, 1), date(2026, 3, 31))),
        ("trimestrielle", date(2026, 5, 15), ("2026-T2", date(2026, 4, 1), date(2026, 6, 30))),
        ("trimestrielle", date(2026, 9, 30), ("2026-T3", date(2026, 7, 1), date(2026, 9, 30))),
        ("trimestrielle", date(2026, 12, 31), ("2026-T4", date(2026, 10, 1), date(2026, 12, 31))),
        ("annuelle", date(2026, 7, 4), ("2026", date(2026, 1, 1), date(2026, 12, 31))),
    ],
)
def test_periode_attendue_est_la_periode_calendaire_de_la_date(
    periodicite: str, reference: date, attendu: tuple[str, date, date]
) -> None:
    assert periode_attendue(periodicite, reference) == attendu


def test_periode_attendue_refuse_une_periodicite_inconnue() -> None:
    with pytest.raises(ValueError, match="périodicité inconnue"):
        periode_attendue("hebdomadaire", date(2026, 3, 1))


@pytest.mark.parametrize(
    ("periodicite", "journee", "attendu"),
    [
        # La période EN COURS n'est pas close : on propose la précédente.
        ("mensuelle", date(2026, 4, 1), ("2026-03", date(2026, 3, 1), date(2026, 3, 31))),
        ("mensuelle", date(2026, 3, 31), ("2026-02", date(2026, 2, 1), date(2026, 2, 28))),
        ("mensuelle", date(2026, 1, 15), ("2025-12", date(2025, 12, 1), date(2025, 12, 31))),
        ("trimestrielle", date(2026, 1, 15), ("2025-T4", date(2025, 10, 1), date(2025, 12, 31))),
        ("trimestrielle", date(2026, 7, 1), ("2026-T2", date(2026, 4, 1), date(2026, 6, 30))),
        ("annuelle", date(2026, 6, 30), ("2025", date(2025, 1, 1), date(2025, 12, 31))),
        ("annuelle", date(2027, 1, 1), ("2026", date(2026, 1, 1), date(2026, 12, 31))),
    ],
)
def test_derniere_periode_close_d_apres_la_journee_comptable(
    periodicite: str, journee: date, attendu: tuple[str, date, date]
) -> None:
    assert derniere_periode_close(periodicite, journee) == attendu


# --- Refus du versement -------------------------------------------------------------


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


def _produit(db: Session, periodicite: str) -> uuid.UUID:
    def compte(numero: str) -> uuid.UUID:
        return db.execute(
            text("SELECT id FROM comptabilite.accounts WHERE account_number = :n"),
            {"n": numero},
        ).scalar_one()

    produit = Product(
        code=f"PP{uuid.uuid4().hex[:6]}", name="Épargne", type="a_vue",
        compte_epargne_id=compte("251111"), compte_charge_interet_id=compte("602511"),
        taux_bp=1000, base_jours=365, methode_calcul_solde="fin_periode",
        periodicite=periodicite,
    )
    db.add(produit)
    db.flush()
    return produit.id


MARS = (date(2026, 3, 1), date(2026, 3, 31))


@pytest.mark.integration
def test_un_produit_annuel_refuse_une_periode_mensuelle(db: Session) -> None:
    produit_id = _produit(db, "annuelle")

    with pytest.raises(PeriodeIncompatibleError, match="chaque année") as erreur:
        verser_interets(db, produit_id=produit_id, periode="2026-03", debut=MARS[0], fin=MARS[1])

    # Le message dit ce qui est attendu : la période « 2026 », du 01/01/2026 au 31/12/2026.
    assert "« 2026 »" in str(erreur.value)
    assert "01/01/2026 au 31/12/2026" in str(erreur.value)


@pytest.mark.integration
def test_un_produit_mensuel_accepte_le_mois_entier(db: Session) -> None:
    produit_id = _produit(db, "mensuelle")

    rapport = verser_interets(
        db, produit_id=produit_id, periode="2026-03", debut=MARS[0], fin=MARS[1]
    )

    assert rapport.en_erreur == []


@pytest.mark.integration
def test_un_produit_mensuel_refuse_une_demi_periode(db: Session) -> None:
    produit_id = _produit(db, "mensuelle")

    with pytest.raises(PeriodeIncompatibleError, match="chaque mois"):
        verser_interets(
            db, produit_id=produit_id, periode="2026-03", debut=date(2026, 3, 1),
            fin=date(2026, 3, 15),
        )


@pytest.mark.integration
def test_un_produit_mensuel_refuse_un_mois_decale(db: Session) -> None:
    produit_id = _produit(db, "mensuelle")

    with pytest.raises(PeriodeIncompatibleError):
        verser_interets(
            db, produit_id=produit_id, periode="2026-03", debut=date(2026, 3, 2),
            fin=date(2026, 4, 1),
        )


@pytest.mark.integration
def test_un_produit_trimestriel_refuse_un_mois_et_accepte_le_trimestre(db: Session) -> None:
    produit_id = _produit(db, "trimestrielle")

    with pytest.raises(PeriodeIncompatibleError, match="chaque trimestre"):
        verser_interets(db, produit_id=produit_id, periode="2026-03", debut=MARS[0], fin=MARS[1])

    verser_interets(
        db, produit_id=produit_id, periode="2026-T1", debut=date(2026, 1, 1),
        fin=date(2026, 3, 31),
    )


@pytest.mark.integration
def test_un_libelle_different_pour_les_memes_bornes_est_refuse(db: Session) -> None:
    # Deux libellés pour la même période contourneraient l'anti-double-versement.
    produit_id = _produit(db, "mensuelle")

    with pytest.raises(PeriodeIncompatibleError):
        verser_interets(db, produit_id=produit_id, periode="MARS", debut=MARS[0], fin=MARS[1])


@pytest.mark.integration
def test_l_apercu_applique_la_meme_regle(db: Session) -> None:
    produit_id = _produit(db, "annuelle")

    with pytest.raises(PeriodeIncompatibleError):
        previsualiser_interets(
            db, produit_id=produit_id, periode="2026-03", debut=MARS[0], fin=MARS[1]
        )


@pytest.mark.integration
def test_un_produit_inconnu_est_refuse(db: Session) -> None:
    with pytest.raises(ProduitInconnuError):
        verser_interets(db, produit_id=uuid.uuid4(), periode="2026", debut=date(2026, 1, 1),
                        fin=date(2026, 12, 31))


# --- Seule une période CLOSE se verse -----------------------------------------------

MESSAGE_NON_CLOSE = "Cette période n'est pas encore close"


@pytest.mark.parametrize(
    ("periodicite", "journee", "debut", "refusee"),
    [
        # Mensuel, journée au 10/04 : mars est close, avril (en cours) et mai ne le sont pas.
        ("mensuelle", date(2026, 4, 10), date(2026, 3, 1), False),
        ("mensuelle", date(2026, 4, 10), date(2026, 4, 1), True),
        ("mensuelle", date(2026, 4, 10), date(2026, 5, 1), True),
        # Antérieure : acceptée ici (rattrapage), l'anti-double-versement tranche ensuite.
        ("mensuelle", date(2026, 4, 10), date(2026, 1, 1), False),
        # Le 1er du mois suivant clôt la période.
        ("mensuelle", date(2026, 4, 1), date(2026, 3, 1), False),
        ("mensuelle", date(2026, 3, 31), date(2026, 3, 1), True),
        ("trimestrielle", date(2026, 7, 1), date(2026, 4, 1), False),
        ("trimestrielle", date(2026, 7, 1), date(2026, 7, 1), True),
        ("annuelle", date(2026, 12, 31), date(2026, 1, 1), True),
        ("annuelle", date(2027, 1, 1), date(2026, 1, 1), False),
    ],
)
def test_verifier_periode_close_selon_la_journee_comptable(
    periodicite: str, journee: date, debut: date, refusee: bool
) -> None:
    if refusee:
        with pytest.raises(PeriodeIncompatibleError, match=MESSAGE_NON_CLOSE):
            verifier_periode_close(periodicite, debut, journee)
    else:
        verifier_periode_close(periodicite, debut, journee)


@pytest.fixture
def journee_au_10_avril(monkeypatch: pytest.MonkeyPatch) -> date:
    jour = date(2026, 4, 10)
    monkeypatch.setattr(interets, "journee_de_reference", lambda db: jour)
    return jour


@pytest.mark.integration
def test_la_periode_en_cours_est_refusee_au_versement_et_a_l_apercu(
    db: Session, journee_au_10_avril: date
) -> None:
    produit_id = _produit(db, "mensuelle")
    avril = {"periode": "2026-04", "debut": date(2026, 4, 1), "fin": date(2026, 4, 30)}

    with pytest.raises(PeriodeIncompatibleError, match=MESSAGE_NON_CLOSE):
        verser_interets(db, produit_id=produit_id, **avril)
    with pytest.raises(PeriodeIncompatibleError, match=MESSAGE_NON_CLOSE):
        previsualiser_interets(db, produit_id=produit_id, **avril)


@pytest.mark.integration
def test_la_periode_close_est_acceptee_et_ses_bornes_viennent_de_la_journee(
    db: Session, journee_au_10_avril: date
) -> None:
    produit_id = _produit(db, "mensuelle")
    periode, debut, fin = derniere_periode_close("mensuelle", journee_au_10_avril)
    assert (periode, debut, fin) == ("2026-03", date(2026, 3, 1), date(2026, 3, 31))

    rapport = verser_interets(db, produit_id=produit_id, periode=periode, debut=debut, fin=fin)

    assert rapport.en_erreur == []
    previsualiser_interets(db, produit_id=produit_id, periode=periode, debut=debut, fin=fin)
