"""Garde-fou de FORME sur la liste de mots lisibles (mots_lisibles.py).

Vérifie la forme, pas le SENS : qu'aucun mot ne soit une grossièreté relève de la relecture
humaine (cf. l'en-tête du module), qu'un test ne peut pas juger. Mais la forme, elle, se
vérifie — et une entrée mal formée (accent, majuscule, doublon) trahirait une modification
faite sans relire les règles.
"""

from app.modules.security.mots_lisibles import MOTS_LISIBLES


def test_tous_les_mots_sont_en_minuscules_ascii() -> None:
    for mot in MOTS_LISIBLES:
        assert mot.isascii(), f"« {mot} » n'est pas ASCII (accent ?)"
        assert mot.islower(), f"« {mot} » n'est pas en minuscules"
        assert mot.isalpha(), f"« {mot} » contient autre chose que des lettres"


def test_aucun_doublon() -> None:
    assert len(set(MOTS_LISIBLES)) == len(MOTS_LISIBLES)


def test_longueur_des_mots_dans_les_bornes() -> None:
    """3 à 6 lettres : au-dessus, la dictée devient pénible ; en dessous, ambigu."""
    for mot in MOTS_LISIBLES:
        assert 3 <= len(mot) <= 8, f"« {mot} » hors bornes de longueur"


def test_la_liste_est_assez_grande_pour_l_entropie() -> None:
    """Un garde-fou minimal : réduire la liste sous ce seuil affaiblirait les mots de passe
    sans que rien ne le signale."""
    assert len(MOTS_LISIBLES) >= 100
