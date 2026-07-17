"""Vérifie le branchement SQLAlchemy : Base, engine et dépendance get_db."""

import pytest
from sqlalchemy import text

from app.core.database import Base, engine, get_db


def test_naming_convention_est_active() -> None:
    assert Base.metadata.naming_convention["pk"] == "pk_%(table_name)s"


# Le garde-fou « aucune table déclarée » vivait ici tant que la fondation ne définissait
# aucun modèle. Le socle Sécurité en déclare 11 : l'assertion « metadata vide » est
# devenue intenable, et son intention — les modèles se mappent, ils ne créent jamais — est
# désormais tenue par tests/integration/test_modeles_socle.py, qui la vérifie contre la
# base réelle plutôt que contre une metadata vide.


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
