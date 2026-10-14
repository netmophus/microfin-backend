"""Dépôt à terme (DAT) — calculs PURS : échéance et décomposition d'un retrait anticipé.

Aucune base de données, aucune horloge : l'appelant fournit la JOURNÉE comptable. Les intérêts d'un
DAT se calculent sur le capital placé, sans capitalisation, avec `interets.calculer_montant` (le
même Decimal pur que le moteur périodique) et les QUATRE paramètres figés au contrat du compte
(taux, pénalité, base de jours, règle d'arrondi) — jamais ceux, modifiables, du produit.

Les intérêts s'arrêtent à `maturity_date` : un dénouement tardif (échéance un jour non ouvré,
caisse fermée) ne rapporte rien de plus.
"""

import calendar
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.modules.epargne.interets import calculer_montant

POINTS_DE_BASE = 10_000


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
