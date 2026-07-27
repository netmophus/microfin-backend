"""Service des écritures comptables — brouillon, validation, contre-passation.

La FRONTIÈRE brouillon / validé est la colonne vertébrale (cf. migration 0017) :

  - `creer_brouillon` : espace de travail. Peut être déséquilibré, se modifie et se supprime
    librement. N'existe pas comptablement (pas de numéro, ne compte dans aucun solde).
  - `valider` : la bascule IRRÉVERSIBLE. Refuse si l'exercice n'est pas ouvert, si < 2 lignes,
    si déséquilibrée, si une ligne vise un compte de regroupement. Alloue le numéro atomique,
    fige la pièce (immuable dès lors).
  - `contre_passer` : la SEULE correction d'une pièce validée — une pièce inverse, les deux
    visibles. Jamais de retouche.

Triple garantie de l'équilibre : ce service pré-contrôle (erreurs métier claires), la contrainte
CHECK/le trigger différé de la base est le dernier rempart (il mord même sur du SQL brut), et les
deux vérifient Σ débit = Σ crédit AU NIVEAU PIÈCE.

Montants : entiers de francs CFA (int Python → BIGINT). Jamais de flottant.

Audit (D5) : on trace l'ACTE (validation, contre-passation) dans le journal d'audit — qui, quoi,
quand — en toute fin de transaction. Le CONTENU comptable (lignes, montants) vit dans le journal
comptable, jamais dupliqué dans l'audit.
"""

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.comptabilite import numerotation
from app.modules.comptabilite.models import Exercice, Journal, JournalEntry, JournalLine


class ExerciceError(Exception):
    """Problème d'exercice (aucun ouvert, clos)."""


class AucunExerciceOuvertError(ExerciceError):
    """Aucun exercice ouvert ne couvre la date : on ne pose rien."""


class PieceError(Exception):
    """Base des refus sur une pièce."""


class PieceDesequilibreeError(PieceError):
    """Σ débit ≠ Σ crédit : validation refusée."""


class PieceIncompleteError(PieceError):
    """Moins de deux lignes : validation refusée."""


class CompteNonSaisissableError(PieceError):
    """Ligne sur un compte de regroupement (non is_posting) : refusée."""


class LigneInvalideError(PieceError):
    """Sens hors D/C ou montant non strictement positif."""


class PieceDejaValideeError(PieceError):
    """La pièce est déjà validée (immuable)."""


class PieceNonValideeError(PieceError):
    """Opération réservée à une pièce validée (ex. contre-passation d'un brouillon)."""


class PieceDejaContrePasseeError(PieceError):
    """La pièce a déjà été contre-passée : on ne la contre-passe pas deux fois."""


@dataclass(frozen=True)
class LigneSaisie:
    """Une ligne saisie par l'utilisateur (avant écriture)."""

    account_id: uuid.UUID
    side: str  # 'D' ou 'C'
    amount: int  # francs CFA entiers, > 0
    label: str | None = None


class ActionsAudit:
    """Actions d'audit du module Comptabilité (format module.action)."""

    VALIDEE = "compta.ecriture.validee"
    CONTRE_PASSEE = "compta.ecriture.contre_passee"


def exercice_ouvert_pour(db: Session, jour: date) -> Exercice:
    """Rend l'unique exercice OUVERT dont la période contient `jour`, ou lève.

    L'unicité est garantie par la contrainte d'exclusion (0016) : deux exercices ne se
    chevauchent pas.
    """
    exercice = db.execute(
        select(Exercice).where(
            Exercice.status == "ouvert",
            Exercice.date_debut <= jour,
            Exercice.date_fin >= jour,
        )
    ).scalar_one_or_none()
    if exercice is None:
        raise AucunExerciceOuvertError(f"aucun exercice ouvert ne couvre le {jour}")
    return exercice


def _valider_lignes_saisies(db: Session, lignes: list[LigneSaisie]) -> None:
    """Contrôles ligne à ligne, indépendants de l'équilibre (sens, montant, compte de saisie)."""
    for ligne in lignes:
        if ligne.side not in ("D", "C"):
            raise LigneInvalideError(f"sens « {ligne.side} » invalide (attendu D ou C)")
        if ligne.amount <= 0:
            raise LigneInvalideError(f"montant {ligne.amount} invalide (doit être > 0)")
    # Compte de SAISIE uniquement (le trigger le garantit aussi ; ici, erreur métier claire).
    ids = {ligne.account_id for ligne in lignes}
    if ids:
        saisissables = set(
            db.execute(
                text(
                    "SELECT id FROM comptabilite.accounts "
                    "WHERE id = ANY(:ids) AND is_posting AND is_active"
                ),
                {"ids": list(ids)},
            ).scalars()
        )
        manquants = ids - saisissables
        if manquants:
            raise CompteNonSaisissableError(
                f"compte(s) non saisissable(s) ou inactif(s) : {manquants}"
            )


def creer_brouillon(
    db: Session,
    *,
    journal_id: uuid.UUID,
    entry_date: date,
    description: str,
    lignes: list[LigneSaisie],
    par: uuid.UUID | None,
) -> JournalEntry:
    """Crée une pièce en BROUILLON. Peut être déséquilibrée ; l'exercice doit être ouvert.

    On rattache l'exercice dès le brouillon (FK non nulle) : préparer une pièce, c'est déjà la
    situer dans un exercice ouvert. Les lignes sont contrôlées (sens, montant, compte de saisie),
    mais PAS l'équilibre — c'est un espace de travail.
    """
    exercice = exercice_ouvert_pour(db, entry_date)
    _valider_lignes_saisies(db, lignes)

    entry = JournalEntry(
        journal_id=journal_id,
        exercice_id=exercice.id,
        entry_date=entry_date,
        description=description,
        status="brouillon",
        created_by=par,
        updated_by=par,
    )
    db.add(entry)
    db.flush()  # obtient entry.id

    for numero, ligne in enumerate(lignes, start=1):
        db.add(
            JournalLine(
                entry_id=entry.id,
                line_number=numero,
                account_id=ligne.account_id,
                side=ligne.side,
                amount=ligne.amount,
                label=ligne.label,
            )
        )
    db.flush()
    return entry


def supprimer_brouillon(db: Session, entry: JournalEntry) -> None:
    """Supprime un brouillon (et ses lignes, par cascade). Refuse une pièce validée."""
    if entry.status == "validee":
        raise PieceDejaValideeError("une pièce validée ne se supprime pas (contre-passer)")
    db.delete(entry)
    db.flush()


def _totaux(db: Session, entry_id: uuid.UUID) -> tuple[int, int, int]:
    """Rend (nombre de lignes, total débit, total crédit) d'une pièce."""
    ligne = db.execute(
        text(
            "SELECT count(*), "
            "COALESCE(SUM(amount) FILTER (WHERE side='D'), 0), "
            "COALESCE(SUM(amount) FILTER (WHERE side='C'), 0) "
            "FROM comptabilite.journal_lines WHERE entry_id = :e"
        ),
        {"e": entry_id},
    ).one()
    return int(ligne[0]), int(ligne[1]), int(ligne[2])


def valider(
    db: Session,
    entry: JournalEntry,
    par: uuid.UUID | None,
    *,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> JournalEntry:
    """Bascule un brouillon en pièce VALIDÉE : numéro atomique, immuable, intégrée aux soldes.

    Refuse si déjà validée, si l'exercice n'est plus ouvert, si < 2 lignes ou si déséquilibrée.
    Le service pré-contrôle (erreurs claires) ; le trigger différé de la base est le dernier
    rempart au commit.
    """
    if entry.status == "validee":
        raise PieceDejaValideeError(f"pièce {entry.entry_number} déjà validée")

    exercice = db.get(Exercice, entry.exercice_id)
    if exercice is None or exercice.status != "ouvert":
        raise ExerciceError("l'exercice de la pièce n'est pas ouvert")

    nb, total_d, total_c = _totaux(db, entry.id)
    if nb < 2:
        raise PieceIncompleteError(f"pièce à {nb} ligne(s) : au moins deux exigées")
    if total_d != total_c:
        raise PieceDesequilibreeError(f"débit {total_d} ≠ crédit {total_c}")

    journal = db.get(Journal, entry.journal_id)
    assert journal is not None  # FK garantit l'existence
    numero = numerotation.prochain_numero(
        db,
        journal_id=entry.journal_id,
        exercice_id=entry.exercice_id,
        journal_code=journal.code,
        exercice_code=exercice.code,
    )

    entry.entry_number = numero
    entry.status = "validee"
    entry.posted_by = par
    entry.updated_by = par
    entry.posted_at = db.execute(text("SELECT NOW()")).scalar_one()
    db.flush()

    # Audit de l'ACTE, en dernier (D5). Le contenu comptable reste dans le journal comptable.
    ecrire_audit(
        db,
        action=ActionsAudit.VALIDEE,
        contexte=contexte,
        acteur_id=par,
        resource_type="compta.journal_entry",
        resource_id=entry.id,
        new_values={"entry_number": numero, "journal": journal.code, "total": total_d},
    )
    return entry


def contre_passer(
    db: Session,
    entry: JournalEntry,
    par: uuid.UUID | None,
    *,
    description: str | None = None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> JournalEntry:
    """Contre-passe une pièce validée : crée et valide une pièce INVERSE (D↔C), les deux visibles.

    La date de contre-passation est celle du jour (côté base) ; elle doit tomber dans un exercice
    ouvert. La pièce d'origine n'est PAS modifiée (son immuabilité est intacte) ; le lien se lit
    par `reversal_of_id` sur la pièce inverse.
    """
    if entry.status != "validee":
        raise PieceNonValideeError("seule une pièce validée se contre-passe")

    deja = db.execute(
        select(JournalEntry.id).where(JournalEntry.reversal_of_id == entry.id)
    ).first()
    if deja is not None:
        raise PieceDejaContrePasseeError(f"pièce {entry.entry_number} déjà contre-passée")

    jour = db.execute(text("SELECT CURRENT_DATE")).scalar_one()
    exercice = exercice_ouvert_pour(db, jour)

    lignes_origine = (
        db.execute(
            select(JournalLine)
            .where(JournalLine.entry_id == entry.id)
            .order_by(JournalLine.line_number)
        )
        .scalars()
        .all()
    )

    inverse = JournalEntry(
        journal_id=entry.journal_id,
        exercice_id=exercice.id,
        entry_date=jour,
        description=description or f"Contre-passation de {entry.entry_number}",
        status="brouillon",
        reversal_of_id=entry.id,
        created_by=par,
        updated_by=par,
    )
    db.add(inverse)
    db.flush()

    for ligne in lignes_origine:
        db.add(
            JournalLine(
                entry_id=inverse.id,
                line_number=ligne.line_number,
                account_id=ligne.account_id,
                side="C" if ligne.side == "D" else "D",  # inversion
                amount=ligne.amount,
                label=f"Contre-passation : {ligne.label}" if ligne.label else None,
            )
        )
    db.flush()

    valider(db, inverse, par, contexte=contexte)
    ecrire_audit(
        db,
        action=ActionsAudit.CONTRE_PASSEE,
        contexte=contexte,
        acteur_id=par,
        resource_type="compta.journal_entry",
        resource_id=inverse.id,
        new_values={"contre_passe": str(entry.id), "entry_number": inverse.entry_number},
    )
    return inverse
