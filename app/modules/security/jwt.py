"""Jetons d'accès et de rafraîchissement (§6 du document de décisions v1.0).

Périmètre : fabriquer et vérifier des jetons. Pas de base, pas de session, pas d'API.
Le stockage hashé du refresh et sa rotation touchent security.user_sessions et relèvent
du bloc suivant ; ici le jeton est un objet autonome, vérifiable hors ligne.

Règles du §6 tenues ici :

  - Access token 15 min, refresh token 8 h (journée de travail).
  - HS256, « prêt pour RS256 » — cf. _cle_signature / _cle_verification plus bas.
  - Claim agency_id : l'agence COURANTE de la session (C6). Un agent multi-agences
    travaille dans une agence à la fois ; primary_agency_id reste son rattachement.

LE JETON EST UNE CRÉDENCE. Un access token volé vaut le compte pendant 15 minutes, un
refresh pendant 8 heures. Aucun message d'exception de ce module ne contient le jeton,
et rien n'y est journalisé. Limite connue, non refermable ici : le jeton est un paramètre
de fonction, donc une variable locale ; un collecteur d'erreurs configuré pour capturer
les locals l'écrirait dans sa traceback. Contrairement au mot de passe (cf. password.py),
on ne peut pas s'en prémunir en ne levant jamais — l'appelant DOIT distinguer expiré,
invalide et mauvais type. À traiter à la configuration du collecteur, pas ici.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal

import jwt
from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.config import settings

# §6. Le refresh couvre une journée de travail : ouvrir sa session le matin suffit.
DUREE_ACCES = timedelta(minutes=15)
DUREE_RAFRAICHISSEMENT = timedelta(hours=8)


class TypeJeton(StrEnum):
    """Valeur du claim « type ». Sépare les deux familles de jetons."""

    ACCES = "access"
    RAFRAICHISSEMENT = "refresh"


# --- erreurs -----------------------------------------------------------------------


class JetonError(Exception):
    """Base des erreurs de jeton. Ne contient jamais le jeton lui-même."""


class JetonExpireError(JetonError):
    """Signature valide, mais exp est dépassé. Cas normal : le client doit rafraîchir."""


class JetonInvalideError(JetonError):
    """Signature fausse, jeton malformé, ou claims non conformes. Cas anormal."""


class TypeDeJetonInvalideError(JetonError):
    """Jeton authentique, mais de la mauvaise famille (refresh présenté comme accès)."""


# --- claims ------------------------------------------------------------------------


class _ClaimsBase(BaseModel):
    """Claims communs aux deux familles.

    frozen : des claims vérifiés ne se modifient pas — les altérer après décodage
    donnerait un objet qui ne correspond plus à aucun jeton signé.

    extra="forbid" : un claim inconnu fait échouer la validation. C'est une seconde
    barrière contre la confusion de familles — la charge d'un access token (roles,
    agences) est refusée par ClaimsRafraichissement avant même l'examen du type.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sub: uuid.UUID
    jti: uuid.UUID
    iat: datetime
    exp: datetime
    # Redéfini par chaque sous-classe en Literal[…] avec sa valeur. Déclaré ici pour que
    # _encoder puisse lire claims.type sans connaître la sous-classe concrète.
    type: TypeJeton


class ClaimsAcces(_ClaimsBase):
    """Claims d'un access token : de quoi autoriser sans retoucher la base."""

    type: Literal[TypeJeton.ACCES] = TypeJeton.ACCES
    # Codes de rôles (roles.code), pas des UUID : lisibles dans le jeton et stables.
    roles: tuple[str, ...]
    # Agence de rattachement (users.primary_agency_id). NULL possible : le socle
    # l'autorise, un administrateur technique n'est rattaché à aucune agence.
    primary_agency_id: uuid.UUID | None
    # C6 — agence COURANTE de la session, celle où l'agent travaille à cet instant.
    # Vaut primary_agency_id par défaut. C'est ce claim qui portera le cloisonnement,
    # pas la colonne scope des permissions.
    agency_id: uuid.UUID | None


class ClaimsRafraichissement(_ClaimsBase):
    """Claims d'un refresh token : le strict minimum.

    Ni rôles ni agences, délibérément. Un refresh vit 8 h ; y figer des rôles ferait
    survivre une habilitation révoquée jusqu'à l'expiration. Ils sont relus en base à
    chaque rafraîchissement, donc toujours à jour.
    """

    type: Literal[TypeJeton.RAFRAICHISSEMENT] = TypeJeton.RAFRAICHISSEMENT


# --- clés ---------------------------------------------------------------------------


def _cle_signature() -> str:
    """Clé qui SIGNE. Avec HS256, identique à celle qui vérifie.

    Couture pour RS256 (§6) : elle renverrait alors la clé PRIVÉE, tandis que
    _cle_verification renverrait la publique. Les deux fonctions existent aujourd'hui
    pour que ce jour-là rien d'autre ne bouge.
    """
    return settings.JWT_SECRET.get_secret_value()


def _cle_verification() -> str:
    """Clé qui VÉRIFIE. Avec HS256, identique à celle qui signe (cf. _cle_signature)."""
    return settings.JWT_SECRET.get_secret_value()


# --- fabrication --------------------------------------------------------------------


def _encoder(claims: _ClaimsBase) -> str:
    """Sérialise et signe. Convertit explicitement vers les types admis par JSON.

    PyJWT refuse un sub de type UUID (« Object of type UUID is not JSON serializable »,
    vérifié sur la 2.13.0) et attend exp/iat en NumericDate, c'est-à-dire un entier de
    secondes Unix — pas une chaîne ISO. D'où cette conversion à la main plutôt qu'un
    model_dump(mode="json"), qui rendrait des dates ISO et produirait un jeton non
    conforme à la RFC 7519.
    """
    charge: dict[str, Any] = {
        "sub": str(claims.sub),
        "jti": str(claims.jti),
        "iat": int(claims.iat.timestamp()),
        "exp": int(claims.exp.timestamp()),
        "type": claims.type.value,
    }
    if isinstance(claims, ClaimsAcces):
        charge["roles"] = list(claims.roles)
        charge["primary_agency_id"] = (
            str(claims.primary_agency_id) if claims.primary_agency_id else None
        )
        charge["agency_id"] = str(claims.agency_id) if claims.agency_id else None

    return jwt.encode(charge, _cle_signature(), algorithm=settings.JWT_ALGORITHM)


def creer_access_token(
    user_id: uuid.UUID,
    roles: Sequence[str],
    primary_agency_id: uuid.UUID | None = None,
    agency_id: uuid.UUID | None = None,
) -> str:
    """Fabrique un access token de 15 min.

    agency_id (C6) est l'agence COURANTE de la session. Omis, il retombe sur
    primary_agency_id : le mono-agence, cas de l'immense majorité des utilisateurs, n'a
    rien à préciser. L'appelant ne le renseigne que lorsqu'un agent multi-agences bascule.

    Aucune vérification que agency_id est habilité pour cet utilisateur : ce module ne
    connaît pas la base. C'est au service d'auth de le confronter à user_agencies avant
    d'appeler — un jeton signé fait foi, il ne doit donc jamais être émis sur un
    périmètre non vérifié.
    """
    emis_a = datetime.now(UTC)
    claims = ClaimsAcces(
        sub=user_id,
        jti=uuid.uuid4(),
        iat=emis_a,
        exp=emis_a + DUREE_ACCES,
        roles=tuple(roles),
        primary_agency_id=primary_agency_id,
        agency_id=agency_id if agency_id is not None else primary_agency_id,
    )
    return _encoder(claims)


def creer_refresh_token(user_id: uuid.UUID) -> str:
    """Fabrique un refresh token de 8 h.

    Son jti identifiera la ligne de security.user_sessions au bloc suivant : c'est lui
    qui rendra la révocation d'une session précise possible.
    """
    emis_a = datetime.now(UTC)
    claims = ClaimsRafraichissement(
        sub=user_id,
        jti=uuid.uuid4(),
        iat=emis_a,
        exp=emis_a + DUREE_RAFRAICHISSEMENT,
    )
    return _encoder(claims)


# --- vérification -------------------------------------------------------------------

# PyJWT n'exige AUCUN claim par défaut : un jeton sans exp se décode sans erreur et
# n'expire jamais (vérifié sur la 2.13.0). Les modèles Pydantic les exigeraient de toute
# façon, mais on le dit aussi à PyJWT — pour que l'absence d'exp échoue comme un jeton
# invalide, et non comme une erreur de validation dans un second temps.
CLAIMS_OBLIGATOIRES = ["sub", "jti", "iat", "exp", "type"]


def _decoder[TClaims: _ClaimsBase](
    jeton: str, type_attendu: TypeJeton, modele: type[TClaims]
) -> TClaims:
    try:
        charge: dict[str, Any] = jwt.decode(
            jeton,
            _cle_verification(),
            # JAMAIS l'algorithme annoncé par l'en-tête du jeton. C'est l'attaque JWT
            # classique : alg=none, ou un RS256 rejoué en HS256 avec la clé publique en
            # guise de secret HMAC. La liste vient de la configuration, point.
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": CLAIMS_OBLIGATOIRES},
        )
    except jwt.ExpiredSignatureError:
        # Sous-classe d'InvalidTokenError : à intercepter en premier, sinon le cas
        # « expiré » serait avalé par le cas « invalide » et le client ne saurait pas
        # qu'il lui suffit de rafraîchir.
        raise JetonExpireError("jeton expiré") from None
    except jwt.InvalidTokenError as erreur:
        # str(erreur) décrit le défaut (« Signature verification failed »), jamais le
        # jeton. On ne chaîne pas : la traceback de PyJWT porte des frames qui, elles,
        # tiennent le jeton.
        raise JetonInvalideError(f"jeton invalide : {erreur}") from None

    # Le contrôle de famille vient avant la validation des claims, pour que « refresh
    # présenté comme accès » donne cette erreur-là plutôt qu'une erreur de schéma.
    if charge.get("type") != type_attendu.value:
        raise TypeDeJetonInvalideError(
            f"type de jeton inattendu : « {type_attendu.value} » attendu"
        )

    try:
        return modele.model_validate(charge)
    except ValidationError as erreur:
        raise JetonInvalideError(
            f"claims non conformes : {erreur.error_count()} erreur(s)"
        ) from None


def decoder_access_token(jeton: str) -> ClaimsAcces:
    """Vérifie signature, expiration, présence des claims ET famille du jeton.

    Refuse un refresh token. La confusion des familles est un classique : sans ce
    contrôle, un refresh — qui vit 8 h et ne porte aucun rôle — passerait pour un accès,
    et l'autorisation s'appuierait sur des rôles absents.
    """
    return _decoder(jeton, TypeJeton.ACCES, ClaimsAcces)


def decoder_refresh_token(jeton: str) -> ClaimsRafraichissement:
    """Vérifie signature, expiration, présence des claims ET famille du jeton.

    Refuse un access token : le rafraîchissement ne doit s'appuyer que sur un jeton
    délivré pour ça, dont le jti correspond à une session en base (bloc suivant).
    """
    return _decoder(jeton, TypeJeton.RAFRAICHISSEMENT, ClaimsRafraichissement)
