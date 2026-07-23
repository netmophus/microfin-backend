"""Normalisation des téléphones (T2b) — phonenumbers + échappatoire tracée au guichet.

LE CRITÈRE N'EST PAS « le numéro est-il valide ? » MAIS « l'agent peut-il faire son travail ? ».
Un agent bloqué au guichet devant un client qui attend note le numéro sur un papier, et la donnée
n'entre JAMAIS dans le système — pire qu'un numéro imparfait en base.

Donc : REFUS PAR DÉFAUT avec un message qui suppose la faute de frappe (l'agent corrige, 99 %
des cas), MAIS une ÉCHAPPATOIRE tracée pour le 1 % légitime que la bibliothèque ne connaît pas
encore (plan de numérotation récemment changé, cas limite). Un numéro forcé porte normalise=False
et son forçage est audité — donc mesurable (voir l'indicateur % de forcés, Décisionnel).

phonenumbers (port de libphonenumber, Google) connaît les plans UEMOA et les maintient. Le pays
par défaut (settings.PAYS_PAR_DEFAUT) sert à normaliser un numéro saisi SANS indicatif ; un
numéro avec indicatif (+221…, 00221…) est parsé selon le sien.
"""

from dataclasses import dataclass

import phonenumbers

from app.core.config import settings

# Garde-fou du forçage : au moins ce nombre de chiffres. Refuse le charabia sans bloquer un
# numéro légitime (le plus court des mobiles UEMOA fait 8 chiffres).
MIN_CHIFFRES = 6


@dataclass(frozen=True)
class TelephoneNormalise:
    e164: str
    country_code: str | None  # code région ISO (NE, SN…) retenu
    normalise: bool  # False si enregistré par forçage (à re-normaliser un jour)


class TelephoneInvalideError(Exception):
    """Numéro refusé. `forcable` dit si un forçage pourrait le sauver (assez de chiffres, juste
    non reconnu par la bibliothèque) ou non (charabia : à corriger, pas à forcer)."""

    def __init__(self, *, forcable: bool) -> None:
        self.forcable = forcable
        super().__init__("téléphone invalide")


def _chiffres(valeur: str) -> str:
    return "".join(c for c in valeur if c.isdigit())


def normaliser(
    raw: str, *, forcer: bool = False, region_defaut: str | None = None
) -> TelephoneNormalise:
    """Rend l'E.164 d'un numéro, ou lève TelephoneInvalideError. Avec forcer=True, enregistre au
    mieux (normalise=False) plutôt que de bloquer l'agent."""
    region = region_defaut or settings.PAYS_PAR_DEFAUT
    brut = raw.strip()

    try:
        numero = phonenumbers.parse(brut, region)
    except phonenumbers.NumberParseException:
        numero = None

    # Chemin nominal : numéro valide selon la bibliothèque.
    if numero is not None and phonenumbers.is_valid_number(numero):
        return TelephoneNormalise(
            e164=phonenumbers.format_number(numero, phonenumbers.PhoneNumberFormat.E164),
            country_code=phonenumbers.region_code_for_number(numero),
            normalise=True,
        )

    chiffres = _chiffres(brut)

    # Refus par défaut. `forcable` guide l'écran : proposer « enregistrer quand même » seulement
    # si le numéro a la longueur d'un vrai numéro (sinon c'est une faute de frappe à corriger).
    if not forcer:
        raise TelephoneInvalideError(forcable=len(chiffres) >= MIN_CHIFFRES)

    # Forçage : même en forçant, un charabia n'est pas un numéro.
    if len(chiffres) < MIN_CHIFFRES:
        raise TelephoneInvalideError(forcable=False)

    # On enregistre AU MIEUX, jamais on ne perd. phone_raw garde la saisie exacte par ailleurs.
    if numero is not None:
        # Parsable et « possible » sans être « valide » : son E.164 est correct de forme.
        return TelephoneNormalise(
            e164=phonenumbers.format_number(numero, phonenumbers.PhoneNumberFormat.E164),
            country_code=phonenumbers.region_code_for_number(numero),
            normalise=False,
        )
    # Impossible à parser : repli sur chiffres + indicatif du pays par défaut.
    if brut.startswith("+"):
        e164 = "+" + chiffres
    else:
        e164 = f"+{phonenumbers.country_code_for_region(region)}{chiffres}"
    return TelephoneNormalise(e164=e164, country_code=region, normalise=False)
