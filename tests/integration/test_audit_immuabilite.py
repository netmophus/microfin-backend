"""Vérifie que audit.audit_logs rejette toute modification (§3.2 — immuabilité)."""

import uuid
from collections.abc import Generator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.database import SessionLocal

pytestmark = pytest.mark.integration

# Mars 2026 : la partition audit_logs_2026_03 existe (migration 0001).
OCCURRED_AT = datetime(2026, 3, 15, 10, 0, tzinfo=UTC)


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """Session dont la transaction est toujours annulée.

    Rien n'est jamais committé : un enregistrement d'audit committé serait ensuite
    impossible à supprimer — c'est exactement ce que ces tests vérifient. Les cas
    d'échec laissent de toute façon la transaction avortée, d'où le rollback final.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def _inserer_log(db: Session) -> uuid.UUID:
    """Insère un enregistrement d'audit et renvoie son id."""
    log_id: uuid.UUID = db.execute(
        text(
            "INSERT INTO audit.audit_logs (occurred_at, action, chain_hash) "
            "VALUES (:occurred_at, 'test.immuabilite', :chain_hash) RETURNING id"
        ),
        {"occurred_at": OCCURRED_AT, "chain_hash": "a" * 64},
    ).scalar_one()
    return log_id


def test_insertion_autorisee(session: Session) -> None:
    # Seules la modification et la suppression sont bloquées : le journal doit
    # évidemment pouvoir être alimenté.
    assert _inserer_log(session) is not None


def test_update_rejete(session: Session) -> None:
    log_id = _inserer_log(session)

    with pytest.raises(DBAPIError) as erreur:
        session.execute(
            text("UPDATE audit.audit_logs SET action = 'falsifie' WHERE id = :id"),
            {"id": log_id},
        )

    assert "immuable" in str(erreur.value)


def test_delete_rejete(session: Session) -> None:
    log_id = _inserer_log(session)

    with pytest.raises(DBAPIError) as erreur:
        session.execute(
            text("DELETE FROM audit.audit_logs WHERE id = :id"),
            {"id": log_id},
        )

    assert "immuable" in str(erreur.value)


def test_update_visant_directement_la_partition_est_rejete(session: Session) -> None:
    # Le trigger de ligne est cloné sur chaque partition : court-circuiter la table
    # parente ne contourne pas l'immuabilité.
    log_id = _inserer_log(session)

    with pytest.raises(DBAPIError) as erreur:
        session.execute(
            text("UPDATE audit.audit_logs_2026_03 SET action = 'falsifie' WHERE id = :id"),
            {"id": log_id},
        )

    assert "immuable" in str(erreur.value)


def test_truncate_du_parent_est_rejete(session: Session) -> None:
    # TRUNCATE ne déclenche aucun trigger de ligne : sans garde-fou dédié, il viderait
    # le journal sans que rien ne s'y oppose.
    with pytest.raises(DBAPIError) as erreur:
        session.execute(text("TRUNCATE audit.audit_logs"))

    assert "immuable" in str(erreur.value)


def test_truncate_visant_directement_la_partition_est_rejete(session: Session) -> None:
    # Le trigger TRUNCATE n'est PAS cloné par PostgreSQL : celui-ci ne passe que parce
    # que la migration 0002 le pose explicitement sur chaque partition. C'est le test
    # qui garde cette subtilité — sans lui, une partition entière serait effaçable.
    with pytest.raises(DBAPIError) as erreur:
        session.execute(text("TRUNCATE audit.audit_logs_2026_03"))

    assert "immuable" in str(erreur.value)
