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

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete
from app.modules.epargne.models import InteretCalcul, Product, SavingsAccount, SavingsMovement
from app.modules.epargne.operations import TYPE_INTERET, poser_ecriture_operation

METHODES = ("min_periode", "moyen_quotidien", "fin_periode")


class DejaVerseError(Exception):
    """Ce compte a déjà un calcul d'intérêts pour cette période : on ne verse pas deux fois."""


@dataclass
class RapportInterets:
    traites: int = 0  # comptes traités (calcul archivé)
    credites: int = 0  # comptes ayant reçu un intérêt > 0
    ignores: int = 0  # comptes déjà traités pour la période (idempotence)
    total: int = 0  # total des intérêts versés (XOF)


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
    db: Session, account_id: uuid.UUID, debut: date, fin: date
) -> tuple[int, list[tuple[date, int]]]:
    """Rend (solde à l'entrée de la période, points [(jour, solde)] DANS la période)."""
    lignes = db.execute(
        text(
            "SELECT created_at::date AS jour, balance_after FROM epargne.movements "
            "WHERE account_id = :a AND created_at::date <= :fin "
            "ORDER BY created_at, id"
        ),
        {"a": account_id, "fin": fin},
    ).all()
    solde_initial = 0
    points: list[tuple[date, int]] = []
    for jour, solde in lignes:
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
    solde_initial, points = _trajectoire(db, account_id, debut, fin)
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


def verser_interets(
    db: Session,
    *,
    periode: str,
    debut: date,
    fin: date,
    par: uuid.UUID | None = None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> RapportInterets:
    """Traitement de masse : verse les intérêts de la période à tous les comptes ACTIFS.

    Compte par compte, CHACUN committé séparément (ne bloque pas le système, reprend proprement).
    Idempotent : un compte déjà traité pour la période est sauté (anti-double-versement).
    """
    ids = list(
        db.execute(
            select(SavingsAccount.id).where(SavingsAccount.status == "actif")
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
