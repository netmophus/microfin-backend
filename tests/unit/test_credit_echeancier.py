"""Vérifie le calcul de l'échéancier de crédit (CR2, §Crédit point 4).

Unitaire : aucune base, generer_echeancier() est pure (Decimal en interne, int en
entrée/sortie). Couvre les deux méthodes d'amortissement, leur convergence à taux
nul, et les combinaisons de paramètres pathologiques qui doivent échouer proprement
plutôt que produire un capital_restant_du négatif.
"""

import itertools
from decimal import Decimal

import pytest

from app.modules.credit.echeancier import EcheancierImpossibleError, generer_echeancier


def _total_capital(echeances) -> int:
    return sum(e.capital for e in echeances)


class TestCapitalConstant:
    def test_capital_identique_sauf_la_derniere_qui_absorbe_le_reste(self) -> None:
        echeances = generer_echeancier(
            montant=100_000,
            taux_bp=1200,
            duree_echeances=12,
            periodicite="mensuelle",
            methode_amortissement="capital_constant",
            regle_arrondi="plus_proche",
        )

        assert len(echeances) == 12
        capitaux = [e.capital for e in echeances[:-1]]
        assert len(set(capitaux)) == 1
        assert _total_capital(echeances) == 100_000
        assert echeances[-1].capital_restant_du == 0

    def test_interets_et_total_decroissent_car_capital_restant_du_decroit(self) -> None:
        echeances = generer_echeancier(
            montant=100_000,
            taux_bp=1200,
            duree_echeances=12,
            periodicite="mensuelle",
            methode_amortissement="capital_constant",
            regle_arrondi="plus_proche",
        )

        for precedent, suivant in itertools.pairwise(echeances):
            assert suivant.interets <= precedent.interets
            assert suivant.total <= precedent.total
            assert suivant.capital_restant_du < precedent.capital_restant_du


class TestEcheanceConstante:
    def test_total_constant_sauf_la_derniere_qui_absorbe_larrondi(self) -> None:
        echeances = generer_echeancier(
            montant=100_000,
            taux_bp=1200,
            duree_echeances=12,
            periodicite="mensuelle",
            methode_amortissement="echeance_constante",
            regle_arrondi="plus_proche",
        )

        totaux = [e.total for e in echeances[:-1]]
        assert len(set(totaux)) == 1
        assert _total_capital(echeances) == 100_000
        assert echeances[-1].capital_restant_du == 0

    def test_capital_croit_et_interets_decroissent(self) -> None:
        echeances = generer_echeancier(
            montant=100_000,
            taux_bp=1200,
            duree_echeances=12,
            periodicite="mensuelle",
            methode_amortissement="echeance_constante",
            regle_arrondi="plus_proche",
        )

        for precedent, suivant in itertools.pairwise(echeances):
            assert suivant.capital >= precedent.capital
            assert suivant.interets <= precedent.interets


class TestConvergenceEtDeterminisme:
    def test_les_deux_methodes_convergent_a_taux_nul(self) -> None:
        params = {
            "montant": 100_000, "taux_bp": 0, "duree_echeances": 12, "periodicite": "mensuelle"
        }

        capital_constant = generer_echeancier(
            **params, methode_amortissement="capital_constant", regle_arrondi="plus_proche"
        )
        echeance_constante = generer_echeancier(
            **params, methode_amortissement="echeance_constante", regle_arrondi="plus_proche"
        )

        assert capital_constant == echeance_constante

    def test_rejouable_a_lidentique(self) -> None:
        params = {
            "montant": 250_000,
            "taux_bp": 1500,
            "duree_echeances": 18,
            "periodicite": "mensuelle",
            "methode_amortissement": "echeance_constante",
            "regle_arrondi": "plus_proche",
        }

        assert generer_echeancier(**params) == generer_echeancier(**params)

    def test_la_regle_darrondi_change_le_resultat(self) -> None:
        params = {
            "montant": 100_000,
            "taux_bp": 1250,
            "duree_echeances": 7,
            "periodicite": "mensuelle",
            "methode_amortissement": "echeance_constante",
        }

        plus_proche = generer_echeancier(**params, regle_arrondi="plus_proche")
        plancher = generer_echeancier(**params, regle_arrondi="plancher")

        assert plus_proche != plancher
        # Les deux versions restent des échéanciers cohérents : capital total reconstitué.
        assert _total_capital(plus_proche) == 100_000
        assert _total_capital(plancher) == 100_000


class TestCasLimite:
    def test_une_seule_echeance_rembourse_tout_le_capital_avec_linteret(self) -> None:
        echeances = generer_echeancier(
            montant=50_000,
            taux_bp=1200,
            duree_echeances=1,
            periodicite="mensuelle",
            methode_amortissement="echeance_constante",
            regle_arrondi="plus_proche",
        )

        assert len(echeances) == 1
        assert echeances[0].capital == 50_000
        assert echeances[0].capital_restant_du == 0
        assert echeances[0].interets == round(50_000 * Decimal(1200) / Decimal(10000) / 12)


class TestCapitalRestantDuJamaisNegatif:
    @pytest.mark.parametrize(
        "methode", ["capital_constant", "echeance_constante"]
    )
    @pytest.mark.parametrize(
        ("montant", "taux_bp", "duree", "periodicite"),
        [
            (100_000, 0, 12, "mensuelle"),
            (100_000, 1200, 12, "mensuelle"),
            (100_000, 2500, 36, "mensuelle"),
            (1_000_000, 900, 4, "trimestrielle"),
            (7, 1200, 3, "mensuelle"),
        ],
    )
    def test_capital_restant_du_reste_toujours_positif_ou_nul(
        self, methode, montant, taux_bp, duree, periodicite
    ) -> None:
        echeances = generer_echeancier(
            montant=montant,
            taux_bp=taux_bp,
            duree_echeances=duree,
            periodicite=periodicite,
            methode_amortissement=methode,
            regle_arrondi="plus_proche",
        )

        for echeance in echeances:
            assert echeance.capital_restant_du >= 0
            assert echeance.capital >= 0
        assert echeances[-1].capital_restant_du == 0

    def test_montant_trop_faible_pour_la_duree_echoue_proprement_arrondi_plus_proche(
        self,
    ) -> None:
        # montant=5, duree=10 : capital de base arrondit (plus proche) 0.5 -> 1,
        # et 1 x 9 échéances = 9 > 5 : la dernière échéance produirait un capital
        # restant dû négatif. Doit échouer proprement, pas produire un résultat absurde.
        with pytest.raises(EcheancierImpossibleError):
            generer_echeancier(
                montant=5,
                taux_bp=1200,
                duree_echeances=10,
                periodicite="mensuelle",
                methode_amortissement="capital_constant",
                regle_arrondi="plus_proche",
            )

    def test_montant_trop_faible_pour_la_duree_echoue_proprement_arrondi_plancher(
        self,
    ) -> None:
        # Même cas, arrondi plancher : le capital de base arrondit vers zéro directement.
        with pytest.raises(EcheancierImpossibleError):
            generer_echeancier(
                montant=5,
                taux_bp=1200,
                duree_echeances=10,
                periodicite="mensuelle",
                methode_amortissement="capital_constant",
                regle_arrondi="plancher",
            )

    def test_duree_demesuree_face_a_un_montant_modeste_echoue_proprement(self) -> None:
        # montant=100, duree=51 : capital de base arrondit (plus proche) 100/51=1.96 -> 2,
        # et 2 x 50 échéances = 100 >= 100 : aucune marge ne reste pour la dernière échéance.
        with pytest.raises(EcheancierImpossibleError):
            generer_echeancier(
                montant=100,
                taux_bp=1200,
                duree_echeances=51,
                periodicite="mensuelle",
                methode_amortissement="capital_constant",
                regle_arrondi="plus_proche",
            )


class TestParametresInvalides:
    def test_montant_nul_ou_negatif_leve_une_erreur(self) -> None:
        with pytest.raises(EcheancierImpossibleError):
            generer_echeancier(
                montant=0,
                taux_bp=1200,
                duree_echeances=12,
                periodicite="mensuelle",
                methode_amortissement="echeance_constante",
                regle_arrondi="plus_proche",
            )

    def test_duree_nulle_ou_negative_leve_une_erreur(self) -> None:
        with pytest.raises(EcheancierImpossibleError):
            generer_echeancier(
                montant=100_000,
                taux_bp=1200,
                duree_echeances=0,
                periodicite="mensuelle",
                methode_amortissement="echeance_constante",
                regle_arrondi="plus_proche",
            )

    def test_periodicite_inconnue_leve_une_erreur(self) -> None:
        with pytest.raises(EcheancierImpossibleError):
            generer_echeancier(
                montant=100_000,
                taux_bp=1200,
                duree_echeances=12,
                periodicite="hebdomadaire",
                methode_amortissement="echeance_constante",
                regle_arrondi="plus_proche",
            )

    def test_methode_damortissement_inconnue_leve_une_erreur(self) -> None:
        with pytest.raises(EcheancierImpossibleError):
            generer_echeancier(
                montant=100_000,
                taux_bp=1200,
                duree_echeances=12,
                periodicite="mensuelle",
                methode_amortissement="lineaire",
                regle_arrondi="plus_proche",
            )
