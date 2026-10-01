"""Affectation du résultat d'un exercice CLOS, après approbation de l'assemblée générale —
chantier P1, lot b2a.

Couche MINCE au-dessus du moteur (ecritures.py, jamais modifié) : solde le compte 591
(« Excédent ou déficit en instance d'approbation ») vers une ventilation saisie À LA MAIN par le
comptable — réserve générale (5521), réserves facultatives (5522), autres réserves (5523) et
report à nouveau (58). Aucun taux automatique : la somme des 4 montants doit égaler EXACTEMENT
le résultat à affecter, sinon refus (voir `_valider_ventilation`).

RÔLE DE 592 — DÉCIDÉ, PAS DISCUTABLE ICI : 592 (« Excédent ou déficit de l'exercice ») n'est PAS
utilisé. b1 ferme déjà directement vers 591 (décision actée en b1) ; faire transiter
l'affectation par 592 ajouterait une paire de lignes qui s'annulent toujours dans la même pièce
(592 crédité puis débité du même montant), sans rien tracer de plus que ce que 591 trace déjà.
592 reste dormant, comme le journal AN — disponible pour une future refonte de b1 si jamais
souhaitée, mais ça toucherait b1, hors périmètre ici.

BASE DE CALCUL — POINT CRITIQUE : le montant à affecter n'est JAMAIS lu depuis le solde COURANT
de 591 (compte GLOBAL, non scopé par exercice — si deux exercices sont clos avant affectation,
591 cumule les deux résultats). Il est lu depuis LA LIGNE 591 DE LA PIÈCE DE CLÔTURE DE CET
EXERCICE PRÉCIS, retrouvée par recoupement (exercice_id + journal OD + date = date_fin de
l'exercice + une ligne sur 591) — jamais par un solde agrégé. Voir `_piece_de_cloture`.

SIGNE : 591 est credit-normal. b1 l'a CRÉDITÉ d'un excédent (résultat > 0) ou DÉBITÉ d'un déficit
(résultat < 0) — voir cloture_exercice.py. L'affectation fait l'inverse pour le solder à zéro :
- EXCÉDENT (591 créditeur) : on DÉBITE 591, on CRÉDITE la ventilation (réserves et/ou 58).
- DÉFICIT (591 débiteur) : on CRÉDITE 591, on DÉBITE 58 SEUL — jamais les réserves (décision
  actée : on ne ventile pas un déficit en réserves, il n'y a rien à mettre en réserve).

GARDE ANTI-DOUBLE AFFECTATION : `Exercice.resultat_affecte_at` (migration 0052) — NOT NULL veut
dire déjà affecté, refus sinon.

PRÉREQUIS : l'exercice doit être 'clos' (lot b1 fait). Refus sur un exercice encore 'ouvert'.

INDÉPENDANCE b2a/b2b (décision actée) : l'écriture d'affectation n'est PAS datée dans l'exercice
qu'on affecte (il est clos, exercice_ouvert_pour le refuserait) — elle est datée au jour COURANT,
dans l'exercice ouvert À CE MOMENT, quel qu'il soit. Les à-nouveaux (b2b, pas codés ici) restent
une opération séparée, qui peut avoir lieu avant ou après cette affectation.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.comptabilite import ecritures
from app.modules.comptabilite.ecritures import LigneSaisie
from app.modules.comptabilite.models import Account, Exercice, Journal, JournalEntry, JournalLine

CODE_JOURNAL_AFFECTATION = "OD"
COMPTE_RESULTAT_INSTANCE = "591"
COMPTE_RESERVE_GENERALE = "5521"
COMPTE_RESERVES_FACULTATIVES = "5522"
COMPTE_AUTRES_RESERVES = "5523"
COMPTE_REPORT_A_NOUVEAU = "58"


class ExerciceNonClosError(Exception):
    """L'exercice n'est pas (encore) clos : rien à affecter tant que b1 n'a pas eu lieu."""


class ResultatDejaAffecteError(Exception):
    """Le résultat de cet exercice a déjà été affecté — pas de deuxième affectation."""


class RienAAffecterError(Exception):
    """L'exercice est clos mais son résultat de clôture était nul (b1 n'a posté aucune ligne
    591) : rien à affecter."""


class PieceClotureAmbigueError(Exception):
    """Plus d'une pièce correspond aux critères de la clôture de cet exercice — cas qui ne
    devrait jamais survenir en usage normal (voir `_piece_de_cloture`). Refus plutôt que de
    deviner laquelle est la bonne."""


class VentilationIncorrecteError(Exception):
    """La ventilation saisie ne correspond pas au résultat à affecter (somme différente, ou
    réserves non nulles sur un déficit)."""


class CompteAffectationIntrouvableError(Exception):
    """Un des comptes de destination (591/5521/5522/5523/58) n'existe pas — plan incomplet."""


class ActionsAudit:
    """Actions d'audit propres à l'affectation du résultat (format module.action)."""

    AFFECTE = "compta.exercice.resultat_affecte"


@dataclass(frozen=True)
class VentilationResultat:
    """Ventilation saisie par le comptable — montants TOUJOURS positifs ou nuls, jamais signés :
    le sens (débit/crédit) est déduit du signe du résultat, pas de la ventilation elle-même."""

    reserve_generale: int = 0
    reserves_facultatives: int = 0
    autres_reserves: int = 0
    report_a_nouveau: int = 0

    def total(self) -> int:
        return (
            self.reserve_generale
            + self.reserves_facultatives
            + self.autres_reserves
            + self.report_a_nouveau
        )


@dataclass(frozen=True)
class ApercuAffectation:
    """Dry-run : ce qu'il y a à affecter pour cet exercice, sans rien poser."""

    exercice: Exercice
    montant: int | None  # signé (+ excédent, - déficit) ; None si rien à affecter
    deja_affecte: bool

    @property
    def affectable(self) -> bool:
        return self.exercice.status == "clos" and self.montant is not None and not self.deja_affecte


@dataclass(frozen=True)
class AffectationExecutee:
    entry: JournalEntry
    montant: int
    ventilation: VentilationResultat = field(default_factory=VentilationResultat)


def _piece_de_cloture(db: Session, exercice: Exercice) -> JournalLine | None:
    """Retrouve LA ligne 591 de LA pièce de clôture de CET exercice — jamais le solde courant du
    compte (voir docstring module). Recoupement à 4 signaux, tous garantis structurellement par
    cloture_exercice.cloturer_exercice : exercice_id, journal OD, date = date_fin de l'exercice,
    une ligne sur 591. Lève si plus d'un résultat (jamais en usage normal)."""
    resultats = db.execute(
        select(JournalLine)
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .join(Journal, Journal.id == JournalEntry.journal_id)
        .join(Account, Account.id == JournalLine.account_id)
        .where(
            JournalEntry.exercice_id == exercice.id,
            JournalEntry.status == "validee",
            JournalEntry.entry_date == exercice.date_fin,
            Journal.code == CODE_JOURNAL_AFFECTATION,
            Account.account_number == COMPTE_RESULTAT_INSTANCE,
        )
    ).scalars().all()
    if len(resultats) > 1:
        raise PieceClotureAmbigueError(
            f"plusieurs pièces correspondent à la clôture de l'exercice {exercice.code} "
            "(attendu : une seule) — affectation refusée, vérifiez manuellement."
        )
    return resultats[0] if resultats else None


def _montant_a_affecter(ligne: JournalLine) -> int:
    """Signé : + si 591 a été CRÉDITÉ (excédent), - si DÉBITÉ (déficit). Jamais nul : b1 ne pose
    cette ligne QUE si le résultat de clôture était non nul (voir cloture_exercice.py)."""
    return ligne.amount if ligne.side == "C" else -ligne.amount


def previsualiser_affectation(db: Session, exercice: Exercice) -> ApercuAffectation:
    """Dry-run : montant à affecter (ou None si rien), déjà-affecté ou non. Ne pose rien."""
    ligne = _piece_de_cloture(db, exercice)
    montant = _montant_a_affecter(ligne) if ligne is not None else None
    return ApercuAffectation(
        exercice=exercice,
        montant=montant,
        deja_affecte=exercice.resultat_affecte_at is not None,
    )


def _valider_ventilation(ventilation: VentilationResultat, montant: int) -> None:
    for nom, valeur in (
        ("reserve_generale", ventilation.reserve_generale),
        ("reserves_facultatives", ventilation.reserves_facultatives),
        ("autres_reserves", ventilation.autres_reserves),
        ("report_a_nouveau", ventilation.report_a_nouveau),
    ):
        if valeur < 0:
            raise VentilationIncorrecteError(f"le montant « {nom} » ne peut pas être négatif.")

    if montant > 0:
        if ventilation.total() != montant:
            raise VentilationIncorrecteError(
                f"la ventilation totalise {ventilation.total()} ; elle doit égaler exactement "
                f"l'excédent à affecter ({montant})."
            )
    else:
        if (
            ventilation.reserve_generale
            or ventilation.reserves_facultatives
            or ventilation.autres_reserves
        ):
            raise VentilationIncorrecteError(
                "un déficit ne se ventile pas en réserves : seul le report à nouveau est "
                "autorisé."
            )
        if ventilation.report_a_nouveau != -montant:
            raise VentilationIncorrecteError(
                f"le report à nouveau saisi ({ventilation.report_a_nouveau}) doit égaler "
                f"exactement le déficit à affecter ({-montant})."
            )


def _compte(db: Session, numero: str) -> Account:
    compte = db.execute(
        select(Account).where(Account.account_number == numero)
    ).scalar_one_or_none()
    if compte is None:
        raise CompteAffectationIntrouvableError(
            f"le compte {numero} n'existe pas — plan de comptes incomplet."
        )
    return compte


def affecter_resultat(
    db: Session,
    exercice: Exercice,
    ventilation: VentilationResultat,
    par: uuid.UUID | None,
    *,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> AffectationExecutee:
    """Affecte le résultat de clôture de cet exercice : valide la ventilation, pose l'écriture
    (journal OD, date du jour), marque l'exercice affecté, audite. Voir le docstring du module
    pour l'ordre des contrôles et les décisions (591 seul, pas de 592 ; base de calcul ; signe)."""
    exercice_verrou = db.execute(
        select(Exercice).where(Exercice.id == exercice.id).with_for_update()
    ).scalar_one()
    if exercice_verrou.status != "clos":
        raise ExerciceNonClosError(f"l'exercice {exercice_verrou.code} n'est pas clos.")
    if exercice_verrou.resultat_affecte_at is not None:
        raise ResultatDejaAffecteError(
            f"le résultat de l'exercice {exercice_verrou.code} a déjà été affecté."
        )

    ligne_591 = _piece_de_cloture(db, exercice_verrou)
    if ligne_591 is None:
        raise RienAAffecterError(
            f"le résultat de clôture de l'exercice {exercice_verrou.code} était nul : rien à "
            "affecter."
        )
    montant = _montant_a_affecter(ligne_591)
    _valider_ventilation(ventilation, montant)

    compte_591 = _compte(db, COMPTE_RESULTAT_INSTANCE)
    lignes_saisie: list[LigneSaisie] = []
    libelle = f"Affectation du résultat {exercice_verrou.code}"

    if montant > 0:
        lignes_saisie.append(
            LigneSaisie(account_id=compte_591.id, side="D", amount=montant, label=libelle)
        )
        for numero, valeur in (
            (COMPTE_RESERVE_GENERALE, ventilation.reserve_generale),
            (COMPTE_RESERVES_FACULTATIVES, ventilation.reserves_facultatives),
            (COMPTE_AUTRES_RESERVES, ventilation.autres_reserves),
            (COMPTE_REPORT_A_NOUVEAU, ventilation.report_a_nouveau),
        ):
            if valeur > 0:
                lignes_saisie.append(
                    LigneSaisie(
                        account_id=_compte(db, numero).id, side="C", amount=valeur, label=libelle
                    )
                )
    else:
        compte_ran = _compte(db, COMPTE_REPORT_A_NOUVEAU)
        lignes_saisie.append(
            LigneSaisie(account_id=compte_ran.id, side="D", amount=-montant, label=libelle)
        )
        lignes_saisie.append(
            LigneSaisie(account_id=compte_591.id, side="C", amount=-montant, label=libelle)
        )

    journal_id = db.execute(
        select(Journal.id).where(Journal.code == CODE_JOURNAL_AFFECTATION)
    ).scalar_one()
    jour = db.execute(text("SELECT CURRENT_DATE")).scalar_one()

    entry = ecritures.creer_brouillon(
        db,
        journal_id=journal_id,
        entry_date=jour,
        description=libelle,
        lignes=lignes_saisie,
        par=par,
    )
    ecritures.valider(db, entry, par, contexte=contexte)

    maintenant: datetime = db.execute(text("SELECT NOW()")).scalar_one()
    exercice_verrou.resultat_affecte_at = maintenant
    exercice_verrou.resultat_affecte_by = par
    db.flush()

    ecrire_audit(
        db,
        action=ActionsAudit.AFFECTE,
        contexte=contexte,
        acteur_id=par,
        resource_type="compta.exercice",
        resource_id=exercice_verrou.id,
        new_values={
            "montant": montant,
            "ventilation": {
                "reserve_generale": ventilation.reserve_generale,
                "reserves_facultatives": ventilation.reserves_facultatives,
                "autres_reserves": ventilation.autres_reserves,
                "report_a_nouveau": ventilation.report_a_nouveau,
            },
            "entry_number": entry.entry_number,
        },
    )
    return AffectationExecutee(entry=entry, montant=montant, ventilation=ventilation)
