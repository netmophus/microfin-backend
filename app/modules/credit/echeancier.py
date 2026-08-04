"""Échéancier de crédit — calcul pur, sans persistance ni écriture comptable (CR2).

Les deux méthodes d'amortissement (capital constant / échéance constante) sont
équivalentes à taux_bp=0 : c'est volontaire, voir docs/conformite-credit.md.
La convention de taux périodique est proportionnelle simple (taux_bp / 10000 / nb
périodes par an), pas actuarielle — un choix mécanique assumé, pas une donnée
réglementaire.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, localcontext

PERIODES_PAR_AN = {"mensuelle": 12, "trimestrielle": 4, "annuelle": 1}


class EcheancierImpossibleError(Exception):
    """Combinaison de paramètres qui ne produit aucun échéancier économiquement cohérent."""


@dataclass(frozen=True)
class Echeance:
    numero: int
    capital: int
    interets: int
    total: int
    capital_restant_du: int


def _arrondir(valeur: Decimal, regle_arrondi: str) -> int:
    mode = ROUND_HALF_UP if regle_arrondi == "plus_proche" else ROUND_DOWN
    return int(valeur.quantize(Decimal(1), rounding=mode))


def generer_echeancier(
    *,
    montant: int,
    taux_bp: int,
    duree_echeances: int,
    periodicite: str,
    methode_amortissement: str,
    regle_arrondi: str,
) -> list[Echeance]:
    """Calcule l'échéancier théorique d'un crédit à échéances fixes. Pure, sans effet de bord.

    Lève EcheancierImpossibleError si la combinaison de paramètres ne permet pas de
    produire un échéancier où capital_restant_du reste toujours >= 0 — plutôt que de
    produire un résultat qui n'a pas de sens économique.
    """
    if montant <= 0:
        raise EcheancierImpossibleError("Le montant doit être positif.")
    if duree_echeances <= 0:
        raise EcheancierImpossibleError("La durée (en échéances) doit être positive.")
    if periodicite not in PERIODES_PAR_AN:
        raise EcheancierImpossibleError(f"Périodicité inconnue : {periodicite}.")
    if methode_amortissement not in ("capital_constant", "echeance_constante"):
        raise EcheancierImpossibleError(
            f"Méthode d'amortissement inconnue : {methode_amortissement}."
        )

    with localcontext() as ctx:
        ctx.prec = 28
        taux_periode = (
            Decimal(taux_bp) / Decimal(10000) / Decimal(PERIODES_PAR_AN[periodicite])
        )

        if methode_amortissement == "capital_constant":
            return _generer_capital_constant(montant, taux_periode, duree_echeances, regle_arrondi)
        return _generer_echeance_constante(montant, taux_periode, duree_echeances, regle_arrondi)


def _generer_capital_constant(
    montant: int, taux_periode: Decimal, duree_echeances: int, regle_arrondi: str
) -> list[Echeance]:
    capital_base = _arrondir(Decimal(montant) / Decimal(duree_echeances), regle_arrondi)
    if capital_base < 1:
        raise EcheancierImpossibleError(
            "Montant trop faible pour la durée demandée : le capital par échéance "
            "arrondirait à zéro."
        )
    if capital_base * (duree_echeances - 1) >= montant:
        raise EcheancierImpossibleError(
            "Montant trop faible pour la durée demandée : la dernière échéance "
            "produirait un capital restant dû négatif ou nul."
        )

    echeances: list[Echeance] = []
    capital_restant = montant
    for numero in range(1, duree_echeances + 1):
        interets = _arrondir(Decimal(capital_restant) * taux_periode, regle_arrondi)
        capital = capital_base if numero < duree_echeances else capital_restant
        capital_restant -= capital
        if capital_restant < 0:
            raise EcheancierImpossibleError(
                "Le calcul produirait un capital restant dû négatif : "
                "combinaison de paramètres incohérente."
            )
        echeances.append(
            Echeance(
                numero=numero,
                capital=capital,
                interets=interets,
                total=capital + interets,
                capital_restant_du=capital_restant,
            )
        )
    return echeances


def _generer_echeance_constante(
    montant: int, taux_periode: Decimal, duree_echeances: int, regle_arrondi: str
) -> list[Echeance]:
    if taux_periode == 0:
        mensualite_brute = Decimal(montant) / Decimal(duree_echeances)
    else:
        facteur = (Decimal(1) + taux_periode) ** -duree_echeances
        mensualite_brute = Decimal(montant) * taux_periode / (Decimal(1) - facteur)
    mensualite = _arrondir(mensualite_brute, regle_arrondi)

    echeances: list[Echeance] = []
    capital_restant = montant
    for numero in range(1, duree_echeances + 1):
        interets = _arrondir(Decimal(capital_restant) * taux_periode, regle_arrondi)
        if numero < duree_echeances:
            capital = mensualite - interets
            if capital < 1:
                raise EcheancierImpossibleError(
                    "Mensualité insuffisante pour couvrir ne serait-ce que le capital "
                    "minimal : combinaison taux/montant/durée incohérente."
                )
        else:
            capital = capital_restant
        capital_restant -= capital
        if capital_restant < 0:
            raise EcheancierImpossibleError(
                "Le calcul produirait un capital restant dû négatif : "
                "combinaison de paramètres incohérente."
            )
        echeances.append(
            Echeance(
                numero=numero,
                capital=capital,
                interets=interets,
                total=capital + interets,
                capital_restant_du=capital_restant,
            )
        )
    return echeances
