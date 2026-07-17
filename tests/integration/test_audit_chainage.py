"""Vérifie le chaînage de audit.audit_logs (§3.2 — chain_hash sous verrou consultatif)."""

import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import SessionLocal

pytestmark = pytest.mark.integration

# Doit rester synchronisé avec AUDIT_CHAIN_LOCK_KEY de la migration 0003.
LOCK_KEY = 20_260_716

# Mars 2026 : la partition audit_logs_2026_03 existe (migration 0001).
OCCURRED_AT = datetime(2026, 3, 15, 10, 0, tzinfo=UTC)

# Identifiant figé : le hash ne peut être comparé d'une exécution à l'autre que si la
# charge est identique — donc pas de gen_random_uuid() dans les tests de reproductibilité.
ID_FIGE = uuid.UUID("00000000-0000-4000-8000-000000000001")


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """Session dont la transaction est toujours annulée (cf. test_audit_immuabilite)."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def _inserer_log(db: Session, log_id: uuid.UUID | None = None) -> Any:
    """Insère un enregistrement sans fournir de chain_hash : c'est le trigger qui le pose."""
    return db.execute(
        text(
            "INSERT INTO audit.audit_logs (id, occurred_at, action) "
            # CAST(... AS uuid) et non « :id::uuid » : le « :: » empêche SQLAlchemy de
            # reconnaître le paramètre, qui partirait littéralement dans le SQL.
            "VALUES (coalesce(CAST(:id AS uuid), gen_random_uuid()), "
            "        :occurred_at, 'test.chainage') "
            "RETURNING id, chain_hash, previous_chain_hash"
        ),
        {"id": log_id, "occurred_at": OCCURRED_AT},
    ).one()


def test_chain_hash_est_pose_par_la_base(session: Session) -> None:
    ligne = _inserer_log(session)

    assert ligne.chain_hash is not None
    assert len(ligne.chain_hash) == 64


def test_chain_hash_fourni_par_l_appelant_est_ecrase(session: Session) -> None:
    # Le maillon n'est pas négociable : une valeur forgée côté client est ignorée.
    faux = "f" * 64
    ligne = session.execute(
        text(
            "INSERT INTO audit.audit_logs "
            "(occurred_at, action, chain_hash, previous_chain_hash) "
            "VALUES (:occurred_at, 'test.forgerie', :faux, :faux) "
            "RETURNING chain_hash, previous_chain_hash"
        ),
        {"occurred_at": OCCURRED_AT, "faux": faux},
    ).one()

    assert ligne.chain_hash != faux
    assert ligne.previous_chain_hash != faux


def test_le_premier_maillon_a_un_precedent_null(session: Session) -> None:
    # Remise à zéro de la tête dans la transaction : le test ne dépend pas de ce que la
    # base contient déjà.
    session.execute(text("UPDATE audit.audit_chain_head SET chain_hash = NULL WHERE id"))

    ligne = _inserer_log(session)

    assert ligne.previous_chain_hash is None
    assert ligne.chain_hash is not None


def test_chaque_maillon_pointe_vers_le_precedent(session: Session) -> None:
    premier = _inserer_log(session)
    second = _inserer_log(session)

    assert second.previous_chain_hash == premier.chain_hash
    assert second.chain_hash != premier.chain_hash


def test_la_tete_de_chaine_suit_le_dernier_maillon(session: Session) -> None:
    _inserer_log(session)
    dernier = _inserer_log(session)

    tete = session.execute(
        text("SELECT chain_hash FROM audit.audit_chain_head WHERE id")
    ).scalar_one()

    assert tete == dernier.chain_hash


def test_le_hash_est_reproductible(session: Session) -> None:
    # Un vérificateur externe (futur audit.export) doit pouvoir recalculer le maillon à
    # partir du seul enregistrement. C'est ce qui rend la chaîne opposable à un auditeur.
    ligne = _inserer_log(session)

    session.execute(text("SET LOCAL TimeZone TO 'UTC'"))
    recalcule = session.execute(
        text(
            "SELECT encode(sha256(convert_to("
            "  (to_jsonb(l) - 'chain_hash' - 'previous_chain_hash')::text"
            "  || coalesce(l.previous_chain_hash, ''), 'UTF8')), 'hex') "
            "FROM audit.audit_logs l WHERE l.id = :id"
        ),
        {"id": ligne.id},
    ).scalar_one()

    assert recalcule == ligne.chain_hash


def _hash_sous_fuseau(fuseau: str) -> str:
    """Hash du MÊME enregistrement, calculé depuis une session dans un fuseau donné."""
    db = SessionLocal()
    try:
        db.execute(text(f"SET LOCAL TimeZone TO '{fuseau}'"))
        db.execute(text("UPDATE audit.audit_chain_head SET chain_hash = NULL WHERE id"))
        ligne = _inserer_log(db, ID_FIGE)
        chain_hash: str = ligne.chain_hash
        return chain_hash
    finally:
        db.rollback()
        db.close()


def test_hash_independant_du_fuseau_de_session() -> None:
    # to_jsonb() rend un timestamptz selon le fuseau de la session : sans le
    # « SET TimeZone TO 'UTC' » porté par la fonction (migration 0003), ces deux hashs
    # différeraient et la chaîne ne serait pas rejouable d'une machine à l'autre.
    # Ce test est le garde-fou de ce SET : ne pas le retirer.
    assert _hash_sous_fuseau("UTC") == _hash_sous_fuseau("Asia/Tokyo")


def test_le_verrou_serialise_les_ecritures() -> None:
    # Deux maillons ne peuvent pas naître du même prédécesseur : l'écrivain garde le
    # verrou consultatif jusqu'à la fin de sa transaction.
    ecrivain = SessionLocal()
    concurrent = SessionLocal()
    try:
        _inserer_log(ecrivain)

        verrou_pendant = concurrent.execute(
            text("SELECT pg_try_advisory_xact_lock(:cle)"), {"cle": LOCK_KEY}
        ).scalar_one()
        assert verrou_pendant is False

        concurrent.rollback()
        ecrivain.rollback()

        verrou_apres = concurrent.execute(
            text("SELECT pg_try_advisory_xact_lock(:cle)"), {"cle": LOCK_KEY}
        ).scalar_one()
        assert verrou_apres is True
    finally:
        ecrivain.rollback()
        ecrivain.close()
        concurrent.rollback()
        concurrent.close()
