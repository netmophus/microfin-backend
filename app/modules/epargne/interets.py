"""Intérêts d'épargne (E5) — moteur PUR + traitement de masse anti-double-versement.

FRONTIÈRE mécanique / valeurs : la mécanique est ici ; les VALEURS (taux, périodicité, méthode de
calcul du solde, base jours, arrondi, seuil) sont des paramètres PRODUIT provisoires, validés par
l'expert (docs/conformite-comptable.md). Aucune méthode n'est codée « en dur » : le produit choisit.

CALCUL PUR ET REJOUABLE (comme le score KYC) : mêmes entrées -> même franc, en Decimal (jamais de
flottant), archivé dans epargne.interet_calculs avec son détail. Un contrôleur rejoue et retombe
sur le même chiffre.

VERSEMENT = écriture équilibrée D 603 (charge) / C 3111 (dette), + mouvement + solde, dans UNE
transaction (comme un dépôt). Le rapprochement Σ soldes == 3111 reste concordant.

ANTI-DOUBLE-VERSEMENT (le garde-fou sacré) : un compte, une période, un seul calcul —
contrainte UNIQUE(account_id, periode) en base. Le batch est idempotent : relancé (crash, reprise,
double lancement), il saute les comptes déjà traités ; la contrainte est le dernier rempart.

TRAITEMENT DE MASSE sans bloquer : compte par compte, CHACUN dans sa propre courte transaction
(verrou du seul compte, commit, relâche). Les comptes FERMÉS sont ignorés (plus d'intérêts après
clôture) ; un membre SUSPENDU est crédité normalement (c'est son argent).
"""

import calendar
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete
from app.modules.comptabilite.journee import journee_de_reference
from app.modules.epargne.models import InteretCalcul, Product, SavingsAccount, SavingsMovement
from app.modules.epargne.operations import TYPE_INTERET, poser_ecriture_operation

METHODES = ("min_periode", "moyen_quotidien", "fin_periode")

MOIS_PAR_TRIMESTRE = 3
FORMAT_JOUR = "%d/%m/%Y"
# « Ce produit verse ses intérêts <adverbe> » — le vocabulaire de l'agent, pas la valeur stockée.
ADVERBE_PERIODICITE = {
    "mensuelle": "chaque mois",
    "trimestrielle": "chaque trimestre",
    "annuelle": "chaque année",
}
MESSAGE_PERIODE_NON_CLOSE = (
    "Cette période n'est pas encore close : les intérêts ne se versent qu'après la fin de la "
    "période."
)


class PeriodeIncompatibleError(Exception):
    """La période demandée est refusée : mauvaise forme pour la périodicité du produit (longueur
    ou bornes), ou période pas encore close."""


class ProduitInconnuError(Exception):
    """Le produit dont on veut verser les intérêts n'existe pas."""


class DejaVerseError(Exception):
    """Ce compte a déjà un calcul d'intérêts pour cette période : on ne verse pas deux fois."""


class MouvementSansPieceError(Exception):
    """Un mouvement du compte n'a pas de pièce comptable : sa date comptable est inconnue, le
    calcul des intérêts serait faux. C'est une anomalie de données (aucun chemin du code n'en
    crée) : on s'arrête sur CE compte, on ne devine pas une date."""


@dataclass
class RapportInterets:
    traites: int = 0  # comptes traités (calcul archivé)
    credites: int = 0  # comptes ayant reçu un intérêt > 0
    ignores: int = 0  # comptes déjà traités pour la période (idempotence)
    total: int = 0  # total des intérêts versés (XOF)
    en_erreur: list[str] = field(default_factory=list)  # comptes non traités, avec la raison


@dataclass(frozen=True)
class ApercuLigne:
    """Un compte de l'échantillon de prévisualisation : de quoi vérifier « ça a l'air juste »."""

    account_number: str
    produit: str
    taux_bp: int
    methode: str
    base_solde: int
    montant: int


@dataclass
class ApercuInterets:
    """Prévisualisation d'un versement (dry-run) : rien n'est écrit. On MONTRE avant de créer
    de l'argent — total ET détail (taux, méthode, période, échantillon par compte)."""

    periode: str
    debut: date
    fin: date
    jours: int
    comptes_actifs: int = 0  # comptes actifs examinés (diagnostic : « aucun compte » possible)
    comptes_taux_zero: int = 0  # parmi eux, ceux dont le produit est à taux 0 (barème non fixé)
    comptes_a_crediter: int = 0
    total: int = 0
    deja_traites: int = 0
    deja_verse_le: datetime | None = None  # si la période a déjà été versée, quand
    en_erreur: list[str] = field(default_factory=list)  # comptes que le calcul ne peut pas traiter
    echantillon: list[ApercuLigne] = field(default_factory=list)


# --- La période, PURE : dérivée de la périodicité du produit -------------------------


def periode_attendue(periodicite: str, date_reference: date) -> tuple[str, date, date]:
    """Période CALENDAIRE (mois, trimestre ou année civile) qui contient `date_reference`.

    Rend (libellé, début, fin). Le libellé est la clé anti-double-versement : « 2026-03 »
    (mensuelle), « 2026-T1 » (trimestrielle), « 2026 » (annuelle). Pure : l'appelant fournit la
    date (la journée comptable), jamais l'horloge du poste.
    """
    annee, mois = date_reference.year, date_reference.month
    if periodicite == "mensuelle":
        premier, dernier = mois, mois
        libelle = f"{annee}-{mois:02d}"
    elif periodicite == "trimestrielle":
        trimestre = (mois - 1) // MOIS_PAR_TRIMESTRE
        premier = trimestre * MOIS_PAR_TRIMESTRE + 1
        dernier = premier + MOIS_PAR_TRIMESTRE - 1
        libelle = f"{annee}-T{trimestre + 1}"
    elif periodicite == "annuelle":
        premier, dernier = 1, 12
        libelle = f"{annee}"
    else:
        raise ValueError(f"périodicité inconnue : {periodicite}")
    fin = date(annee, dernier, calendar.monthrange(annee, dernier)[1])
    return libelle, date(annee, premier, 1), fin


def derniere_periode_close(periodicite: str, journee: date) -> tuple[str, date, date]:
    """Dernière période ENTIÈREMENT écoulée à la journée comptable `journee` : la période qui
    précède celle où se trouve la journée (la période en cours n'est pas close)."""
    _, debut_courante, _ = periode_attendue(periodicite, journee)
    return periode_attendue(periodicite, debut_courante - timedelta(days=1))


def verifier_periode(periodicite: str, periode: str, debut: date, fin: date) -> None:
    """Refuse une période qui n'est pas EXACTEMENT celle de la périodicité du produit : même
    longueur ET mêmes bornes (un produit annuel ne se verse pas au mois ; 01/03 au 15/03 n'est pas
    un mois). Le libellé doit aussi être le libellé attendu : deux libellés pour les mêmes bornes
    contourneraient l'anti-double-versement (UNIQUE compte + libellé)."""
    attendue = periode_attendue(periodicite, debut)
    if (periode, debut, fin) == attendue:
        return
    libelle, debut_attendu, fin_attendue = attendue
    raise PeriodeIncompatibleError(
        f"Ce produit verse ses intérêts {ADVERBE_PERIODICITE[periodicite]} : la période "
        f"« {libelle} » va du {debut_attendu.strftime(FORMAT_JOUR)} au "
        f"{fin_attendue.strftime(FORMAT_JOUR)}. La période demandée ({periode}, du "
        f"{debut.strftime(FORMAT_JOUR)} au {fin.strftime(FORMAT_JOUR)}) ne correspond pas."
    )


def verifier_periode_close(periodicite: str, debut: date, journee: date) -> None:
    """Refuse une période qui n'est pas ENTIÈREMENT écoulée à la journée comptable `journee`.

    Toute période postérieure à `derniere_periode_close` (dont la période en cours) est refusée.
    Une période antérieure passe ici : si elle a déjà été versée, l'anti-double-versement
    (UNIQUE compte + période) la bloque ; sinon c'est un rattrapage légitime."""
    _, debut_derniere_close, _ = derniere_periode_close(periodicite, journee)
    if debut > debut_derniere_close:
        raise PeriodeIncompatibleError(MESSAGE_PERIODE_NON_CLOSE)


def _produit_et_periode(
    db: Session, produit_id: uuid.UUID, periode: str, debut: date, fin: date
) -> Product:
    produit = db.get(Product, produit_id)
    if produit is None:
        raise ProduitInconnuError("Ce produit d'épargne n'existe pas.")
    verifier_periode(produit.periodicite, periode, debut, fin)
    verifier_periode_close(produit.periodicite, debut, journee_de_reference(db))
    return produit


# --- Le calcul, PUR (aucune base de données) ----------------------------------------


def calculer_montant(
    base_solde: int, taux_bp: int, jours: int, base_jours: int, arrondi: str
) -> int:
    """Intérêt = base x (taux_bp / 10000) x (jours / base_jours), arrondi au franc. Pure, Decimal.

    Jamais négatif. `arrondi` : 'plus_proche' (ROUND_HALF_UP) ou 'plancher' (ROUND_DOWN).
    """
    if base_solde <= 0 or taux_bp <= 0 or jours <= 0 or base_jours <= 0:
        return 0
    brut = (
        Decimal(base_solde)
        * Decimal(taux_bp)
        / Decimal(10000)
        * Decimal(jours)
        / Decimal(base_jours)
    )
    mode = ROUND_HALF_UP if arrondi == "plus_proche" else ROUND_DOWN
    return max(int(brut.quantize(Decimal(1), rounding=mode)), 0)


def calculer_base_solde(
    methode: str,
    solde_initial: int,
    points: list[tuple[date, int]],
    debut: date,
    fin: date,
) -> int:
    """Solde de la période selon la MÉTHODE (choisie par le produit). Pure.

    `solde_initial` : solde à l'entrée de la période. `points` : (jour, solde_après) des mouvements
    DANS [debut, fin], triés. Le solde est une fonction en escalier : solde_initial jusqu'au 1er
    mouvement, puis chaque niveau.
    """
    niveaux = [solde_initial, *[solde for _, solde in points]]
    if methode == "fin_periode":
        return niveaux[-1]
    if methode == "min_periode":
        return min(niveaux)
    if methode == "moyen_quotidien":
        total_jours = (fin - debut).days + 1
        if total_jours <= 0:
            return solde_initial
        cumul = 0
        courant = solde_initial
        debut_segment = debut
        for jour, solde in points:
            cumul += courant * max((jour - debut_segment).days, 0)
            courant = solde
            debut_segment = jour
        cumul += courant * ((fin - debut_segment).days + 1)
        # Base moyenne arrondie à l'entier : rend le montant rejouable depuis base_solde archivé.
        return int(Decimal(cumul) / Decimal(total_jours))
    raise ValueError(f"méthode de calcul du solde inconnue : {methode}")


# --- La reconstruction de la trajectoire depuis les mouvements ----------------------


def _trajectoire(
    db: Session, account_id: uuid.UUID, numero_compte: str, debut: date, fin: date
) -> tuple[int, list[tuple[date, int]]]:
    """Rend (solde à l'entrée de la période, points [(jour, solde)] DANS la période).

    Un mouvement est daté de la journée comptable de sa pièce (`journal_entries.entry_date`),
    jamais de sa date de saisie : un dépôt saisi lundi pour la journée de vendredi compte dès
    vendredi, et un intérêt posté à la fin de la période N mais versé en N+1 est déjà dans le
    solde d'entrée de N+1. Un mouvement sans pièce lève `MouvementSansPieceError`.
    """
    lignes = db.execute(
        text(
            "SELECT je.entry_date AS jour, m.balance_after FROM epargne.movements m "
            "LEFT JOIN comptabilite.journal_entries je ON je.id = m.journal_entry_id "
            "WHERE m.account_id = :a AND (je.entry_date IS NULL OR je.entry_date <= :fin) "
            "ORDER BY je.entry_date, m.created_at, m.id"
        ),
        {"a": account_id, "fin": fin},
    ).all()
    solde_initial = 0
    points: list[tuple[date, int]] = []
    for jour, solde in lignes:
        if jour is None:
            raise MouvementSansPieceError(
                f"mouvement sans pièce comptable sur le compte {numero_compte} : "
                "calcul impossible"
            )
        if jour < debut:
            solde_initial = solde
        else:
            points.append((jour, solde))
    return solde_initial, points


# --- Le versement d'un compte (dans SA transaction) ---------------------------------


def _verser_un(
    db: Session,
    account_id: uuid.UUID,
    *,
    periode: str,
    debut: date,
    fin: date,
    par: uuid.UUID | None,
    contexte: ContexteRequete,
) -> int | None:
    """Calcule et verse l'intérêt d'UN compte pour la période. Rend le montant, ou None si le
    compte n'est plus actif (fermé). Lève DejaVerseError si déjà traité (idempotence)."""
    compte = db.execute(
        select(SavingsAccount)
        .where(SavingsAccount.id == account_id, SavingsAccount.status == "actif")
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if compte is None:
        return None  # fermé entre-temps : ignoré

    deja = db.execute(
        select(InteretCalcul.id).where(
            InteretCalcul.account_id == account_id, InteretCalcul.periode == periode
        )
    ).first()
    if deja is not None:
        raise DejaVerseError(periode)

    produit = db.get(Product, compte.product_id)
    assert produit is not None
    solde_initial, points = _trajectoire(db, account_id, compte.account_number, debut, fin)
    base_solde = calculer_base_solde(
        produit.methode_calcul_solde, solde_initial, points, debut, fin
    )
    jours = (fin - debut).days + 1

    montant = 0
    if base_solde >= produit.solde_minimum_remunere:
        montant = calculer_montant(
            base_solde, produit.taux_bp, jours, produit.base_jours, produit.regle_arrondi
        )

    journal_entry_id: uuid.UUID | None = None
    if montant > 0:
        piece = poser_ecriture_operation(
            db, compte, TYPE_INTERET, montant, par, entry_date=fin, contexte=contexte
        )
        journal_entry_id = piece.id
        db.add(
            SavingsMovement(
                account_id=compte.id,
                sens="credit",
                amount=montant,
                balance_after=compte.balance + montant,
                operation_type="interet",
                journal_entry_id=piece.id,
                date_valeur=piece.entry_date,
                created_by=par,
            )
        )
        compte.balance = compte.balance + montant
        compte.updated_by = par
        db.flush()

    detail: dict[str, Any] = {
        "solde_initial": solde_initial,
        "points": [[jour.isoformat(), solde] for jour, solde in points],
        "methode": produit.methode_calcul_solde,
        "debut": debut.isoformat(),
        "fin": fin.isoformat(),
        "arrondi": produit.regle_arrondi,
    }
    db.add(
        InteretCalcul(
            account_id=account_id,
            periode=periode,
            base_solde=base_solde,
            methode=produit.methode_calcul_solde,
            taux_bp=produit.taux_bp,
            base_jours=produit.base_jours,
            jours=jours,
            montant=montant,
            detail=detail,
            journal_entry_id=journal_entry_id,
            computed_by=par,
        )
    )
    db.flush()
    return montant


def previsualiser_interets(
    db: Session,
    *,
    produit_id: uuid.UUID,
    periode: str,
    debut: date,
    fin: date,
    taille_echantillon: int = 12,
) -> ApercuInterets:
    """Prévisualisation (dry-run) : CALCULE ce qui SERAIT versé, sans RIEN écrire.

    On MONTRE avant de créer de l'argent sur des milliers de comptes. En plus du total, on rend un
    ÉCHANTILLON détaillé (taux, méthode, base, montant par compte) : un responsable financier doit
    pouvoir vérifier « ça a l'air juste » — une erreur de taux se voit dans le détail, pas dans un
    total global. Lecture seule : aucun flush, aucun commit, aucune écriture comptable.

    Anti-double : les comptes DÉJÀ traités pour la période sont comptés à part (`deja_traites`,
    `deja_verse_le`) et EXCLUS du total à verser — l'aperçu ne montre que ce qui reste à faire.
    """
    _produit_et_periode(db, produit_id, periode, debut, fin)
    ids = list(
        db.execute(
            select(SavingsAccount.id).where(
                SavingsAccount.status == "actif", SavingsAccount.product_id == produit_id
            )
        ).scalars()
    )
    jours = (fin - debut).days + 1
    apercu = ApercuInterets(
        periode=periode, debut=debut, fin=fin, jours=jours, comptes_actifs=len(ids)
    )
    for account_id in ids:
        deja = db.execute(
            select(InteretCalcul.computed_at).where(
                InteretCalcul.account_id == account_id, InteretCalcul.periode == periode
            )
        ).scalar_one_or_none()
        if deja is not None:
            apercu.deja_traites += 1
            if apercu.deja_verse_le is None or deja < apercu.deja_verse_le:
                apercu.deja_verse_le = deja
            continue

        compte = db.get(SavingsAccount, account_id)
        assert compte is not None
        produit = db.get(Product, compte.product_id)
        assert produit is not None
        if produit.taux_bp == 0:
            apercu.comptes_taux_zero += 1  # diagnostic : barème non encore fixé
        try:
            solde_initial, points = _trajectoire(db, account_id, compte.account_number, debut, fin)
        except MouvementSansPieceError as erreur:
            apercu.en_erreur.append(str(erreur))
            continue
        base_solde = calculer_base_solde(
            produit.methode_calcul_solde, solde_initial, points, debut, fin
        )
        montant = 0
        if base_solde >= produit.solde_minimum_remunere:
            montant = calculer_montant(
                base_solde, produit.taux_bp, jours, produit.base_jours, produit.regle_arrondi
            )
        if montant > 0:
            apercu.comptes_a_crediter += 1
            apercu.total += montant
            if len(apercu.echantillon) < taille_echantillon:
                apercu.echantillon.append(
                    ApercuLigne(
                        account_number=compte.account_number,
                        produit=produit.name,
                        taux_bp=produit.taux_bp,
                        methode=produit.methode_calcul_solde,
                        base_solde=base_solde,
                        montant=montant,
                    )
                )
    return apercu


def verser_interets(
    db: Session,
    *,
    produit_id: uuid.UUID,
    periode: str,
    debut: date,
    fin: date,
    par: uuid.UUID | None = None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> RapportInterets:
    """Traitement de masse : verse les intérêts de la période aux comptes ACTIFS d'UN produit.

    La période doit être celle de la périodicité du produit (`verifier_periode`), sinon refus.

    Compte par compte, CHACUN committé séparément (ne bloque pas le système, reprend proprement).
    Un compte dont un mouvement n'a pas de pièce comptable est SIGNALÉ (`en_erreur`), pas traité,
    sans bloquer les autres. Idempotent : un compte déjà traité pour la période est sauté
    (anti-double-versement).
    """
    _produit_et_periode(db, produit_id, periode, debut, fin)
    ids = list(
        db.execute(
            select(SavingsAccount.id).where(
                SavingsAccount.status == "actif", SavingsAccount.product_id == produit_id
            )
        ).scalars()
    )
    rapport = RapportInterets()
    for account_id in ids:
        try:
            montant = _verser_un(
                db, account_id, periode=periode, debut=debut, fin=fin, par=par, contexte=contexte
            )
        except DejaVerseError:
            db.rollback()
            rapport.ignores += 1
            continue
        except MouvementSansPieceError as erreur:
            db.rollback()  # ce compte est signalé ; les autres sont traités normalement
            rapport.en_erreur.append(str(erreur))
            continue
        if montant is None:  # fermé entre-temps
            db.rollback()
            rapport.ignores += 1
            continue
        db.commit()  # ce compte est acquis : une reprise ne le retraitera pas
        rapport.traites += 1
        if montant > 0:
            rapport.credites += 1
            rapport.total += montant
    return rapport
