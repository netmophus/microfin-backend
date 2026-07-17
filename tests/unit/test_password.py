"""Vérifie le hachage Argon2id et la politique de mot de passe (§6).

Unitaires : aucune base, ces fonctions sont pures.

AUCUN MOT DE PASSE EN DUR. Tous sont fabriqués par _fabriquer(), qui tire ses caractères
avec `secrets`. Un littéral ressemblant à un mot de passe finirait dans l'historique Git
pour toujours, et les scanners de secrets le signaleraient — à raison, on ne peut pas
prouver qu'il n'est utilisé nulle part.
"""

import re
import secrets
import string
from collections.abc import Callable

import pytest
from argon2 import PasswordHasher

from app.modules.security.password import (
    HASH_LEURRE,
    MEMORY_COST_KIB,
    PARALLELISM,
    TIME_COST,
    PolitiqueMotDePasse,
    RegleMotDePasse,
    hasher_mot_de_passe,
    mot_de_passe_deja_utilise,
    rehachage_necessaire,
    valider_politique,
    verifier_mot_de_passe,
)

SPECIAUX = string.punctuation


def _fabriquer(
    longueur: int = 16,
    *,
    majuscule: bool = True,
    minuscule: bool = True,
    chiffre: bool = True,
    special: bool = True,
) -> str:
    """Fabrique un mot de passe portant exactement les catégories demandées.

    Les catégories exclues le sont vraiment : l'alphabet de remplissage ne contient que
    les familles demandées, sinon un caractère tiré au hasard réintroduirait la catégorie
    qu'un test cherche justement à faire manquer.
    """
    familles = [
        famille
        for famille, demandee in (
            (string.ascii_uppercase, majuscule),
            (string.ascii_lowercase, minuscule),
            (string.digits, chiffre),
            (SPECIAUX, special),
        )
        if demandee
    ]
    if not familles:
        raise ValueError("au moins une famille de caractères est nécessaire")

    alphabet = "".join(familles)
    # Un caractère de chaque famille demandée, garanti ; le reste au hasard dans l'alphabet.
    caracteres = [secrets.choice(famille) for famille in familles]
    caracteres += [secrets.choice(alphabet) for _ in range(longueur - len(caracteres))]
    secrets.SystemRandom().shuffle(caracteres)
    return "".join(caracteres)


@pytest.fixture
def mot_de_passe() -> str:
    """Mot de passe conforme au §6, différent à chaque test."""
    return _fabriquer()


@pytest.fixture
def fabriquer() -> Callable[..., str]:
    return _fabriquer


# --- hachage ------------------------------------------------------------------------


def test_le_hash_se_verifie(mot_de_passe: str) -> None:
    assert verifier_mot_de_passe(mot_de_passe, hasher_mot_de_passe(mot_de_passe))


def test_deux_hashs_du_meme_mot_de_passe_different(mot_de_passe: str) -> None:
    # Sel aléatoire : deux hashs identiques trahiraient un sel figé, qui rendrait les
    # tables arc-en-ciel possibles et révélerait que deux comptes partagent un mot de passe.
    premier = hasher_mot_de_passe(mot_de_passe)
    second = hasher_mot_de_passe(mot_de_passe)
    assert premier != second
    assert verifier_mot_de_passe(mot_de_passe, premier)
    assert verifier_mot_de_passe(mot_de_passe, second)


def test_le_hash_ne_contient_pas_le_mot_de_passe(mot_de_passe: str) -> None:
    assert mot_de_passe not in hasher_mot_de_passe(mot_de_passe)


def test_un_mauvais_mot_de_passe_est_rejete(
    mot_de_passe: str, fabriquer: Callable[..., str]
) -> None:
    assert not verifier_mot_de_passe(fabriquer(), hasher_mot_de_passe(mot_de_passe))


def test_la_verification_est_sensible_a_la_casse(mot_de_passe: str) -> None:
    hash_stocke = hasher_mot_de_passe(mot_de_passe)
    assert not verifier_mot_de_passe(mot_de_passe.swapcase(), hash_stocke)


def test_le_hash_porte_les_parametres_du_paragraphe_6(mot_de_passe: str) -> None:
    """Verrouille le §6 contre les défauts de la bibliothèque.

    argon2-cffi hache par défaut en m=65536, t=3, p=4 : sans paramètres explicites, on
    serait hors spécification sans qu'aucune erreur ne le signale. Ce test lit ce que le
    hash déclare vraiment, pas ce que le module prétend.
    """
    hash_stocke = hasher_mot_de_passe(mot_de_passe)
    assert hash_stocke.startswith("$argon2id$"), "le §6 impose Argon2id, jamais argon2i/d"
    parametres = re.search(r"\$m=(\d+),t=(\d+),p=(\d+)\$", hash_stocke)
    assert parametres is not None
    assert (int(parametres[1]), int(parametres[2]), int(parametres[3])) == (
        MEMORY_COST_KIB,
        TIME_COST,
        PARALLELISM,
    )
    assert (MEMORY_COST_KIB, TIME_COST, PARALLELISM) == (19456, 2, 1)


@pytest.mark.parametrize(
    "hash_illisible", ["", "pas-un-hash", "$argon2id$corrompu", "$2b$12$bcrypt"]
)
def test_un_hash_illisible_echoue_sans_lever(mot_de_passe: str, hash_illisible: str) -> None:
    """Fail-closed, et surtout : aucune exception ne s'échappe.

    Le mot de passe est une variable locale de verifier_mot_de_passe ; une exception qui
    remonterait embarquerait la frame — donc le clair — dans sa traceback.
    """
    assert verifier_mot_de_passe(mot_de_passe, hash_illisible) is False


def test_le_hash_leurre_est_un_vrai_hash_argon2id() -> None:
    """Le leurre doit faire travailler Argon2 autant qu'un vrai hash.

    On vérifie sa FORME plutôt que sa durée : un test chronométré serait instable en CI,
    alors que c'est justement d'être un hash Argon2id complet, aux paramètres du §6, qui
    lui garantit le même coût qu'un vrai. Un leurre mal formé échouerait au parsing en
    ~0 ms et rétablirait l'oracle qu'il est censé fermer.
    """
    assert HASH_LEURRE.startswith("$argon2id$")
    assert f"m={MEMORY_COST_KIB},t={TIME_COST},p={PARALLELISM}" in HASH_LEURRE


def test_aucun_mot_de_passe_ne_correspond_au_leurre(
    mot_de_passe: str, fabriquer: Callable[..., str]
) -> None:
    assert not verifier_mot_de_passe(mot_de_passe, HASH_LEURRE)
    assert not verifier_mot_de_passe(fabriquer(), HASH_LEURRE)


# --- rehachage ----------------------------------------------------------------------


def test_un_hash_aux_parametres_courants_na_pas_besoin_de_rehachage(mot_de_passe: str) -> None:
    assert not rehachage_necessaire(hasher_mot_de_passe(mot_de_passe))


def test_un_hash_aux_anciens_parametres_demande_un_rehachage(mot_de_passe: str) -> None:
    # Simule le durcissement futur des paramètres OWASP : un hash produit avec des coûts
    # plus faibles doit être repéré au prochain login réussi.
    ancien = PasswordHasher(memory_cost=8, time_cost=1, parallelism=1).hash(mot_de_passe)
    assert rehachage_necessaire(ancien)
    # Le mot de passe reste vérifiable : c'est ce qui rend le rehachage possible.
    assert verifier_mot_de_passe(mot_de_passe, ancien)


def test_un_hash_illisible_demande_un_rehachage() -> None:
    assert rehachage_necessaire("pas-un-hash")


# --- historique des 12 derniers -----------------------------------------------------


def test_un_mot_de_passe_deja_utilise_est_reconnu(fabriquer: Callable[..., str]) -> None:
    anciens = [fabriquer() for _ in range(12)]
    hashs = [hasher_mot_de_passe(m) for m in anciens]
    assert mot_de_passe_deja_utilise(anciens[0], hashs)
    assert mot_de_passe_deja_utilise(anciens[-1], hashs)


def test_un_mot_de_passe_neuf_nest_pas_dans_lhistorique(fabriquer: Callable[..., str]) -> None:
    hashs = [hasher_mot_de_passe(fabriquer()) for _ in range(12)]
    assert not mot_de_passe_deja_utilise(fabriquer(), hashs)


def test_un_historique_vide_naccepte_rien_par_erreur(mot_de_passe: str) -> None:
    # Cas du premier mot de passe d'un compte : la liste est vide, rien n'est « déjà utilisé ».
    assert not mot_de_passe_deja_utilise(mot_de_passe, [])


def test_lhistorique_ignore_les_hashs_illisibles(mot_de_passe: str) -> None:
    hashs = ["pas-un-hash", hasher_mot_de_passe(mot_de_passe)]
    assert mot_de_passe_deja_utilise(mot_de_passe, hashs)


# --- politique ----------------------------------------------------------------------


def test_un_mot_de_passe_conforme_est_accepte(mot_de_passe: str) -> None:
    resultat = valider_politique(mot_de_passe)
    assert resultat.est_conforme
    assert resultat.violations == ()


@pytest.mark.parametrize(
    ("categorie_absente", "regle_attendue"),
    [
        ("majuscule", RegleMotDePasse.MAJUSCULE_REQUISE),
        ("minuscule", RegleMotDePasse.MINUSCULE_REQUISE),
        ("chiffre", RegleMotDePasse.CHIFFRE_REQUIS),
        ("special", RegleMotDePasse.CARACTERE_SPECIAL_REQUIS),
    ],
)
def test_chaque_categorie_manquante_est_signalee_precisement(
    categorie_absente: str, regle_attendue: RegleMotDePasse, fabriquer: Callable[..., str]
) -> None:
    resultat = valider_politique(fabriquer(**{categorie_absente: False}))
    assert not resultat.est_conforme
    # La règle exacte, et elle seule : l'utilisateur doit savoir CE QUI manque.
    assert resultat.violations == (regle_attendue,)


def test_un_mot_de_passe_trop_court_est_signale(fabriquer: Callable[..., str]) -> None:
    resultat = valider_politique(fabriquer(longueur=11))
    assert resultat.violations == (RegleMotDePasse.LONGUEUR_MINIMALE,)


def test_douze_caracteres_suffisent(fabriquer: Callable[..., str]) -> None:
    # Borne exacte du §6 : « >= 12 », donc 12 passe et 11 échoue.
    assert valider_politique(fabriquer(longueur=12)).est_conforme


def test_toutes_les_violations_sont_rendues_dun_coup(fabriquer: Callable[..., str]) -> None:
    # Un mot de passe qui ne respecte rien : l'utilisateur doit tout voir en une fois,
    # pas corriger un défaut à la fois.
    tout_faux = fabriquer(longueur=1, majuscule=False, chiffre=False, special=False)
    resultat = valider_politique(tout_faux)
    assert set(resultat.violations) == {
        RegleMotDePasse.LONGUEUR_MINIMALE,
        RegleMotDePasse.MAJUSCULE_REQUISE,
        RegleMotDePasse.CHIFFRE_REQUIS,
        RegleMotDePasse.CARACTERE_SPECIAL_REQUIS,
    }


def test_le_resultat_ne_contient_jamais_le_mot_de_passe(fabriquer: Callable[..., str]) -> None:
    # ResultatPolitique circule vers l'API et finit dans les logs et les traces.
    trop_court = fabriquer(longueur=11)
    assert trop_court not in repr(valider_politique(trop_court))


# --- politique configurable ---------------------------------------------------------


def test_une_imf_peut_durcir_la_longueur(fabriquer: Callable[..., str]) -> None:
    exigeante = PolitiqueMotDePasse(longueur_minimale=20)
    conforme_au_defaut = fabriquer(longueur=16)
    assert valider_politique(conforme_au_defaut).est_conforme
    assert valider_politique(conforme_au_defaut, exigeante).violations == (
        RegleMotDePasse.LONGUEUR_MINIMALE,
    )


def test_une_imf_peut_relacher_une_categorie(fabriquer: Callable[..., str]) -> None:
    sans_special = PolitiqueMotDePasse(exige_caractere_special=False)
    mot = fabriquer(special=False)
    assert not valider_politique(mot).est_conforme
    assert valider_politique(mot, sans_special).est_conforme


def test_les_defauts_de_la_politique_sont_ceux_du_paragraphe_6() -> None:
    defaut = PolitiqueMotDePasse()
    assert defaut.longueur_minimale == 12
    assert defaut.exige_majuscule
    assert defaut.exige_minuscule
    assert defaut.exige_chiffre
    assert defaut.exige_caractere_special


def test_la_politique_est_immuable() -> None:
    # frozen : une politique partagée ne doit pas pouvoir être durcie ou relâchée en place
    # par un appelant, à l'insu des autres.
    with pytest.raises(AttributeError):
        PolitiqueMotDePasse().longueur_minimale = 4  # type: ignore[misc]
