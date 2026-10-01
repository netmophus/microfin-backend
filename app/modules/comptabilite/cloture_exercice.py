"""Clôture TECHNIQUE d'un exercice comptable — chantier P1, lot (b1).

Couche MINCE au-dessus du moteur (ecritures.py, jamais modifié) : la clôture pose UNE écriture
qui SOLDE chaque compte de charges (classe 6) et de produits (classe 7) mouvementé sur
l'exercice, avec contrepartie nette au compte 591 (« Excédent ou déficit en instance
d'approbation »), puis fait basculer l'exercice en 'clos'.

RÉSULTAT EN DEUX TEMPS (décision actée, pas une simplification de ma part) : cette clôture
technique s'arrête à 591. Elle ne touche JAMAIS 592 (« de l'exercice ») ni 58 (« report à
nouveau ») — l'affectation du résultat après approbation de l'assemblée générale est un lot
SÉPARÉ (b2), pas encore codé, pas même esquissé ici.

JOURNAL RETENU : OD (Opérations diverses), pas AN. AN (« À-nouveaux ») est seedé pour la
REPRISE des soldes à l'OUVERTURE de l'exercice SUIVANT (report à nouveau, lot b2) — l'écriture
de clôture d'un exercice qui se TERMINE n'est pas une à-nouveaux, c'est une régularisation de
fin de période. Elle suit donc la même voie que la saisie manuelle (ecritures_od.py) : OD.

ORDRE POSTER-PUIS-CLORE (vérifié avant de coder, voir ecritures.py) : l'écriture de clôture est
créée ET VALIDÉE PENDANT que l'exercice est encore 'ouvert' — `creer_brouillon`/`valider`
l'exigent via `exercice_ouvert_pour` / la revérification de `exercice.status`. L'exercice ne
bascule à 'clos' qu'ENSUITE, dans la même transaction, juste avant l'audit. Aucun blocage de
verrou : une seule transaction du début à la fin.

CONCURRENCE : l'exercice est RECHARGÉ SOUS VERROU (FOR UPDATE) en tout début de fonction — deux
clics simultanés sur « Clôturer » ne doivent jamais produire deux écritures de clôture.

BROUILLONS BLOQUANTS (décision actée) : la clôture est refusée tant qu'il reste un brouillon
dans l'exercice, quel que soit son journal — jamais de brouillon orphelin qu'on oublierait de
traiter après coup.

DÉFINITIVE (décision actée) : aucune réouverture, aucune colonne ni fonction pour ça. Une
erreur constatée après clôture se corrige par contre-passation dans l'exercice ouvert courant
(ecritures.contre_passer), jamais en rouvrant l'exercice clos.

LIMITE CONNUE (signalée, pas résolue ici) : si un compte de classe 6/7 mouvementé sur
l'exercice a été verrouillé entre-temps (`comptes.verrouiller_saisie`, is_posting -> FALSE), la
clôture échoue avec le refus normal du moteur (CompteNonSaisissableError) — poser la ligne de
clôture exigerait un compte de saisie actif. Cas rare, traitement manuel (déverrouillage
impossible aujourd'hui — voir comptes.py) laissé à l'IMF ; pas d'échappatoire ajoutée ici.
"""

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.comptabilite import ecritures
from app.modules.comptabilite.ecritures import LigneSaisie
from app.modules.comptabilite.models import Account, Exercice, Journal, JournalEntry, JournalLine

CODE_JOURNAL_CLOTURE = "OD"
COMPTE_RESULTAT_INSTANCE = "591"
CLASSES_A_SOLDER = (6, 7)


class ExerciceDejaClosError(Exception):
    """L'exercice est déjà clos : pas de deuxième clôture, pas de réouverture."""


class BrouillonsBloquantsError(Exception):
    """Des brouillons subsistent dans l'exercice : la clôture est refusée tant qu'ils n'ont été
    ni validés ni supprimés (aucun brouillon orphelin après clôture)."""


class RienAClorerError(Exception):
    """Aucun mouvement de charges ou de produits sur l'exercice : rien à solder. Lève AVANT de
    créer le moindre brouillon — sinon une pièce vide resterait en brouillon, bloquant elle-même
    toute clôture future (voir BrouillonsBloquantsError)."""


class CompteResultatIntrouvableError(Exception):
    """Le compte 591 n'existe pas — plan de comptes incomplet (import RCSFD non joué)."""


class ActionsAudit:
    """Actions d'audit propres à la clôture d'exercice (format module.action)."""

    CLOTUREE = "compta.exercice.cloturee"


@dataclass(frozen=True)
class LigneResultatApercu:
    """Le détail, par compte de classe 6/7 mouvementé, de ce que la clôture va solder."""

    account_number: str
    name: str
    account_class: int
    total_debit: int
    total_credit: int
    side: str  # sens de la ligne de clôture qui soldera ce compte ('D' ou 'C')
    amount: int


@dataclass(frozen=True)
class BrouillonBloquant:
    entry_id: uuid.UUID
    journal_code: str
    entry_date: date
    description: str


@dataclass(frozen=True)
class ApercuCloture:
    """Dry-run : ce que `cloturer_exercice` ferait, sans rien poser ni basculer."""

    exercice: Exercice
    resultat: int
    lignes: list[LigneResultatApercu]
    brouillons_bloquants: list[BrouillonBloquant]

    @property
    def cloturable(self) -> bool:
        return not self.brouillons_bloquants


@dataclass(frozen=True)
class ClotureExecutee:
    entry: JournalEntry
    resultat: int


def lister_exercices(db: Session) -> list[Exercice]:
    """Tous les exercices, du plus récent au plus ancien — jamais supprimés (pas de fonction de
    suppression sur ce modèle), donc toujours la liste complète."""
    return list(db.execute(select(Exercice).order_by(Exercice.date_debut.desc())).scalars())


def _brouillons_bloquants(db: Session, exercice_id: uuid.UUID) -> list[BrouillonBloquant]:
    resultats = db.execute(
        select(JournalEntry, Journal.code)
        .join(Journal, Journal.id == JournalEntry.journal_id)
        .where(JournalEntry.exercice_id == exercice_id, JournalEntry.status == "brouillon")
        .order_by(JournalEntry.entry_date)
    ).all()
    return [
        BrouillonBloquant(
            entry_id=entry.id,
            journal_code=code,
            entry_date=entry.entry_date,
            description=entry.description,
        )
        for entry, code in resultats
    ]


def _mouvements_6_7(db: Session, exercice: Exercice) -> list[tuple[Account, int, int]]:
    """Σdébit/Σcrédit BRUTS par compte de classe 6/7 mouvementé (écritures VALIDÉES de
    l'exercice, bornées par ses dates) — même socle de requête que `rapports._agreger_par_compte`,
    restreint aux classes à solder à la clôture."""
    resultats = db.execute(
        select(
            Account,
            func.coalesce(
                func.sum(case((JournalLine.side == "D", JournalLine.amount), else_=0)), 0
            ),
            func.coalesce(
                func.sum(case((JournalLine.side == "C", JournalLine.amount), else_=0)), 0
            ),
        )
        .join(JournalLine, JournalLine.account_id == Account.id)
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .where(
            Account.account_class.in_(CLASSES_A_SOLDER),
            JournalEntry.status == "validee",
            JournalEntry.entry_date >= exercice.date_debut,
            JournalEntry.entry_date <= exercice.date_fin,
        )
        .group_by(Account.id)
        .order_by(Account.account_number)
    ).all()
    return [(compte, int(debit), int(credit)) for compte, debit, credit in resultats]


def previsualiser_cloture(db: Session, exercice: Exercice) -> ApercuCloture:
    """Dry-run : calcule le résultat et le détail, liste les brouillons bloquants. Ne pose rien."""
    lignes: list[LigneResultatApercu] = []
    resultat = 0
    for compte, debit, credit in _mouvements_6_7(db, exercice):
        resultat += credit - debit
        if debit == credit:
            continue
        lignes.append(
            LigneResultatApercu(
                account_number=compte.account_number,
                name=compte.name,
                account_class=compte.account_class,
                total_debit=debit,
                total_credit=credit,
                side="C" if debit > credit else "D",
                amount=abs(debit - credit),
            )
        )
    return ApercuCloture(
        exercice=exercice,
        resultat=resultat,
        lignes=lignes,
        brouillons_bloquants=_brouillons_bloquants(db, exercice.id),
    )


def cloturer_exercice(
    db: Session,
    exercice: Exercice,
    par: uuid.UUID | None,
    *,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> ClotureExecutee:
    """Clôture TECHNIQUE : refuse si brouillons, pose l'écriture de clôture vers 591, bascule
    l'exercice à 'clos', audite l'acte. Voir le docstring du module pour l'ordre et les décisions.
    """
    exercice_verrou = db.execute(
        select(Exercice).where(Exercice.id == exercice.id).with_for_update()
    ).scalar_one()
    if exercice_verrou.status != "ouvert":
        raise ExerciceDejaClosError(f"l'exercice {exercice_verrou.code} est déjà clos")

    bloquants = _brouillons_bloquants(db, exercice_verrou.id)
    if bloquants:
        aperçu_noms = "; ".join(
            f"« {b.description} » ({b.journal_code}, {b.entry_date})" for b in bloquants[:5]
        )
        suite = "…" if len(bloquants) > 5 else ""
        raise BrouillonsBloquantsError(
            f"{len(bloquants)} brouillon(s) en attente dans l'exercice {exercice_verrou.code} : "
            f"{aperçu_noms}{suite} — validez-les ou supprimez-les avant de clôturer."
        )

    lignes_saisie: list[LigneSaisie] = []
    resultat = 0
    for compte, debit, credit in _mouvements_6_7(db, exercice_verrou):
        resultat += credit - debit
        if debit == credit:
            continue
        lignes_saisie.append(
            LigneSaisie(
                account_id=compte.id,
                side="C" if debit > credit else "D",
                amount=abs(debit - credit),
                label=f"Clôture {exercice_verrou.code} — solde {compte.account_number}",
            )
        )

    if not lignes_saisie:
        raise RienAClorerError(
            f"Aucun mouvement de charges ou de produits n'a été enregistré sur l'exercice "
            f"{exercice_verrou.code} : rien à clôturer."
        )

    compte_591 = db.execute(
        select(Account).where(Account.account_number == COMPTE_RESULTAT_INSTANCE)
    ).scalar_one_or_none()
    if compte_591 is None:
        raise CompteResultatIntrouvableError(
            f"le compte {COMPTE_RESULTAT_INSTANCE} (résultat en instance d'approbation) "
            "n'existe pas — plan de comptes incomplet."
        )

    if resultat > 0:
        lignes_saisie.append(
            LigneSaisie(
                account_id=compte_591.id,
                side="C",
                amount=resultat,
                label=f"Excédent {exercice_verrou.code} en instance d'approbation",
            )
        )
    elif resultat < 0:
        lignes_saisie.append(
            LigneSaisie(
                account_id=compte_591.id,
                side="D",
                amount=-resultat,
                label=f"Déficit {exercice_verrou.code} en instance d'approbation",
            )
        )
    # Si resultat == 0 malgré des lignes 6/7 (compensation exacte, cas limite) : l'écriture
    # s'équilibre déjà entre les comptes 6/7 eux-mêmes, pas de ligne 591 à ajouter.

    journal_id = db.execute(
        select(Journal.id).where(Journal.code == CODE_JOURNAL_CLOTURE)
    ).scalar_one()

    entry = ecritures.creer_brouillon(
        db,
        journal_id=journal_id,
        entry_date=exercice_verrou.date_fin,
        description=f"Clôture de l'exercice {exercice_verrou.code}",
        lignes=lignes_saisie,
        par=par,
    )
    ecritures.valider(db, entry, par, contexte=contexte)

    exercice_verrou.status = "clos"
    exercice_verrou.updated_by = par
    db.flush()

    ecrire_audit(
        db,
        action=ActionsAudit.CLOTUREE,
        contexte=contexte,
        acteur_id=par,
        resource_type="compta.exercice",
        resource_id=exercice_verrou.id,
        new_values={
            "status": "clos",
            "resultat": resultat,
            "entry_number": entry.entry_number,
        },
    )
    return ClotureExecutee(entry=entry, resultat=resultat)
