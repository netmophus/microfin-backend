"""Vérifie le branchement SQLAlchemy : Base, engine et dépendance get_db."""

import pytest
from sqlalchemy import text

from app.core.database import Base, engine, get_db


def test_naming_convention_est_active() -> None:
    assert Base.metadata.naming_convention["pk"] == "pk_%(table_name)s"


def test_aucune_table_metier_declaree() -> None:
    # Garde-fou de cette étape : la fondation ne définit encore aucun modèle.
    assert dict(Base.metadata.tables) == {}


@pytest.mark.integration
def test_connexion_postgres() -> None:
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT 1")) == 1


@pytest.mark.integration
def test_get_db_fournit_une_session_utilisable() -> None:
    generator = get_db()
    db = next(generator)
    try:
        assert db.scalar(text("SELECT 1")) == 1
    finally:
        generator.close()
