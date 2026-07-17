"""Hachage Argon2id et politique de mot de passe (§6 du document de décisions v1.0).

Périmètre : le mot de passe, rien d'autre. Ni JWT, ni session, ni accès base. Les
fonctions sont pures et synchrones ; le service d'authentification les composera.

Règles du §6 tenues ici :

  - Argon2id, paramètres OWASP (memory_cost 19456 KiB, time_cost 2, parallelism 1).
    Jamais bcrypt.
  - Longueur >= 12 et 4 catégories de caractères.
  - Historique des 12 derniers : la RÈGLE est ici (c'est de la vérification Argon2) ;
    la lecture de security.user_passwords_history revient au service d'auth.

NON-EXPOSITION DU MOT DE PASSE EN CLAIR — trois précautions, pas une :

  1. Aucune fonction de ce module ne journalise quoi que ce soit.
  2. Aucun objet de ce module ne porte le mot de passe : ResultatPolitique ne contient
     que des règles violées, donc aucun repr ne peut le révéler.
  3. verifier_mot_de_passe ne lève JAMAIS. Ce n'est pas du confort : le mot de passe est
     un paramètre, donc une variable locale de la frame. Toute exception qui s'échapperait
     d'ici embarquerait cette frame dans sa traceback, et les collecteurs d'erreurs qui
     capturent les locals (Sentry & co.) l'écriraient en clair dans un service tiers. Un
     try/except sur le message ne fermerait pas ce vecteur — ne rien laisser remonter, si.
"""

import secrets
import string
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

# --- Argon2id ----------------------------------------------------------------------

# §6, paramètres OWASP. ATTENTION : ce ne sont PAS les défauts d'argon2-cffi, qui hache
# en m=65536, t=3, p=4. Les passer explicitement n'est donc pas redondant — sans eux, on
# hacherait hors spécification sans que rien ne le signale. Verrouillé par
# test_le_hash_porte_les_parametres_du_paragraphe_6.
MEMORY_COST_KIB = 19456
TIME_COST = 2
PARALLELISM = 1

# type=Type.ID est déjà le défaut de la bibliothèque ; on l'écrit parce que le §6 exige
# Argon2id nommément et qu'un défaut de dépendance n'est pas une garantie.
_hacheur = PasswordHasher(
    memory_cost=MEMORY_COST_KIB,
    time_cost=TIME_COST,
    parallelism=PARALLELISM,
    type=Type.ID,
)

# Hash d'un secret aléatoire tiré à chaque démarrage, que rien ne connaît et que rien ne
# stocke : aucun mot de passe ne peut y correspondre.
#
# À passer à verifier_mot_de_passe quand le compte est introuvable, pour que l'échec coûte
# les mêmes ~22 ms qu'un mot de passe faux sur un compte réel :
#
#     hash_a_verifier = utilisateur.password_hash if utilisateur else HASH_LEURRE
#     if not verifier_mot_de_passe(saisi, hash_a_verifier):
#         ...  # même message, même durée, que le compte existe ou non
#
# Sans lui, « compte inconnu » répond en ~0 ms et « mot de passe faux » en ~22 ms : l'écart
# suffit à moissonner les comptes existants sans jamais réussir un login, donc sans
# déclencher le verrouillage du §6, qui compte les échecs par compte existant.
#
# Le coût est un hachage au chargement du module, une fois par processus.
HASH_LEURRE = _hacheur.hash(secrets.token_urlsafe(32))


def hasher_mot_de_passe(mot_de_passe: str) -> str:
    """Renvoie le hash Argon2id, sel aléatoire inclus.

    Deux appels sur le même mot de passe donnent deux hashs différents : le sel est tiré
    à chaque fois. Ne compare jamais deux hashs entre eux — utilise verifier_mot_de_passe.

    Ne valide pas la politique : c'est le rôle de valider_politique, que le service
    appelle en amont. Séparer les deux permet de re-hacher un mot de passe existant
    (rehachage_necessaire) sans le soumettre à une politique qui aurait durci depuis.
    """
    return _hacheur.hash(mot_de_passe)


def verifier_mot_de_passe(mot_de_passe: str, hash_stocke: str) -> bool:
    """Vérifie un mot de passe contre son hash. Ne lève jamais, ne journalise jamais.

    Face à un hash valide, le temps de réponse ne dépend pas du mot de passe : ~22 ms
    qu'il soit bon ou faux (mesuré). La comparaison d'Argon2 est à temps constant, et
    aucun test préalable n'est fait ici qui la court-circuiterait.

    CE QUE CETTE FONCTION NE PEUT PAS PROTÉGER — à traiter par le service d'auth :
    un hash vide ou illisible fait échouer Argon2 au parsing, donc en ~0 ms (mesuré). Le
    contraste avec les ~22 ms d'un hash valide est un oracle d'énumération. L'appelant ne
    doit donc JAMAIS écrire :

        verifier_mot_de_passe(saisi, u.password_hash if u else "")   # 0 ms => u n'existe pas

    mais toujours faire travailler Argon2 sur un hash réel, y compris quand le compte est
    introuvable — cf. HASH_LEURRE ci-dessous. Aucune précaution prise ici ne peut y
    suppléer : le contraste naît du hash reçu, pas du code de cette fonction.

    False couvre deux cas volontairement confondus : mot de passe faux, et hash stocké
    illisible (corrompu, ou d'un algorithme antérieur). Fail-closed : dans les deux cas
    l'authentification échoue. Au service d'auth de journaliser l'anomalie s'il souhaite
    distinguer — sans le mot de passe.
    """
    try:
        _hacheur.verify(hash_stocke, mot_de_passe)
    except (VerificationError, InvalidHashError):
        # VerifyMismatchError hérite de VerificationError : les deux sont couverts.
        return False
    return True


def rehachage_necessaire(hash_stocke: str) -> bool:
    """Le hash a-t-il été produit avec des paramètres autres que ceux d'aujourd'hui ?

    Prévu pour le jour où les paramètres OWASP durciront : un mot de passe vérifié avec
    succès mais haché à l'ancienne doit être re-haché. L'appelant ne dispose du mot de
    passe en clair qu'à l'instant du login — c'est la seule fenêtre pour le faire :

        if verifier_mot_de_passe(saisi, u.password_hash):
            if rehachage_necessaire(u.password_hash):
                u.password_hash = hasher_mot_de_passe(saisi)

    Un hash illisible renvoie True : le re-hacher est la seule issue, et c'est sans
    risque puisque l'appelant n'arrive ici qu'après une vérification réussie.
    """
    try:
        return _hacheur.check_needs_rehash(hash_stocke)
    except InvalidHashError:
        return True


def mot_de_passe_deja_utilise(mot_de_passe: str, hashs_precedents: Sequence[str]) -> bool:
    """Le mot de passe figure-t-il parmi les hashs fournis ? (§6 — les 12 derniers refusés)

    Fonction pure : c'est la RÈGLE, pas sa plomberie. Le service d'auth lit les 12 derniers
    hashs dans security.user_passwords_history, les passe ici, et insère le nouveau hash.

    Toutes les entrées sont vérifiées, sans court-circuit sur la première correspondance.
    Le cas courant — un mot de passe neuf, donc aucune correspondance — les parcourt de
    toute façon jusqu'au bout : s'arrêter tôt n'économiserait que le cas rare, au prix
    d'un temps de réponse qui trahirait le RANG de la correspondance.
    """
    correspondances = [verifier_mot_de_passe(mot_de_passe, h) for h in hashs_precedents]
    return any(correspondances)


# --- Politique ---------------------------------------------------------------------

# string.punctuation : les 32 signes ASCII. L'espace n'en fait pas partie et ne compte
# donc pas comme caractère spécial. Les lettres accentuées ne comptent pas non plus :
# « é ».islower() est vrai, elles satisfont la règle « minuscule ».
CARACTERES_SPECIAUX = frozenset(string.punctuation)


class RegleMotDePasse(StrEnum):
    """Règle du §6 qu'un mot de passe peut violer.

    Contrat destiné à l'appelant : l'API pourra traduire chaque membre en message
    localisé. On ne rend pas de phrases toutes faites ici — la présentation n'a pas sa
    place dans une règle métier.
    """

    LONGUEUR_MINIMALE = "longueur_minimale"
    MAJUSCULE_REQUISE = "majuscule_requise"
    MINUSCULE_REQUISE = "minuscule_requise"
    CHIFFRE_REQUIS = "chiffre_requis"
    CARACTERE_SPECIAL_REQUIS = "caractere_special_requis"


@dataclass(frozen=True)
class PolitiqueMotDePasse:
    """Seuils de la politique. Les défauts sont ceux du §6.

    Paramétrable pour qu'une IMF puisse durcir sans toucher au code. Reste global pour
    l'instant : seule l'expiration est déjà portée par le rôle (roles.password_expiry_days,
    C9). Aucun maximum de longueur — le §6 n'en fixe pas, et Argon2 n'a pas la troncature
    à 72 octets de bcrypt.
    """

    longueur_minimale: int = 12
    exige_majuscule: bool = True
    exige_minuscule: bool = True
    exige_chiffre: bool = True
    exige_caractere_special: bool = True


POLITIQUE_PAR_DEFAUT = PolitiqueMotDePasse()


@dataclass(frozen=True)
class ResultatPolitique:
    """Verdict de valider_politique.

    Ne porte que les règles violées, jamais le mot de passe : ce type circule vers l'API
    et finit dans des logs et des traces. Rien à y révéler.
    """

    violations: tuple[RegleMotDePasse, ...] = ()

    @property
    def est_conforme(self) -> bool:
        return not self.violations


def valider_politique(
    mot_de_passe: str, politique: PolitiqueMotDePasse = POLITIQUE_PAR_DEFAUT
) -> ResultatPolitique:
    """Confronte un mot de passe à la politique et renvoie TOUTES les règles violées.

    Pas de court-circuit à la première violation, mais pour une raison d'usage et non de
    sécurité : l'utilisateur doit voir tout ce qui manque d'un coup plutôt que de corriger
    un défaut à la fois. Aucun secret stocké n'est comparé ici — le temps d'exécution ne
    révèle rien.
    """
    violations: list[RegleMotDePasse] = []

    if len(mot_de_passe) < politique.longueur_minimale:
        violations.append(RegleMotDePasse.LONGUEUR_MINIMALE)
    if politique.exige_majuscule and not any(c.isupper() for c in mot_de_passe):
        violations.append(RegleMotDePasse.MAJUSCULE_REQUISE)
    if politique.exige_minuscule and not any(c.islower() for c in mot_de_passe):
        violations.append(RegleMotDePasse.MINUSCULE_REQUISE)
    if politique.exige_chiffre and not any(c.isdigit() for c in mot_de_passe):
        violations.append(RegleMotDePasse.CHIFFRE_REQUIS)
    if politique.exige_caractere_special and not any(
        c in CARACTERES_SPECIAUX for c in mot_de_passe
    ):
        violations.append(RegleMotDePasse.CARACTERE_SPECIAL_REQUIS)

    return ResultatPolitique(tuple(violations))
