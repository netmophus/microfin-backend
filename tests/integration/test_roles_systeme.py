"""Vérifie la protection des rôles système (§4 — is_system, migration 0004)."""

from collections.abc import Generator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.cli.seed_security import executer_seed
from app.core.database import SessionLocal

pytestmark = pytest.mark.integration


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """Session dont la transaction est toujours annulée."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def _creer_role(db: Session, code: str, is_system: bool) -> None:
    db.execute(
        text(
            "INSERT INTO security.roles (code, name, is_system) VALUES (:code, :name, :is_system)"
        ),
        {"code": code, "name": f"Rôle {code}", "is_system": is_system},
    )


def test_un_role_systeme_ne_peut_pas_etre_supprime(session: Session) -> None:
    _creer_role(session, "TEST_SYSTEME", is_system=True)

    with pytest.raises(DBAPIError) as erreur:
        session.execute(text("DELETE FROM security.roles WHERE code = 'TEST_SYSTEME'"))

    assert "TEST_SYSTEME" in str(erreur.value)
    assert "supprim" in str(erreur.value)


def test_un_role_personnalise_reste_supprimable(session: Session) -> None:
    # Le WHEN (OLD.is_system) du trigger : une IMF doit pouvoir supprimer ses propres
    # rôles (permission roles.delete). Sans cette clause, le produit les figerait aussi.
    _creer_role(session, "ROLE_MAISON", is_system=False)

    session.execute(text("DELETE FROM security.roles WHERE code = 'ROLE_MAISON'"))

    restant = session.execute(
        text("SELECT count(*) FROM security.roles WHERE code = 'ROLE_MAISON'")
    ).scalar_one()
    assert restant == 0


# Un cas par rôle plutôt qu'une boucle : le premier DELETE avorte la transaction, et la
# rattraper dans la même exigerait un ROLLBACK — qui emporterait aussi le seed.
@pytest.mark.parametrize("code", ["CAISSIER", "ADMIN_TECHNIQUE", "DIRECTION_GENERALE"])
def test_les_roles_du_seed_sont_proteges(session: Session, code: str) -> None:
    executer_seed(session)

    with pytest.raises(DBAPIError) as erreur:
        session.execute(text("DELETE FROM security.roles WHERE code = :code"), {"code": code})

    assert code in str(erreur.value)


def test_supprimer_tous_les_roles_dun_coup_echoue_aussi(session: Session) -> None:
    # Un DELETE sans WHERE ne doit pas emporter le référentiel : le trigger est
    # FOR EACH ROW, il se déclenche dès la première ligne système rencontrée.
    executer_seed(session)

    with pytest.raises(DBAPIError):
        session.execute(text("DELETE FROM security.roles"))


def test_update_dun_role_systeme_reste_possible_en_base(session: Session) -> None:
    # ÉCART ASSUMÉ (option B) : la base n'interdit PAS l'UPDATE d'un rôle système, sinon
    # le seed ne pourrait plus resynchroniser les définitions entre deux versions du
    # produit. La protection contre l'UPDATE illégitime relève du service Sécurité, qui
    # n'existe pas encore. Ce test documente le trou plutôt que de le laisser croire fermé.
    _creer_role(session, "TEST_SYSTEME", is_system=True)

    session.execute(
        text("UPDATE security.roles SET description = 'resync' WHERE code = 'TEST_SYSTEME'")
    )

    description = session.execute(
        text("SELECT description FROM security.roles WHERE code = 'TEST_SYSTEME'")
    ).scalar_one()
    assert description == "resync"
