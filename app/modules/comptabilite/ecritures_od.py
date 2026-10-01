"""Saisie manuelle d'écriture, journal OD (Opérations diverses) — chantier P1, lot 1.

Couche MINCE au-dessus du moteur (ecritures.py, jamais modifié) : résout le compte par NUMÉRO
(même convention que tous les écrans de rattachement — SelecteurCompte ne connaît que des
numéros, jamais un UUID), restreint STRUCTURELLEMENT au journal OD, et prépare les lignes
enrichies (numéro + libellé de compte) pour l'affichage — le moteur ne porte que des account_id.

POURQUOI SEUL OD (décision actée, pas une validation après coup) : CA/BQ/AN restent pilotés par
les modules métier (décaissement, dépôt, transfert de caisse...) — une saisie manuelle dans ces
journaux casserait le lien entre le mouvement comptable et le registre auxiliaire du module
(Repayment, SavingsAccount, etc.), jamais mis à jour par une écriture posée à la main. Le journal
n'est JAMAIS un champ accepté côté API : impossible de le demander, donc impossible de le
contourner — plus fort qu'« accepter puis refuser si ce n'est pas OD ».

CETTE RESTRICTION S'APPLIQUE À TOUTES LES OPÉRATIONS, pas seulement à la création :
`charger_od`/`lister_od` ne renvoient JAMAIS une pièce d'un autre journal — une pièce de caisse
ou de crédit, par exemple, est invisible depuis ce module (404 au routeur, comme une ressource
hors périmètre). Sans ça, cet écran deviendrait une porte dérobée pour contre-passer ou consulter
n'importe quelle pièce posée par un autre module via ses propres règles métier.

PAS DE LISTE NOIRE DE COMPTES (décision actée) : tout compte de saisie actif est autorisé — le
moteur le garantit déjà (is_posting AND is_active, voir ecritures._valider_lignes_saisies). La
seule garde ajoutée ici porte sur le JOURNAL, jamais sur le compte.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.modules.comptabilite.comptes import compte_saisie_actif
from app.modules.comptabilite.ecritures import LigneSaisie
from app.modules.comptabilite.models import Account, Journal, JournalEntry, JournalLine

CODE_JOURNAL_OD = "OD"

TAILLE_PAGE_DEFAUT = 50
TAILLE_PAGE_MAX = 100


class JournalODIntrouvableError(Exception):
    """Le journal OD n'existe pas — paramétrage incomplet (seed_comptabilite.py non joué).
    Ne devrait jamais arriver en usage normal."""


def journal_od_id(db: Session) -> uuid.UUID:
    """L'id du journal OD. SEUL journal autorisé à la saisie manuelle — jamais un paramètre
    côté API, voir docstring module."""
    journal_id = db.execute(
        select(Journal.id).where(Journal.code == CODE_JOURNAL_OD)
    ).scalar_one_or_none()
    if journal_id is None:
        raise JournalODIntrouvableError(
            "le journal OD (Opérations diverses) n'existe pas — paramétrage incomplet"
        )
    return journal_id


@dataclass(frozen=True)
class LigneSaisieNumero:
    """Une ligne saisie par NUMÉRO de compte — ce que l'écran envoie (SelecteurCompte ne
    renvoie jamais un UUID)."""

    account_number: str
    side: str
    amount: int
    label: str | None = None


def resoudre_lignes(db: Session, lignes: list[LigneSaisieNumero]) -> list[LigneSaisie]:
    """Résout chaque numéro de compte en account_id — réutilise `compte_saisie_actif` (même
    garde-fou que tous les écrans de rattachement : compte de saisie actif, sinon refus nommant
    le numéro en cause). Le moteur revérifie ensuite par account_id (son dernier mot), mais
    l'erreur ici est attribuable à un numéro lisible, jamais à un UUID brut."""
    return [
        LigneSaisie(
            account_id=compte_saisie_actif(db, ligne.account_number).id,
            side=ligne.side,
            amount=ligne.amount,
            label=ligne.label,
        )
        for ligne in lignes
    ]


@dataclass(frozen=True)
class LigneEcritureDetail:
    account_number: str
    name: str
    side: str
    amount: int
    label: str | None


@dataclass(frozen=True)
class EcritureAvecTotaux:
    entry: JournalEntry
    nb_lignes: int
    total_debit: int
    total_credit: int
    deja_contre_passee: bool


def charger_od(db: Session, entry_id: uuid.UUID) -> JournalEntry | None:
    """Une pièce du journal OD par id — None si elle n'existe pas OU si elle appartient à un
    autre journal (voir docstring module : jamais distingué du 404 générique côté routeur)."""
    return db.execute(
        select(JournalEntry)
        .join(Journal, Journal.id == JournalEntry.journal_id)
        .where(JournalEntry.id == entry_id, Journal.code == CODE_JOURNAL_OD)
    ).scalar_one_or_none()


def _deja_contre_passees(db: Session, entry_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Parmi `entry_ids`, celles qui sont DÉJÀ contre-passées (une autre pièce les référence via
    `reversal_of_id`) — pour que l'écran grise le bouton plutôt que de laisser l'utilisateur
    découvrir le refus du moteur (PieceDejaContrePasseeError) après coup."""
    if not entry_ids:
        return set()
    # `reversal_of_id` ne peut pas être NULL ici : la clause IN exclut déjà les lignes nulles.
    return {
        id_ for id_ in db.execute(
            select(JournalEntry.reversal_of_id).where(
                JournalEntry.reversal_of_id.in_(entry_ids)
            )
        ).scalars()
        if id_ is not None
    }


def avec_totaux(db: Session, entry: JournalEntry) -> EcritureAvecTotaux:
    """Une pièce + son nombre de lignes et ses totaux débit/crédit (calculés depuis les lignes,
    jamais stockés — cohérent avec `ecritures._totaux`, qui fait de même pour la validation).
    `nb_lignes` permet à l'écran de griser « Valider » pour une pièce à une seule ligne même si
    elle paraît équilibrée (0 = 0) — `equilibree` seule ne suffit pas à anticiper le refus du
    moteur (PieceIncompleteError, exige >= 2 lignes)."""
    nb, total_d, total_c = db.execute(
        select(
            func.count(),
            func.coalesce(
                func.sum(case((JournalLine.side == "D", JournalLine.amount), else_=0)), 0
            ),
            func.coalesce(
                func.sum(case((JournalLine.side == "C", JournalLine.amount), else_=0)), 0
            ),
        ).where(JournalLine.entry_id == entry.id)
    ).one()
    deja_cp = bool(_deja_contre_passees(db, [entry.id]))
    return EcritureAvecTotaux(
        entry=entry, nb_lignes=int(nb), total_debit=int(total_d), total_credit=int(total_c),
        deja_contre_passee=deja_cp,
    )


def lignes_avec_compte(db: Session, entry_id: uuid.UUID) -> list[LigneEcritureDetail]:
    """Les lignes d'une pièce, enrichies du numéro et du libellé de compte (jamais l'UUID brut
    à l'écran)."""
    resultats = db.execute(
        select(JournalLine, Account.account_number, Account.name)
        .join(Account, Account.id == JournalLine.account_id)
        .where(JournalLine.entry_id == entry_id)
        .order_by(JournalLine.line_number)
    ).all()
    return [
        LigneEcritureDetail(
            account_number=numero, name=nom, side=ligne.side, amount=ligne.amount, label=ligne.label
        )
        for ligne, numero, nom in resultats
    ]


def lister_od(
    db: Session, *, page: int, taille: int
) -> tuple[list[EcritureAvecTotaux], int]:
    """Les pièces du journal OD, les plus récentes d'abord — total/débit/crédit calculés en
    une seule requête agrégée (pas de N+1)."""
    total = db.execute(
        select(func.count(JournalEntry.id))
        .join(Journal, Journal.id == JournalEntry.journal_id)
        .where(Journal.code == CODE_JOURNAL_OD)
    ).scalar_one()

    totaux_ligne = (
        select(
            JournalLine.entry_id.label("entry_id"),
            func.count().label("nb"),
            func.coalesce(
                func.sum(case((JournalLine.side == "D", JournalLine.amount), else_=0)), 0
            ).label("debit"),
            func.coalesce(
                func.sum(case((JournalLine.side == "C", JournalLine.amount), else_=0)), 0
            ).label("credit"),
        )
        .group_by(JournalLine.entry_id)
        .subquery()
    )
    resultats = db.execute(
        select(JournalEntry, totaux_ligne.c.nb, totaux_ligne.c.debit, totaux_ligne.c.credit)
        .join(Journal, Journal.id == JournalEntry.journal_id)
        .outerjoin(totaux_ligne, totaux_ligne.c.entry_id == JournalEntry.id)
        .where(Journal.code == CODE_JOURNAL_OD)
        .order_by(JournalEntry.created_at.desc())
        .offset((page - 1) * taille)
        .limit(taille)
    ).all()

    ids = [entry.id for entry, _n, _d, _c in resultats]
    deja_cp_ids = _deja_contre_passees(db, ids)
    entries = [
        EcritureAvecTotaux(
            entry=entry,
            nb_lignes=int(nb or 0),
            total_debit=int(debit or 0),
            total_credit=int(credit or 0),
            deja_contre_passee=entry.id in deja_cp_ids,
        )
        for entry, nb, debit, credit in resultats
    ]
    return entries, total
