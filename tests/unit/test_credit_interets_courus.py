"""Vérifie le calcul des intérêts courus PONCTUELS (solde anticipé/clôture d'un crédit,
chantier lot A/D).

Unitaire : aucune base, calculer_interets_courus() est pure (Decimal en interne, int en
entrée/sortie). DISTINCT de test_credit_echeancier.py (moteur périodique) : ici, le prorata
jour-par-jour entre deux dates, formule jumelle de epargne.interets.calculer_montant.
"""

from app.modules.credit.echeancier import calculer_interets_courus


class TestFormule:
    def test_cas_connu_calcule_a_la_main(self) -> None:
        """1 000 000 F à 12 % l'an, 15 jours (base 360) : 1 000 000 x 0,12 x 15/360 = 5 000 F
        exactement — aucun arrondi en jeu, valeur ronde vérifiable de tête."""
        interets = calculer_interets_courus(
            capital_restant=1_000_000,
            taux_bp=1200,
            jours=15,
            base_jours=360,
            regle_arrondi="plus_proche",
        )
        assert interets == 5_000

    def test_cas_realiste_verifie_au_franc_pres(self) -> None:
        """2 450 000 F à 15 % l'an, 23 jours (base 360) :
        2 450 000 x 0,15 x 23/360 = 8 452 500 / 360 = 23 479,1666... -> 23 479 F
        (arrondi au plus proche, fraction < 0,5)."""
        interets = calculer_interets_courus(
            capital_restant=2_450_000,
            taux_bp=1500,
            jours=23,
            base_jours=360,
            regle_arrondi="plus_proche",
        )
        assert interets == 23_479


class TestArrondi:
    def test_la_regle_darrondi_change_le_resultat(self) -> None:
        """100 000 F à 18,25 % l'an, 1 jour (base 360) :
        100 000 x 0,1825 x 1/360 = 18 250 / 360 = 50,69444... ->
        51 F au plus proche (ROUND_HALF_UP), 50 F au plancher (ROUND_DOWN)."""
        parametres = {
            "capital_restant": 100_000,
            "taux_bp": 1825,
            "jours": 1,
            "base_jours": 360,
        }
        assert calculer_interets_courus(**parametres, regle_arrondi="plus_proche") == 51
        assert calculer_interets_courus(**parametres, regle_arrondi="plancher") == 50


class TestCasNuls:
    def test_jours_nul_rend_zero(self) -> None:
        assert (
            calculer_interets_courus(
                capital_restant=1_000_000,
                taux_bp=1200,
                jours=0,
                base_jours=360,
                regle_arrondi="plus_proche",
            )
            == 0
        )

    def test_capital_nul_rend_zero(self) -> None:
        assert (
            calculer_interets_courus(
                capital_restant=0,
                taux_bp=1200,
                jours=15,
                base_jours=360,
                regle_arrondi="plus_proche",
            )
            == 0
        )

    def test_taux_nul_rend_zero(self) -> None:
        assert (
            calculer_interets_courus(
                capital_restant=1_000_000,
                taux_bp=0,
                jours=15,
                base_jours=360,
                regle_arrondi="plus_proche",
            )
            == 0
        )

    def test_base_jours_nulle_rend_zero(self) -> None:
        """Garde-fou défensif : ne devrait jamais arriver en pratique (base_jours gelé à 360
        côté produit), mais la fonction reste pure et ne doit jamais diviser par zéro."""
        assert (
            calculer_interets_courus(
                capital_restant=1_000_000,
                taux_bp=1200,
                jours=15,
                base_jours=0,
                regle_arrondi="plus_proche",
            )
            == 0
        )

    def test_valeurs_negatives_rendent_zero(self) -> None:
        """Même discipline que epargne.interets.calculer_montant : aucune entrée négative ne
        devrait survenir (gardée en amont, lot B), mais la fonction reste défensive."""
        assert (
            calculer_interets_courus(
                capital_restant=-1,
                taux_bp=1200,
                jours=15,
                base_jours=360,
                regle_arrondi="plus_proche",
            )
            == 0
        )
