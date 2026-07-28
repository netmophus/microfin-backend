"""Intérêts d'épargne (E5) — calcul pur, versement = écriture, et surtout l'ANTI-DOUBLE.

  - calcul PUR : mêmes entrées -> même montant (Decimal, jamais de flottant), archivé ;
  - versement = écriture équilibrée D 603 / C 3111, + mouvement + solde, une transaction ;
  - rapprochement Σ soldes == 3111 toujours concordant après versement ;
  - LE garde-fou sacré : relancer sur une période déjà traitée ne verse RIEN (contrainte base) ;
  - reprise après interruption : pas de double versement au redémarrage ;
  - compte FERMÉ ignoré ; membre SUSPENDU crédité normalement.
"""

import uuid
from collections.abc import Generator
from dataclasses import dataclass
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.core.database import engine
from app.modules.epargne import interets, service
from app.modules.epargne.guichet import deposer, fermer_compte
from app.modules.epargne.interets import (
    calculer_base_solde,
    calculer_montant,
    previsualiser_interets,
    verser_interets,
)
from app.modules.epargne.models import Product, SavingsAccount
from app.modules.epargne.rapprochement import rapprocher
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant

pytestmark = pytest.mark.integration

DEBUT = date(2026, 1, 1)
FIN = date(2026, 12, 31)  # 365 jours


# --- Calcul PUR (aucune base) -------------------------------------------------------


def test_calcul_montant_annuel_exact() -> None:
    # 100 000 à 10 % sur 365/365 jours = 10 000, au franc.
    assert calculer_montant(100000, 1000, 365, 365, "plus_proche") == 10000


def test_calcul_arrondi_plus_proche_vs_plancher() -> None:
    # 10 000 à 3,5 % sur 1/365 jour = 0,9589… -> 1 (plus proche) / 0 (plancher).
    assert calculer_montant(10000, 350, 1, 365, "plus_proche") == 1
    assert calculer_montant(10000, 350, 1, 365, "plancher") == 0


def test_calcul_taux_zero_pas_dinteret() -> None:
    assert calculer_montant(100000, 0, 365, 365, "plus_proche") == 0


def test_calcul_pur_rejouable() -> None:
    # Mêmes entrées -> même montant, à chaque appel.
    a = calculer_montant(123456, 375, 90, 360, "plus_proche")
    b = calculer_montant(123456, 375, 90, 360, "plus_proche")
    assert a == b


def test_methodes_de_calcul_du_solde() -> None:
    points = [(date(2026, 6, 1), 5000), (date(2026, 9, 1), 2000)]
    assert calculer_base_solde("fin_periode", 1000, points, DEBUT, FIN) == 2000
    assert calculer_base_solde("min_periode", 1000, points, DEBUT, FIN) == 1000
    # moyen : 1000x151 + 5000x92 + 2000x122 = 855 000 ; / 365 = 2342
    assert calculer_base_solde("moyen_quotidien", 1000, points, DEBUT, FIN) == 855000 // 365


# --- Intégration --------------------------------------------------------------------


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


@dataclass
class Cadre:
    compte: SavingsAccount
    caissier: UtilisateurCourant
    agence: uuid.UUID
    produit: uuid.UUID


def _caissier(db: Session, agency_id: uuid.UUID) -> UtilisateurCourant:
    uid = db.execute(text("SELECT id FROM security.users LIMIT 1")).scalar_one()
    return UtilisateurCourant(
        user_id=uid,
        roles=("CAISSIER",),
        permissions=frozenset({"epargne.operation.deposit"}),
        primary_agency_id=agency_id,
        agency_id=agency_id,
        voit_tout=True,
    )


def _cadre(db: Session, suffixe: str, *, depot: int = 100000) -> Cadre:
    agence = Agency(code=f"AGI-{suffixe}", name="Agence", compte_caisse_id=_cid(db, "5721"))
    # 10 % l'an, base 365 : un dépôt gardé toute l'année produit exactement depot x 10 %.
    produit = Product(
        code=f"PI{suffixe}", name="Épargne", type="a_vue",
        compte_epargne_id=_cid(db, "3111"), compte_charge_interet_id=_cid(db, "603"),
        taux_bp=1000, base_jours=365, methode_calcul_solde="fin_periode",
    )
    db.add_all([agence, produit])
    db.flush()
    tier_id = db.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES (:n, 'individual', :a, 'actif') RETURNING id"
        ),
        {"n": f"M-INT-{suffixe}", "a": agence.id},
    ).scalar_one()
    caissier = _caissier(db, agence.id)
    compte = service.ouvrir_compte(
        db, tier_id=tier_id, product_id=produit.id, agency_id=agence.id, par=None
    )
    if depot:
        deposer(db, caissier, compte.id, depot)
    return Cadre(compte, caissier, agence.id, produit.id)


def _interet_calculs(db: Session, account_id: uuid.UUID) -> list[tuple[str, int]]:
    return [
        (p, m)
        for p, m in db.execute(
            text("SELECT periode, montant FROM epargne.interet_calculs WHERE account_id = :a"),
            {"a": account_id},
        )
    ]


def test_versement_credite_pose_ecriture_et_archive(db: Session) -> None:
    c = _cadre(db, "V1")
    verser_interets(db, periode="2026", debut=DEBUT, fin=FIN)

    db.refresh(c.compte)
    assert c.compte.balance == 110000  # 100 000 + 10 000 d'intérêts
    # mouvement 'interet' credit 10 000 relié à une pièce D 603 / C 3111
    mvt = db.execute(
        text(
            "SELECT sens, amount FROM epargne.movements "
            "WHERE account_id = :a AND operation_type = 'interet'"
        ),
        {"a": c.compte.id},
    ).one()
    assert mvt == ("credit", 10000)
    lignes = {
        (n, s, m)
        for n, s, m in db.execute(
            text(
                "SELECT a.account_number, l.side, l.amount FROM comptabilite.journal_lines l "
                "JOIN comptabilite.accounts a ON a.id = l.account_id "
                "JOIN epargne.movements mv ON mv.journal_entry_id = l.entry_id "
                "WHERE mv.account_id = :a AND mv.operation_type = 'interet'"
            ),
            {"a": c.compte.id},
        )
    }
    assert lignes == {("603", "D", 10000), ("3111", "C", 10000)}
    # archivé, rejouable
    assert _interet_calculs(db, c.compte.id) == [("2026", 10000)]


def test_mouvement_porte_une_date_valeur_egale_a_la_date_operation(db: Session) -> None:
    # FONDATION DORMANTE (migration 0024) : chaque mouvement porte une date de valeur, posée par
    # défaut à la date d'opération tant qu'aucune règle (quinzaines…) ne l'en écarte.
    c = _cadre(db, "DV")  # le dépôt de _cadre crée un mouvement
    op_date, date_valeur = db.execute(
        text(
            "SELECT created_at::date, date_valeur FROM epargne.movements "
            "WHERE account_id = :a ORDER BY created_at LIMIT 1"
        ),
        {"a": c.compte.id},
    ).one()
    assert date_valeur == op_date


def test_date_valeur_immuable_comme_le_mouvement(db: Session) -> None:
    # La date de valeur est FIGÉE à la création : le trigger d'immuabilité refuse tout UPDATE,
    # elle aussi (on ne réécrit pas le passé ; une règle future la pose à l'insertion des NOUVEAUX).
    c = _cadre(db, "DVI")
    with pytest.raises(DBAPIError) as erreur:
        db.execute(
            text("UPDATE epargne.movements SET date_valeur = '2000-01-01' WHERE account_id = :a"),
            {"a": c.compte.id},
        )
    assert "immuable" in str(erreur.value)


def test_previsualisation_calcule_le_detail_sans_rien_ecrire(db: Session) -> None:
    c = _cadre(db, "PV")
    apercu = previsualiser_interets(db, periode="2026", debut=DEBUT, fin=FIN)

    # Le total ET le détail, pour vérifier « ça a l'air juste » avant de lancer.
    assert apercu.comptes_a_crediter >= 1
    assert apercu.total >= 10000
    mien = next(x for x in apercu.echantillon if x.account_number == c.compte.account_number)
    assert mien.montant == 10000
    assert mien.taux_bp == 1000  # le taux appliqué est visible, pas seulement le total
    assert mien.methode == "fin_periode"

    # DRY-RUN : rien n'a été écrit. Ni solde, ni calcul archivé.
    db.refresh(c.compte)
    assert c.compte.balance == 100000  # le dépôt seul, aucun intérêt versé
    assert _interet_calculs(db, c.compte.id) == []


def test_previsualisation_signale_periode_deja_versee(db: Session) -> None:
    c = _cadre(db, "PVD")
    verser_interets(db, periode="2026", debut=DEBUT, fin=FIN)

    apercu = previsualiser_interets(db, periode="2026", debut=DEBUT, fin=FIN)
    # La période est déjà versée : rien à re-verser, et on sait QUAND (pas d'échec silencieux).
    assert not any(x.account_number == c.compte.account_number for x in apercu.echantillon)
    assert apercu.deja_traites >= 1
    assert apercu.deja_verse_le is not None


def test_anti_double_versement_relance_ne_verse_rien(db: Session) -> None:
    c = _cadre(db, "AD")
    verser_interets(db, periode="2026", debut=DEBUT, fin=FIN)
    db.refresh(c.compte)
    assert c.compte.balance == 110000

    # RELANCE sur la même période : rien versé une seconde fois.
    rapport = verser_interets(db, periode="2026", debut=DEBUT, fin=FIN)
    db.refresh(c.compte)
    assert c.compte.balance == 110000  # inchangé
    assert rapport.credites == 0
    assert rapport.ignores >= 1
    assert _interet_calculs(db, c.compte.id) == [("2026", 10000)]  # toujours UN seul


def test_anti_double_au_niveau_base(db: Session) -> None:
    c = _cadre(db, "ADB", depot=0)
    db.execute(
        text(
            "INSERT INTO epargne.interet_calculs "
            "(account_id, periode, base_solde, methode, taux_bp, base_jours, jours, montant) "
            "VALUES (:a, '2026', 0, 'fin_periode', 0, 365, 365, 0)"
        ),
        {"a": c.compte.id},
    )
    # Deuxième insertion même (compte, période) -> refus par la contrainte UNIQUE.
    with pytest.raises(IntegrityError):
        db.execute(
            text(
                "INSERT INTO epargne.interet_calculs "
                "(account_id, periode, base_solde, methode, taux_bp, base_jours, jours, montant) "
                "VALUES (:a, '2026', 0, 'fin_periode', 0, 365, 365, 0)"
            ),
            {"a": c.compte.id},
        )


def test_rapprochement_concorde_apres_interets(db: Session) -> None:
    _cadre(db, "RA")
    verser_interets(db, periode="2026", debut=DEBUT, fin=FIN)
    resultat = rapprocher(db, _cid(db, "3111"))
    assert resultat.concordant is True


def test_compte_ferme_ne_produit_pas_dinteret(db: Session) -> None:
    c = _cadre(db, "CF")
    fermer_compte(db, c.caissier, c.compte.id)  # restitution -> solde 0, fermé

    verser_interets(db, periode="2026", debut=DEBUT, fin=FIN)

    assert _interet_calculs(db, c.compte.id) == []  # un compte fermé n'est pas traité


def test_membre_suspendu_est_credite(db: Session) -> None:
    c = _cadre(db, "SU")
    db.execute(
        text("UPDATE tiers.tiers SET status = 'suspendu_temporaire' WHERE id = :t"),
        {"t": c.compte.tier_id},
    )
    verser_interets(db, periode="2026", debut=DEBUT, fin=FIN)

    db.refresh(c.compte)
    assert c.compte.balance == 110000  # c'est son argent : la rémunération lui revient


def test_reprise_apres_interruption_ne_verse_pas_en_double(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    c1 = _cadre(db, "RP1")
    c2 = _cadre(db, "RP2")

    original = interets._verser_un
    appels = {"n": 0}

    def interrompt(*args: object, **kwargs: object) -> object:
        appels["n"] += 1
        if appels["n"] == 2:  # panne au 2e compte, après que le 1er a été committé
            raise RuntimeError("interruption du batch")
        return original(*args, **kwargs)

    monkeypatch.setattr(interets, "_verser_un", interrompt)
    with pytest.raises(RuntimeError):
        verser_interets(db, periode="2026", debut=DEBUT, fin=FIN)
    monkeypatch.undo()

    # Reprise : le 1er compte est sauté (déjà traité), les autres traités. Aucun double.
    verser_interets(db, periode="2026", debut=DEBUT, fin=FIN)

    assert _interet_calculs(db, c1.compte.id) == [("2026", 10000)]  # crédité UNE fois
    assert _interet_calculs(db, c2.compte.id) == [("2026", 10000)]
