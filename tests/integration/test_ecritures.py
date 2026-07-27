"""Les écritures à double entrée et leurs garde-fous, VUS MORDRE.

Le garde-fou SACRÉ, à plusieurs niveaux :
  - service : pièce déséquilibrée / à une seule ligne / sur compte de regroupement → refus clair ;
  - base (dernier rempart) : les mêmes refus tiennent même sur du SQL brut (trigger différé au
    commit pour l'équilibre, trigger immédiat pour le compte de saisie).

Et la FRONTIÈRE brouillon/validé :
  - un brouillon peut être déséquilibré, se supprime librement, n'a pas de numéro ;
  - une pièce validée est numérotée, immuable ; sa seule correction est la contre-passation.
"""

import uuid
from collections.abc import Generator
from dataclasses import dataclass
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, InternalError
from sqlalchemy.orm import Session

from app.core.database import engine
from app.modules.comptabilite import ecritures
from app.modules.comptabilite.ecritures import (
    AucunExerciceOuvertError,
    CompteNonSaisissableError,
    LigneSaisie,
    PieceDejaContrePasseeError,
    PieceDejaValideeError,
    PieceDesequilibreeError,
    PieceIncompleteError,
    PieceNonValideeError,
)
from app.modules.comptabilite.models import Account, Exercice, Journal, JournalEntry

pytestmark = pytest.mark.integration

DANS_2099 = date(2099, 6, 15)


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


@dataclass
class Cadre:
    journal: Journal
    exercice: Exercice
    a: Account  # compte de saisie
    b: Account  # compte de saisie
    regroupement: Account  # compte de regroupement (non is_posting)


@pytest.fixture
def cadre(db: Session) -> Cadre:
    """Un journal, un exercice ouvert (2099, sans chevaucher le 2026 committé) et trois comptes."""
    journal = Journal(code="TST", name="Journal de test", type="operations_diverses")
    exercice = Exercice(
        code="2099", label="Exercice 2099", date_debut=date(2099, 1, 1), date_fin=date(2099, 12, 31)
    )
    a = Account(
        account_number="6T801", name="Saisie A", account_class=6, normal_side="D", is_posting=True
    )
    b = Account(
        account_number="6T802", name="Saisie B", account_class=6, normal_side="C", is_posting=True
    )
    regroupement = Account(
        account_number="6T800", name="Regroupement", account_class=6, normal_side="D",
        is_posting=False,
    )
    db.add_all([journal, exercice, a, b, regroupement])
    db.flush()
    return Cadre(journal=journal, exercice=exercice, a=a, b=b, regroupement=regroupement)


def _brouillon(db: Session, cadre: Cadre, lignes: list[LigneSaisie], jour: date = DANS_2099):
    return ecritures.creer_brouillon(
        db,
        journal_id=cadre.journal.id,
        entry_date=jour,
        description="Écriture de test",
        lignes=lignes,
        par=None,
    )


# --- Chemin légitime + frontière brouillon ------------------------------------------


def test_valider_une_piece_equilibree_reussit(db: Session, cadre: Cadre) -> None:
    entry = _brouillon(
        db,
        cadre,
        [LigneSaisie(cadre.a.id, "D", 1000), LigneSaisie(cadre.b.id, "C", 1000)],
    )
    assert entry.status == "brouillon"
    assert entry.entry_number is None  # un brouillon n'a pas de numéro

    ecritures.valider(db, entry, par=None)

    assert entry.status == "validee"
    assert entry.entry_number == "TST-2099-000001"
    assert entry.posted_at is not None


def test_un_brouillon_desequilibre_est_accepte(db: Session, cadre: Cadre) -> None:
    # Espace de travail : le déséquilibre est permis TANT QU'ON NE VALIDE PAS.
    entry = _brouillon(
        db, cadre, [LigneSaisie(cadre.a.id, "D", 1000), LigneSaisie(cadre.b.id, "C", 400)]
    )
    assert entry.status == "brouillon"


def test_supprimer_un_brouillon_reussit(db: Session, cadre: Cadre) -> None:
    entry = _brouillon(
        db, cadre, [LigneSaisie(cadre.a.id, "D", 1000), LigneSaisie(cadre.b.id, "C", 1000)]
    )
    entry_id = entry.id
    ecritures.supprimer_brouillon(db, entry)
    assert db.get(JournalEntry, entry_id) is None


# --- Garde-fou sacré, au niveau SERVICE ---------------------------------------------


def test_valider_une_piece_desequilibree_est_refuse(db: Session, cadre: Cadre) -> None:
    entry = _brouillon(
        db, cadre, [LigneSaisie(cadre.a.id, "D", 1000), LigneSaisie(cadre.b.id, "C", 999)]
    )
    with pytest.raises(PieceDesequilibreeError):
        ecritures.valider(db, entry, par=None)
    assert entry.status == "brouillon"  # la bascule n'a pas eu lieu


def test_valider_une_piece_a_une_seule_ligne_est_refuse(db: Session, cadre: Cadre) -> None:
    entry = _brouillon(db, cadre, [LigneSaisie(cadre.a.id, "D", 1000)])
    with pytest.raises(PieceIncompleteError):
        ecritures.valider(db, entry, par=None)


def test_ligne_sur_compte_de_regroupement_refusee_au_service(db: Session, cadre: Cadre) -> None:
    with pytest.raises(CompteNonSaisissableError):
        _brouillon(
            db,
            cadre,
            [LigneSaisie(cadre.regroupement.id, "D", 1000), LigneSaisie(cadre.b.id, "C", 1000)],
        )


def test_aucun_exercice_ouvert_refus_propre(db: Session, cadre: Cadre) -> None:
    # 2098 n'est couvert par aucun exercice ouvert.
    with pytest.raises(AucunExerciceOuvertError):
        _brouillon(
            db,
            cadre,
            [LigneSaisie(cadre.a.id, "D", 1000), LigneSaisie(cadre.b.id, "C", 1000)],
            jour=date(2098, 6, 15),
        )


# --- Garde-fou sacré, au niveau BASE (le dernier rempart) ---------------------------


def _entry_brouillon_sql(db: Session, cadre: Cadre) -> uuid.UUID:
    return db.execute(
        text(
            "INSERT INTO comptabilite.journal_entries "
            "(journal_id, exercice_id, entry_date, description, status) "
            "VALUES (:j, :e, :d, 'SQL brut', 'brouillon') RETURNING id"
        ),
        {"j": cadre.journal.id, "e": cadre.exercice.id, "d": DANS_2099},
    ).scalar_one()


def _ligne_sql(
    db: Session, entry_id: uuid.UUID, account_id: uuid.UUID, side: str, amount: int, n: int
) -> None:
    db.execute(
        text(
            "INSERT INTO comptabilite.journal_lines "
            "(entry_id, line_number, account_id, side, amount) "
            "VALUES (:e, :n, :a, :s, :m)"
        ),
        {"e": entry_id, "n": n, "a": account_id, "s": side, "m": amount},
    )


def _valider_sql(db: Session, entry_id: uuid.UUID, numero: str) -> None:
    db.execute(
        text(
            "UPDATE comptabilite.journal_entries "
            "SET status='validee', entry_number=:num, posted_at=NOW() WHERE id=:id"
        ),
        {"num": numero, "id": entry_id},
    )


def test_equilibre_impose_par_la_base_au_commit(db: Session, cadre: Cadre) -> None:
    # SQL brut : on court-circuite le service. Le trigger différé doit mordre à la vérification.
    eid = _entry_brouillon_sql(db, cadre)
    _ligne_sql(db, eid, cadre.a.id, "D", 1000, 1)
    _ligne_sql(db, eid, cadre.b.id, "C", 500, 2)  # déséquilibrée
    _valider_sql(db, eid, "TST-2099-999001")

    with pytest.raises((IntegrityError, InternalError)) as exc:
        db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert "desequilibree" in str(exc.value).lower() or "déséquilibrée" in str(exc.value).lower()


def test_moins_de_deux_lignes_impose_par_la_base(db: Session, cadre: Cadre) -> None:
    eid = _entry_brouillon_sql(db, cadre)
    _ligne_sql(db, eid, cadre.a.id, "D", 1000, 1)  # une seule ligne
    _valider_sql(db, eid, "TST-2099-999002")

    with pytest.raises((IntegrityError, InternalError)):
        db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


def test_ligne_sur_regroupement_refusee_par_la_base(db: Session, cadre: Cadre) -> None:
    eid = _entry_brouillon_sql(db, cadre)
    # Trigger immédiat : l'insertion même est refusée.
    with pytest.raises(IntegrityError) as exc:
        _ligne_sql(db, eid, cadre.regroupement.id, "D", 1000, 1)
    assert "regroupement" in str(exc.value).lower()


# --- Immuabilité de la pièce validée ------------------------------------------------


def _piece_validee(db: Session, cadre: Cadre) -> JournalEntry:
    entry = _brouillon(
        db, cadre, [LigneSaisie(cadre.a.id, "D", 1000), LigneSaisie(cadre.b.id, "C", 1000)]
    )
    ecritures.valider(db, entry, par=None)
    return entry


def test_une_piece_validee_ne_se_modifie_pas(db: Session, cadre: Cadre) -> None:
    entry = _piece_validee(db, cadre)
    with pytest.raises((IntegrityError, InternalError)) as exc:
        db.execute(
            text("UPDATE comptabilite.journal_entries SET description='hack' WHERE id=:id"),
            {"id": entry.id},
        )
    assert "modification interdite" in str(exc.value).lower()


def test_une_piece_validee_ne_se_supprime_pas(db: Session, cadre: Cadre) -> None:
    entry = _piece_validee(db, cadre)
    with pytest.raises((IntegrityError, InternalError)) as exc:
        db.execute(
            text("DELETE FROM comptabilite.journal_entries WHERE id=:id"), {"id": entry.id}
        )
    assert "suppression interdite" in str(exc.value).lower()


def test_supprimer_brouillon_refuse_une_piece_validee(db: Session, cadre: Cadre) -> None:
    entry = _piece_validee(db, cadre)
    with pytest.raises(PieceDejaValideeError):
        ecritures.supprimer_brouillon(db, entry)


def test_les_lignes_dune_piece_validee_sont_figees(db: Session, cadre: Cadre) -> None:
    entry = _piece_validee(db, cadre)
    with pytest.raises((IntegrityError, InternalError)) as exc:
        _ligne_sql(db, entry.id, cadre.a.id, "D", 1, 3)
    assert "figee" in str(exc.value).lower() or "figées" in str(exc.value).lower()


# --- Numérotation atomique sans trou ------------------------------------------------


def test_numerotation_sequentielle_sans_trou(db: Session, cadre: Cadre) -> None:
    numeros = []
    for _ in range(3):
        entry = _brouillon(
            db, cadre, [LigneSaisie(cadre.a.id, "D", 100), LigneSaisie(cadre.b.id, "C", 100)]
        )
        ecritures.valider(db, entry, par=None)
        numeros.append(entry.entry_number)
    assert numeros == ["TST-2099-000001", "TST-2099-000002", "TST-2099-000003"]


# --- Contre-passation : la seule correction -----------------------------------------


def test_contre_passation_cree_une_piece_inverse_les_deux_visibles(
    db: Session, cadre: Cadre
) -> None:
    origine = _brouillon(
        db, cadre, [LigneSaisie(cadre.a.id, "D", 1000), LigneSaisie(cadre.b.id, "C", 1000)]
    )
    ecritures.valider(db, origine, par=None)

    inverse = ecritures.contre_passer(db, origine, par=None)

    assert inverse.status == "validee"
    assert inverse.reversal_of_id == origine.id
    # L'originale n'a pas bougé (immuable).
    assert origine.status == "validee"
    # Les sens sont inversés, mêmes comptes et montants.
    lignes_inverse = {
        (ln.account_id, ln.side, ln.amount)
        for ln in db.execute(
            text(
                "SELECT account_id, side, amount FROM comptabilite.journal_lines "
                "WHERE entry_id=:e"
            ),
            {"e": inverse.id},
        )
    }
    assert (cadre.a.id, "C", 1000) in lignes_inverse
    assert (cadre.b.id, "D", 1000) in lignes_inverse


def test_contre_passation_refuse_un_brouillon(db: Session, cadre: Cadre) -> None:
    brouillon = _brouillon(
        db, cadre, [LigneSaisie(cadre.a.id, "D", 1000), LigneSaisie(cadre.b.id, "C", 1000)]
    )
    with pytest.raises(PieceNonValideeError):
        ecritures.contre_passer(db, brouillon, par=None)


def test_contre_passation_refuse_deux_fois(db: Session, cadre: Cadre) -> None:
    origine = _piece_validee(db, cadre)
    ecritures.contre_passer(db, origine, par=None)
    with pytest.raises(PieceDejaContrePasseeError):
        ecritures.contre_passer(db, origine, par=None)
