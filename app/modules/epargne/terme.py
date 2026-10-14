"""Dépôt à terme (DAT) — calculs PURS : échéance et décomposition d'un retrait anticipé.

Aucune base de données, aucune horloge : l'appelant fournit la JOURNÉE comptable. Les intérêts d'un
DAT se calculent sur le capital placé, sans capitalisation, avec `interets.calculer_montant` (le
même Decimal pur que le moteur périodique) et les QUATRE paramètres figés au contrat du compte
(taux, pénalité, base de jours, règle d'arrondi) — jamais ceux, modifiables, du produit.

Les intérêts s'arrêtent à `maturity_date` : un dénouement tardif (échéance un jour non ouvré,
caisse fermée) ne rapporte rien de plus.
"""

import calendar
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.orm import Session

from app.modules.comptabilite.journee import date_comptable_obligatoire
from app.modules.epargne.interets import calculer_montant
from app.modules.epargne.models import Product

POINTS_DE_BASE = 10_000


class DepotATermeError(Exception):
    """Base des refus d'ouverture d'un dépôt à terme (message métier, en français)."""


class ProduitATermeIntrouvableError(DepotATermeError):
    """Produit inexistant, inactif, ou qui n'est pas un dépôt à terme."""


class DureeInvalideError(DepotATermeError):
    """Durée hors des bornes du produit, ou produit dont les durées ne sont pas paramétrées."""


class CapitalInvalideError(DepotATermeError):
    """Capital nul ou négatif."""


@dataclass(frozen=True)
class ConditionsTerme:
    """Les conditions FIGÉES au contrat : posées une fois à l'ouverture, immuables en base."""

    term_months: int
    term_start_date: date
    maturity_date: date
    principal: int
    taux_bp: int
    penalite_bp: int
    base_jours: int
    regle_arrondi: str


@dataclass(frozen=True)
class SimulationTerme:
    """Ce que l'agent montre au client avant de valider : l'échéance et les intérêts attendus."""

    journee_comptable: date
    maturity_date: date
    jours: int
    interets_a_l_echeance: int
    total_a_l_echeance: int
    taux_bp: int
    penalite_anticipee_bp: int
    duree_min_mois: int
    duree_max_mois: int


def ajouter_mois(depart: date, mois: int) -> date:
    """`depart` + `mois` mois civils, ramené au dernier jour du mois d'arrivée si le quantième n'y
    existe pas (31/01 + 1 mois = 28/02, ou 29/02 d'une année bissextile)."""
    index = depart.year * 12 + (depart.month - 1) + mois
    annee, mois_zero = divmod(index, 12)
    mois_arrivee = mois_zero + 1
    dernier_jour = calendar.monthrange(annee, mois_arrivee)[1]
    return date(annee, mois_arrivee, min(depart.day, dernier_jour))


def jours_courus(debut: date, jusqu_a: date, echeance: date) -> int:
    """Nombre de jours d'intérêts entre `debut` et `jusqu_a`, plafonné à l'échéance : au-delà,
    rien ne court. Jamais négatif."""
    return max((min(jusqu_a, echeance) - debut).days, 0)


def interets_du_terme(
    principal: int, taux_bp: int, jours: int, base_jours: int, arrondi: str
) -> int:
    """Intérêts bruts de `jours` jours sur le capital placé, arrondis au franc."""
    return calculer_montant(principal, taux_bp, jours, base_jours, arrondi)


def penalite_sur_interets(interets_courus: int, penalite_bp: int) -> int:
    """Part des intérêts courus retenue en cas de retrait anticipé, arrondie au franc au plus
    proche. Jamais supérieure aux intérêts courus : le capital n'est JAMAIS entamé."""
    if interets_courus <= 0 or penalite_bp <= 0:
        return 0
    brut = Decimal(interets_courus) * Decimal(penalite_bp) / Decimal(POINTS_DE_BASE)
    return min(int(brut.quantize(Decimal(1), rounding=ROUND_HALF_UP)), interets_courus)


MODE_ECHEANCE = "echeance"
MODE_ANTICIPE = "anticipe"


@dataclass(frozen=True)
class DecompositionDenouement:
    """Ce que le client reçoit en soldant son dépôt à terme `journee`, et pourquoi."""

    mode: str  # MODE_ECHEANCE (journée >= échéance) | MODE_ANTICIPE
    jours: int
    capital: int
    interets_courus: int
    penalite: int  # toujours 0 à l'échéance
    interets_nets: int  # courus moins pénalité : ce qui est réellement versé
    total_paye: int
    message: str


def _francs(montant: int) -> str:
    return f"{montant:,} F".replace(",", " ")


def decomposer_denouement(
    *,
    principal: int,
    debut: date,
    echeance: date,
    taux_bp: int,
    penalite_bp: int,
    base_jours: int,
    arrondi: str,
    journee: date,
) -> DecompositionDenouement:
    """Décompose le solde d'un DAT à la journée comptable `journee`, avec les paramètres FIGÉS
    au contrat. À l'échéance (ou après) : intérêts de toute la durée, sans pénalité, et rien de
    plus passé l'échéance. Avant : intérêts courus au prorata, diminués de la pénalité (jamais
    plus que les intérêts : le capital reste intact)."""
    mode = MODE_ECHEANCE if journee >= echeance else MODE_ANTICIPE
    jours = jours_courus(debut, journee, echeance)
    courus = interets_du_terme(principal, taux_bp, jours, base_jours, arrondi)
    penalite = penalite_sur_interets(courus, penalite_bp) if mode == MODE_ANTICIPE else 0
    nets = courus - penalite
    total = principal + nets
    if mode == MODE_ECHEANCE:
        message = (
            f"Dénouement à l'échéance : capital {_francs(principal)} + intérêts "
            f"{_francs(nets)} = {_francs(total)}."
        )
    else:
        retenue = f", moins la pénalité de {_francs(penalite)}" if penalite else ""
        message = (
            f"Retrait anticipé : capital {_francs(principal)} + intérêts courus "
            f"{_francs(courus)}{retenue} = {_francs(total)}."
        )
    return DecompositionDenouement(
        mode=mode, jours=jours, capital=principal, interets_courus=courus, penalite=penalite,
        interets_nets=nets, total_paye=total, message=message,
    )


def conditions_du_contrat(
    db: Session, product_id: uuid.UUID, term_months: int, montant: int
) -> tuple[Product, ConditionsTerme]:
    """Valide la demande d'ouverture et fige les conditions du contrat.

    La journée de départ est la JOURNÉE comptable ouverte (jamais la date du poste) ; sans journée
    ouverte, `AucuneJourneeOuverteError` remonte. Les quatre paramètres de calcul sont COPIÉS du
    produit à cet instant : ils ne bougeront plus pour ce compte."""
    produit = db.get(Product, product_id)
    if produit is None or not produit.is_active or produit.type != "terme":
        raise ProduitATermeIntrouvableError(
            "Ce produit n'est pas un dépôt à terme disponible."
        )
    if produit.duree_min_mois is None or produit.duree_max_mois is None:
        raise DureeInvalideError(
            "Les durées de ce dépôt à terme ne sont pas encore paramétrées : "
            "contactez l'administration fonctionnelle."
        )
    if not produit.duree_min_mois <= term_months <= produit.duree_max_mois:
        raise DureeInvalideError(
            f"Durée refusée : ce dépôt à terme se place de {produit.duree_min_mois} à "
            f"{produit.duree_max_mois} mois."
        )
    if montant <= 0:
        raise CapitalInvalideError("Montant invalide : saisissez un capital supérieur à zéro.")
    depart = date_comptable_obligatoire(db)
    return produit, ConditionsTerme(
        term_months=term_months,
        term_start_date=depart,
        maturity_date=ajouter_mois(depart, term_months),
        principal=montant,
        taux_bp=produit.taux_bp,
        penalite_bp=produit.penalite_anticipee_bp,
        base_jours=produit.base_jours,
        regle_arrondi=produit.regle_arrondi,
    )


def interets_du_contrat(conditions: ConditionsTerme) -> int:
    """Intérêts bruts d'un placement conduit jusqu'à l'échéance : capital x taux x durée exacte
    (échéance moins départ, en jours), avec les paramètres figés au contrat."""
    jours = (conditions.maturity_date - conditions.term_start_date).days
    return interets_du_terme(
        conditions.principal, conditions.taux_bp, jours, conditions.base_jours,
        conditions.regle_arrondi,
    )


def simuler(
    db: Session, product_id: uuid.UUID, term_months: int, montant: int
) -> SimulationTerme:
    """Échéance et intérêts attendus d'un placement, SANS rien écrire : même validation et même
    calcul que l'ouverture réelle."""
    produit, conditions = conditions_du_contrat(db, product_id, term_months, montant)
    assert produit.duree_min_mois is not None and produit.duree_max_mois is not None
    jours = (conditions.maturity_date - conditions.term_start_date).days
    interets = interets_du_contrat(conditions)
    return SimulationTerme(
        journee_comptable=conditions.term_start_date,
        maturity_date=conditions.maturity_date,
        jours=jours,
        interets_a_l_echeance=interets,
        total_a_l_echeance=conditions.principal + interets,
        taux_bp=conditions.taux_bp,
        penalite_anticipee_bp=conditions.penalite_bp,
        duree_min_mois=produit.duree_min_mois,
        duree_max_mois=produit.duree_max_mois,
    )
