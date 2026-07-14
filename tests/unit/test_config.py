"""Vérifie que la configuration se charge bien depuis le .env."""

from app.core.config import Settings, get_settings, settings


def test_env_a_pour_defaut_dev() -> None:
    assert Settings.model_fields["ENV"].default == "dev"


def test_database_url_pointe_sur_psycopg() -> None:
    # Le driver doit être psycopg3 (postgresql+psycopg), pas psycopg2.
    assert settings.DATABASE_URL.startswith("postgresql+psycopg://")


def test_redis_url_est_chargee() -> None:
    assert settings.REDIS_URL.startswith("redis://")


def test_get_settings_est_mis_en_cache() -> None:
    assert get_settings() is get_settings()
