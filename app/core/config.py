"""Configuration applicative, chargée depuis les variables d'environnement / .env."""

from functools import lru_cache
from pathlib import Path

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
    REDIS_URL: str


@lru_cache
def get_settings() -> Settings:
    """Instance unique, mise en cache : le .env n'est lu qu'une fois par process."""
    return Settings()


settings = get_settings()
