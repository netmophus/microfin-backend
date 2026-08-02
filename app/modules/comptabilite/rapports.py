"""Rapports comptables en LECTURE SEULE — grand livre (R1) et balance (R2).

Aucune écriture ici : ce sont des VUES sur `comptabilite.journal_lines` / `journal_entries`
(écritures VALIDÉES uniquement), le même socle de requête que `epargne/rapprochement.py`. Rien
n'est mis en cache : tout est recalculé à chaque appel, comme partout ailleurs dans ce module —
un rapport comptable qui mentirait par staleness serait pire qu'utile.

Convention de signe : un solde est présenté selon le sens NORMAL du compte (+ si le compte se
comporte comme attendu, - sinon) — jamais le solde brut débit-crédit, qui serait illisible pour
un compte de passif (toujours "négatif" en brut alors qu'il porte un solde créditeur normal).
"""

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.modules.comptabilite.models import Account, Journal, JournalEntry, JournalLine

TAILLE_PAGE_GRAND_LIVRE = 50

_ORDRE_CHRONOLOGIQUE = (JournalEntry.entry_date, JournalEntry.entry_number, JournalLine.line_number)


class RapportError(Exception):
    """Base des refus propres aux rapports."""


class CompteNonSaisieError(RapportError):
    """Le grand livre n'existe que pour un compte de SAISIE (aucune écriture ne passe jamais
    sur un compte de regroupement — l'interroger n'aurait pas de sens)."""


# --- R1 — Grand livre ------------------------------------------------------------------------


@dataclass(frozen=True)
class LigneGrandLivreResultat:
    entry_date: date
    entry_number: str | None
    journal_code: str
    label: str
    side: str
    amount: int
    solde_cumule: int


@dataclass(frozen=True)
class GrandLivreResultat:
    compte: Account
    solde_ouverture: int
    lignes: list[LigneGrandLivreResultat]
    total: int


def _conditions_lignes(
    compte_id: uuid.UUID, date_debut: date | None, date_fin: date | None
) -> list:
    conditions: list = [JournalLine.account_id == compte_id, JournalEntry.status == "validee"]
    if date_debut is not None:
        conditions.append(JournalEntry.entry_date >= date_debut)
    if date_fin is not None:
        conditions.append(JournalEntry.entry_date <= date_fin)
    return conditions


def _solde_avant(db: Session, compte: Account, avant: date) -> int:
    """Σ signée (selon le sens normal du compte) de tous les mouvements VALIDÉS strictement
    antérieurs à `avant` — le solde d'ouverture d'une période."""
    stmt = (
        select(
            func.coalesce(
                func.sum(case((JournalLine.side == compte.normal_side, JournalLine.amount), else_=-JournalLine.amount)),
                0,
            )
        )
        .select_from(JournalLine)
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(
            JournalLine.account_id == compte.id,
            JournalEntry.status == "validee",
            JournalEntry.entry_date < avant,
        )
    )
    return int(db.execute(stmt).scalar_one())


def grand_livre(
    db: Session,
    compte: Account,
    *,
    date_debut: date | None,
    date_fin: date | None,
    page: int = 1,
    taille: int = TAILLE_PAGE_GRAND_LIVRE,
) -> GrandLivreResultat:
    """Grand livre d'UN compte de saisie : mouvements validés triés chronologiquement, avec
    solde cumulé après chaque ligne.

    Le solde cumulé reste EXACT à travers la pagination : `solde_ouverture` couvre tout ce qui
    précède `date_debut` (0 si pas de borne) ; pour une page > 1, on ajoute la Σ signée de
    TOUTES les lignes filtrées qui précèdent cette page (une requête bornée par le même décalage,
    sans limite de taille de page) avant d'accumuler les lignes de la page elle-même — jamais une
    somme fausse d'un cran à la frontière entre deux pages.
    """
    if not compte.is_posting:
        raise CompteNonSaisieError(
            f"Le compte « {compte.account_number} » est un compte de regroupement : pas de "
            "grand livre (aucune écriture n'y est jamais passée)."
        )

    conditions = _conditions_lignes(compte.id, date_debut, date_fin)

    total = db.execute(
        select(func.count())
        .select_from(JournalLine)
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(*conditions)
    ).scalar_one()

    solde = _solde_avant(db, compte, date_debut) if date_debut is not None else 0
    solde_ouverture = solde

    decalage = (page - 1) * taille
    if decalage:
        precedentes = db.execute(
            select(JournalLine.side, JournalLine.amount)
            .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
            .where(*conditions)
            .order_by(*_ORDRE_CHRONOLOGIQUE)
            .limit(decalage)
        ).all()
        for side, amount in precedentes:
            solde += amount if side == compte.normal_side else -amount

    resultats = db.execute(
        select(JournalLine, JournalEntry, Journal)
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Journal, JournalEntry.journal_id == Journal.id)
        .where(*conditions)
        .order_by(*_ORDRE_CHRONOLOGIQUE)
        .offset(decalage)
        .limit(taille)
    ).all()

    lignes = []
    for ligne, entree, journal in resultats:
        solde += ligne.amount if ligne.side == compte.normal_side else -ligne.amount
        lignes.append(
            LigneGrandLivreResultat(
                entry_date=entree.entry_date,
                entry_number=entree.entry_number,
                journal_code=journal.code,
                label=ligne.label or entree.description,
                side=ligne.side,
                amount=ligne.amount,
                solde_cumule=solde,
            )
        )

    return GrandLivreResultat(
        compte=compte, solde_ouverture=solde_ouverture, lignes=lignes, total=total
    )


# --- R2 — Balance ------------------------------------------------------------------------------


@dataclass(frozen=True)
class LigneBalanceResultat:
    compte: Account
    solde_ouverture: int
    total_debit: int
    total_credit: int
    solde_cloture: int


@dataclass(frozen=True)
class BalanceResultat:
    lignes: list[LigneBalanceResultat]
    total_debit: int
    total_credit: int


def _agreger_par_compte(db: Session, conditions: list) -> dict[uuid.UUID, tuple[int, int]]:
    """Σdébit / Σcrédit BRUTS par compte, sous les conditions données."""
    stmt = (
        select(
            JournalLine.account_id,
            func.coalesce(func.sum(case((JournalLine.side == "D", JournalLine.amount), else_=0)), 0),
            func.coalesce(func.sum(case((JournalLine.side == "C", JournalLine.amount), else_=0)), 0),
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(*conditions)
        .group_by(JournalLine.account_id)
    )
    return {compte_id: (debit, credit) for compte_id, debit, credit in db.execute(stmt).all()}


def balance(
    db: Session,
    *,
    date_debut: date | None,
    date_fin: date | None,
    inclure_sans_mouvement: bool = False,
) -> BalanceResultat:
    """Balance de tous les comptes de saisie sur la période.

    Σdébit / Σcrédit sont BRUTS (pas signés) — c'est le sens du contrôle. `total_debit` /
    `total_credit` (retournés à part) sont l'invariant natif de la partie double : ils sont
    TOUJOURS égaux pour des écritures passées par le service (chaque pièce validée est
    équilibrée) ; les afficher est une preuve pour le comptable, pas un calcul de plus — un
    écart signalerait une anomalie posée hors du chemin normal.

    Par défaut, seuls les comptes AYANT eu un mouvement sur la période apparaissent
    (`inclure_sans_mouvement=False`) : sinon la balance afficherait ~380 lignes à zéro.
    """
    conditions_periode: list = [JournalEntry.status == "validee"]
    if date_debut is not None:
        conditions_periode.append(JournalEntry.entry_date >= date_debut)
    if date_fin is not None:
        conditions_periode.append(JournalEntry.entry_date <= date_fin)
    mouvements = _agreger_par_compte(db, conditions_periode)

    ouverture: dict[uuid.UUID, tuple[int, int]] = {}
    if date_debut is not None:
        ouverture = _agreger_par_compte(
            db, [JournalEntry.status == "validee", JournalEntry.entry_date < date_debut]
        )

    comptes_stmt = select(Account).where(Account.is_posting)
    if not inclure_sans_mouvement:
        comptes_stmt = comptes_stmt.where(Account.id.in_(mouvements.keys()))
    comptes_stmt = comptes_stmt.order_by(Account.account_number)

    lignes = []
    for compte in db.execute(comptes_stmt).scalars():
        debit, credit = mouvements.get(compte.id, (0, 0))
        debit_ouv, credit_ouv = ouverture.get(compte.id, (0, 0))
        signe = 1 if compte.normal_side == "D" else -1
        solde_ouverture = signe * (debit_ouv - credit_ouv)
        solde_cloture = solde_ouverture + signe * (debit - credit)
        lignes.append(
            LigneBalanceResultat(
                compte=compte,
                solde_ouverture=solde_ouverture,
                total_debit=debit,
                total_credit=credit,
                solde_cloture=solde_cloture,
            )
        )

    total_debit = sum(debit for debit, _credit in mouvements.values())
    total_credit = sum(credit for _debit, credit in mouvements.values())

    return BalanceResultat(lignes=lignes, total_debit=total_debit, total_credit=total_credit)
