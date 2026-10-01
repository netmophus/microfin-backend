"""Génération des à-nouveaux à l'ouverture de l'exercice suivant — chantier P1, lot b2b.

Couche MINCE au-dessus du moteur (ecritures.py, jamais modifié) et de `rapports.balance`
(jamais modifié non plus) : reporte le solde de CLÔTURE de chaque compte de BILAN (classes 1 à
5) de l'exercice SOURCE (N) comme solde d'OUVERTURE de l'exercice SUIVANT (N+1), via UNE écriture
dans le journal AN (« À-nouveaux »), datée au premier jour de N+1.

PÉRIMÈTRE : classes 1 à 5 UNIQUEMENT. Les comptes de gestion (classes 6/7) sont EXCLUS — déjà
soldés à zéro par la clôture b1 (`cloture_exercice.py`) ; les reporter échouerait de toute façon
(le moteur refuse un montant <= 0, voir `LigneInvalideError`). Un compte de bilan à solde nul est
exclu pour la même raison, jamais pour une autre.

591 N'EST PAS UN CAS PARTICULIER ICI (décision actée, indépendance b2a/b2b) : c'est un compte de
classe 5 comme un autre, reporté QUEL QUE SOIT L'ÉTAT DE L'AFFECTATION (`affectation_resultat.py`)
du résultat de N. S'il n'a pas encore été affecté, son solde se retrouve simplement en ouverture
de N+1, où il sera soldé plus tard par une affectation posée DANS N+1 — exactement comme b2a le
prévoit déjà pour un résultat approuvé tardivement.

SOURCE DES SOLDES : `rapports.balance(db, date_debut=None, date_fin=N.date_fin,
inclure_sans_mouvement=False)` — confirmé au diagnostic b2 : avec `date_debut=None`, chaque
`solde_cloture` est le cumul de TOUTES les écritures validées depuis l'origine jusqu'à la fin de
N, exactement le solde de clôture recherché (pas un solde de période). `inclure_sans_mouvement`
reste à False : un compte jamais mouvementé a nécessairement un solde nul, inutile de le charger
pour le filtrer ensuite.

EXERCICE SUIVANT : celui dont `date_debut` tombe EXACTEMENT le lendemain de `N.date_fin` — pas
« le prochain exercice chronologiquement », qui pourrait exister avec un TROU (hypothèse
structurellement permise, la contrainte d'exclusion n'empêche que le chevauchement, pas l'écart).
Un trou n'a pas de sens pour des à-nouveaux : ils portent le solde du DERNIER instant de N au
PREMIER instant de ce qui le suit sans interruption.

ÉQUILIBRE VÉRIFIÉ, PAS SUPPOSÉ : la pièce doit s'équilibrer par construction (Σ débit = Σ crédit
sur tous les comptes de bilan, puisque les classes 6/7 sont nulles après b1 et que toute écriture
validée est elle-même équilibrée — l'invariant de la partie double garantit Σ sur TOUS les
comptes = 0, donc Σ sur 1-5 seul = 0 si 6/7 = 0). `generer_a_nouveaux` le revérifie explicitement
avant de poser quoi que ce soit et lève `BilanDesequilibreError` si jamais ce n'était pas le cas
— un signal d'incohérence à ne jamais masquer en reposant une pièce déséquilibrée.

MARQUEUR ANTI-DOUBLE-GÉNÉRATION SUR LE RECEVEUR (N+1), PAS LA SOURCE (N) — voir migration 0053
pour la justification complète : la question posée est « cet exercice a-t-il déjà reçu ses
soldes d'ouverture ? », une propriété de N+1.

LIMITE CONNUE (signalée, pas résolue ici) : si un compte de bilan à solde non nul a été
verrouillé entre-temps (`comptes.verrouiller_saisie`, is_posting -> FALSE) ou désactivé
(is_active -> FALSE), reporter son solde échoue avec le refus normal du moteur
(CompteNonSaisissableError) — même limite que celle déjà documentée dans cloture_exercice.py.
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.comptabilite import ecritures, rapports
from app.modules.comptabilite.ecritures import LigneSaisie
from app.modules.comptabilite.models import Account, Exercice, Journal, JournalEntry

CODE_JOURNAL_A_NOUVEAUX = "AN"
CLASSES_BILAN = (1, 2, 3, 4, 5)


class ExerciceSourceNonClosError(Exception):
    """L'exercice source n'est pas clos : pas de solde de clôture fiable à reporter."""


class ExerciceSuivantIntrouvableError(Exception):
    """Aucun exercice ne commence le lendemain de la fin de l'exercice source — il doit être
    ouvert (acte manuel, séparé) avant de générer ses à-nouveaux."""


class ExerciceSuivantNonOuvertError(Exception):
    """L'exercice suivant existe mais n'est pas 'ouvert' (improbable, mais vérifié)."""


class ANouveauxDejaGeneresError(Exception):
    """L'exercice suivant a déjà reçu ses à-nouveaux — pas une deuxième fois."""


class RienAReporterError(Exception):
    """Aucun compte de bilan à solde non nul sur l'exercice source : rien à reporter."""


class BilanDesequilibreError(Exception):
    """Le bilan de clôture reporté ne s'équilibre pas — incohérence réelle, jamais masquée par
    une pièce déséquilibrée. Ne devrait jamais survenir en usage normal."""


class JournalANIntrouvableError(Exception):
    """Le journal AN n'existe pas — paramétrage incomplet (seed_comptabilite.py non joué)."""


class ActionsAudit:
    """Actions d'audit propres aux à-nouveaux (format module.action)."""

    GENERES = "compta.exercice.a_nouveaux_generes"


@dataclass(frozen=True)
class LigneANouveaux:
    account_number: str
    name: str
    account_class: int
    side: str  # sens de la ligne d'à-nouveaux ('D' ou 'C')
    amount: int


@dataclass(frozen=True)
class ApercuANouveaux:
    """Dry-run : ce que `generer_a_nouveaux` ferait, sans rien poser."""

    exercice_source: Exercice
    exercice_suivant: Exercice | None
    lignes: list[LigneANouveaux]
    total_debit: int
    total_credit: int
    deja_genere: bool

    @property
    def equilibre(self) -> bool:
        return self.total_debit == self.total_credit

    @property
    def generable(self) -> bool:
        return (
            self.exercice_source.status == "clos"
            and self.exercice_suivant is not None
            and self.exercice_suivant.status == "ouvert"
            and not self.deja_genere
            and bool(self.lignes)
            and self.equilibre
        )


@dataclass(frozen=True)
class ANouveauxGeneres:
    entry: JournalEntry
    exercice_suivant: Exercice
    total: int


def exercice_suivant_de(db: Session, exercice_source: Exercice) -> Exercice | None:
    """L'exercice qui commence EXACTEMENT le lendemain de la fin de l'exercice source — voir
    docstring module pour pourquoi un trou n'est pas accepté."""
    lendemain = exercice_source.date_fin + timedelta(days=1)
    return db.execute(
        select(Exercice).where(Exercice.date_debut == lendemain)
    ).scalar_one_or_none()


def _lignes_bilan(db: Session, exercice_source: Exercice) -> list[tuple[Account, str, int]]:
    """Compte, sens et montant de la ligne d'à-nouveaux pour chaque compte de bilan (classes 1-5)
    à solde de clôture non nul — voir `rapports.balance` pour le calcul du solde cumulé."""
    resultat = rapports.balance(
        db, date_debut=None, date_fin=exercice_source.date_fin, inclure_sans_mouvement=False
    )
    lignes: list[tuple[Account, str, int]] = []
    for ligne in resultat.lignes:
        compte = ligne.compte
        # int() : SUM() PostgreSQL sur un BigInteger peut revenir en Decimal (voir rapports.py,
        # non recasté là-bas) — jamais propager autre chose qu'un int sous ce service (moteur,
        # JSON de l'audit).
        solde = int(ligne.solde_cloture)
        if compte.account_class not in CLASSES_BILAN or solde == 0:
            continue
        if solde > 0:
            side, amount = compte.normal_side, solde
        else:
            side = "C" if compte.normal_side == "D" else "D"
            amount = -solde
        lignes.append((compte, side, amount))
    lignes.sort(key=lambda t: t[0].account_number)
    return lignes


def previsualiser_a_nouveaux(db: Session, exercice_source: Exercice) -> ApercuANouveaux:
    """Dry-run : comptes à reporter, totaux, détection de l'exercice suivant et de son état. Ne
    pose rien."""
    exercice_suivant = exercice_suivant_de(db, exercice_source)
    triplets = _lignes_bilan(db, exercice_source)
    lignes = [
        LigneANouveaux(
            account_number=compte.account_number,
            name=compte.name,
            account_class=compte.account_class,
            side=side,
            amount=amount,
        )
        for compte, side, amount in triplets
    ]
    total_debit = sum(ligne.amount for ligne in lignes if ligne.side == "D")
    total_credit = sum(ligne.amount for ligne in lignes if ligne.side == "C")
    deja_genere = (
        exercice_suivant is not None and exercice_suivant.a_nouveaux_generes_at is not None
    )
    return ApercuANouveaux(
        exercice_source=exercice_source,
        exercice_suivant=exercice_suivant,
        lignes=lignes,
        total_debit=total_debit,
        total_credit=total_credit,
        deja_genere=deja_genere,
    )


def generer_a_nouveaux(
    db: Session,
    exercice_source: Exercice,
    par: uuid.UUID | None,
    *,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> ANouveauxGeneres:
    """Génère les à-nouveaux de l'exercice suivant à partir du bilan de clôture de la source.
    Voir le docstring du module pour l'ordre des contrôles et les décisions (périmètre, source
    des soldes, équilibre, emplacement du marqueur)."""
    if exercice_source.status != "clos":
        raise ExerciceSourceNonClosError(f"l'exercice {exercice_source.code} n'est pas clos.")

    exercice_suivant = exercice_suivant_de(db, exercice_source)
    if exercice_suivant is None:
        lendemain = exercice_source.date_fin + timedelta(days=1)
        raise ExerciceSuivantIntrouvableError(
            f"aucun exercice ne commence le {lendemain} : ouvrez l'exercice suivant avant de "
            "générer ses à-nouveaux."
        )

    exercice_suivant_verrou = db.execute(
        select(Exercice).where(Exercice.id == exercice_suivant.id).with_for_update()
    ).scalar_one()
    if exercice_suivant_verrou.status != "ouvert":
        raise ExerciceSuivantNonOuvertError(
            f"l'exercice {exercice_suivant_verrou.code} existe mais n'est pas ouvert "
            f"(statut : {exercice_suivant_verrou.status})."
        )
    if exercice_suivant_verrou.a_nouveaux_generes_at is not None:
        raise ANouveauxDejaGeneresError(
            f"l'exercice {exercice_suivant_verrou.code} a déjà reçu ses à-nouveaux."
        )

    triplets = _lignes_bilan(db, exercice_source)
    if not triplets:
        raise RienAReporterError(
            f"aucun compte de bilan à solde non nul sur l'exercice {exercice_source.code} : "
            "rien à reporter."
        )

    libelle = f"À-nouveaux {exercice_source.code} → {exercice_suivant_verrou.code}"
    lignes_saisie = [
        LigneSaisie(
            account_id=compte.id,
            side=side,
            amount=amount,
            label=f"{libelle} — {compte.account_number}",
        )
        for compte, side, amount in triplets
    ]
    total_debit = sum(ligne.amount for ligne in lignes_saisie if ligne.side == "D")
    total_credit = sum(ligne.amount for ligne in lignes_saisie if ligne.side == "C")
    if total_debit != total_credit:
        raise BilanDesequilibreError(
            f"le bilan de clôture de l'exercice {exercice_source.code} ne s'équilibre pas "
            f"(débit {total_debit} ≠ crédit {total_credit}) — à-nouveaux refusés, vérifiez la "
            "comptabilité de cet exercice avant de réessayer."
        )

    journal_id = db.execute(
        select(Journal.id).where(Journal.code == CODE_JOURNAL_A_NOUVEAUX)
    ).scalar_one_or_none()
    if journal_id is None:
        raise JournalANIntrouvableError(
            "le journal AN (à-nouveaux) n'existe pas — paramétrage incomplet."
        )

    entry = ecritures.creer_brouillon(
        db,
        journal_id=journal_id,
        entry_date=exercice_suivant_verrou.date_debut,
        description=libelle,
        lignes=lignes_saisie,
        par=par,
    )
    ecritures.valider(db, entry, par, contexte=contexte)

    assert entry.posted_at is not None  # valider() le pose toujours avant de rendre la main
    exercice_suivant_verrou.a_nouveaux_generes_at = entry.posted_at
    exercice_suivant_verrou.a_nouveaux_generes_by = par
    db.flush()

    ecrire_audit(
        db,
        action=ActionsAudit.GENERES,
        contexte=contexte,
        acteur_id=par,
        resource_type="compta.exercice",
        resource_id=exercice_suivant_verrou.id,
        new_values={
            "exercice_source": exercice_source.code,
            "nb_comptes": len(lignes_saisie),
            "total": total_debit,
            "entry_number": entry.entry_number,
        },
    )
    return ANouveauxGeneres(
        entry=entry, exercice_suivant=exercice_suivant_verrou, total=total_debit
    )
