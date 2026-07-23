"""Configuration applicative, chargée depuis les variables d'environnement / .env."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# app/core/config.py -> app/core -> app -> microfinance-backend/
BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Paramètres de l'instance. Une institution = un déploiement = un .env."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    ENV: str = "dev"
    DATABASE_URL: str
    # Optionnel TANT QUE le rate limiting par IP n'est pas branché : aucun code ne lit
    # encore Redis. Le rendre obligatoire ferait échouer le démarrage d'une installation
    # pour un service qu'elle n'utilise pas — une contrainte inutile à l'installation. Il
    # redeviendra requis le jour où le rate limiting (chantier post-4c) en dépendra.
    REDIS_URL: str | None = None

    # Clé de signature des jetons (§6). Sans valeur par défaut, délibérément : un défaut
    # serait un secret en dur, et une instance mal configurée démarrerait en signant avec
    # une clé publiquement connue. Ici, elle refuse de démarrer.
    #
    # SecretStr : sa valeur ne s'imprime pas. repr(settings) affiche « SecretStr('**...') »,
    # ce qui la protège des logs de démarrage et des traces d'exception. Lecture explicite
    # par .get_secret_value(), donc jamais par accident.
    #
    # min_length=32 (RFC 7518 §3.2 : la clé HMAC doit valoir au moins la taille du hash,
    # soit 32 octets pour SHA-256). PyJWT accepte une clé d'un seul octet — il se contente
    # d'un InsecureKeyLengthWarning, vérifié sur la 2.13.0. Un avertissement ne protège
    # personne en production : on en fait une erreur de démarrage.
    #
    # DÉPLOIEMENT : chaque installation IMF doit générer SON secret. Deux installations
    # qui partagent la clé rendent leurs jetons interchangeables — un utilisateur de l'une
    # s'authentifierait chez l'autre.
    JWT_SECRET: SecretStr = Field(min_length=32)

    # Un seul algorithme accepté, et il ne vient jamais de l'en-tête du jeton (attaque
    # classique : alg=none, ou RS256 rejoué en HS256 avec la clé publique comme secret).
    #
    # Le §6 demande d'être « prêt pour RS256 ». Literal["HS256"] n'est pas un renoncement :
    # accepter "RS256" ici serait mentir, JWT_SECRET n'étant pas une clé RSA — la config
    # passerait et l'exécution casserait. Le jour venu, le passage coûte ce mot dans le
    # Literal, une clé privée / publique dans _cle_signature() / _cle_verification()
    # (app/modules/security/jwt.py), et cryptography au lock. Aucune restructuration :
    # l'algorithme est déjà lu d'ici et les deux coutures de clé existent déjà.
    JWT_ALGORITHM: Literal["HS256"] = "HS256"

    # Pays d'EXPLOITATION de l'institution (code ISO 3166-1 alpha-2), repère pour normaliser
    # les téléphones saisis SANS indicatif : « 90123456 » -> « +227… » si PAYS_PAR_DEFAUT=NE.
    #
    # DÉPLOIEMENT (checklist) : une IMF sénégalaise doit régler SN, une nigérienne NE. On ne
    # peut PAS le déduire de la nationalité du membre (un Malien à Niamey a un numéro nigérien).
    # Réglage anodin qui casse une fonction majeure EN SILENCE : mal réglé, tous les numéros
    # locaux sont mal normalisés, la recherche par téléphone ne trouve rien et la déduplication
    # (T4) ne voit jamais que deux fiches sont la même personne. Défaut "NE" pour le dev.
    PAYS_PAR_DEFAUT: str = "NE"


@lru_cache
def get_settings() -> Settings:
    """Instance unique, mise en cache : le .env n'est lu qu'une fois par process."""
    return Settings()


settings = get_settings()
