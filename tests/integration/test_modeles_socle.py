"""Garde-fou des modèles ORM : ils se mappent sur l'existant, ils ne créent jamais rien.

Remplace le test_aucune_table_metier_declaree de la fondation, qui exigeait une metadata
vide et ne pouvait pas survivre à l'arrivée des modèles. L'intention est conservée, et
même durcie : au lieu d'affirmer qu'aucun modèle n'existe, on vérifie que chaque modèle
correspond à une table réellement créée par les migrations.
"""

import subprocess
import sys
import uuid
from collections.abc import Generator
from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from app.core.database import Base, SessionLocal, engine
from app.modules.audit.models import AuditLog, EcritureAuditInterditeError
from app.modules.parameters.models import (
    Agency,
    City,
    Country,
    Currency,
    IdentityDocumentType,
    Region,
)
from app.modules.security.models import (
    Permission,
    Role,
    RolePermission,
    User,
    User2FASecret,
    UserAgency,
    UserPasswordHistory,
    UserRole,
    UserSession,
)
from app.modules.tiers.models import (
    GroupProfile,
    IndividualProfile,
    LegalEntityProfile,
    LifecycleEvent,
    NumberingSequence,
    Tier,
)

# Le périmètre exact des tables mappées. Toute entrée ajoutée ou retirée ici doit être un
# choix conscient : ce set est ce qui empêche un modèle d'apparaître par inadvertance.
TABLES_ATTENDUES = frozenset(
    {
        "security.users",
        "security.roles",
        "security.permissions",
        "security.role_permissions",
        "security.user_roles",
        "security.user_agencies",
        "security.user_sessions",
        "security.user_2fa_secrets",
        "security.user_passwords_history",
        "audit.audit_logs",
        "parameters.agencies",
        # Référentiels du module Tiers (migration 0007) — modèles de lecture.
        "parameters.countries",
        "parameters.currencies",
        "parameters.regions",
        "parameters.cities",
        "parameters.identity_document_types",
        # Cœur du module Tiers (migration 0008) — Class Table Inheritance.
        "tiers.tiers",
        "tiers.individual_profiles",
        "tiers.legal_entity_profiles",
        "tiers.group_profiles",
        "tiers.numbering_sequences",
        "tiers.lifecycle_events",
    }
)

MODELES = [
    User,
    Role,
    Permission,
    RolePermission,
    UserRole,
    UserAgency,
    UserSession,
    User2FASecret,
    UserPasswordHistory,
    AuditLog,
    Agency,
    Country,
    Currency,
    Region,
    City,
    IdentityDocumentType,
    Tier,
    IndividualProfile,
    LegalEntityProfile,
    GroupProfile,
    NumberingSequence,
    LifecycleEvent,
]


@pytest.fixture
def db() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def test_liste_des_tables_mappees_est_figee() -> None:
    assert set(Base.metadata.tables) == TABLES_ATTENDUES


def test_aucun_modele_ne_mappe_une_partition() -> None:
    # Les partitions mensuelles sont routées par PostgreSQL depuis la table parente.
    # En mapper une serait un contresens : on lit et on écrit toujours audit.audit_logs.
    assert not [t for t in Base.metadata.tables if "audit_logs_2" in t]


@pytest.mark.integration
def test_les_modeles_ne_mappent_que_des_tables_existantes() -> None:
    """Le cœur du garde-fou : aucun modèle n'invente de table."""
    inspecteur = inspect(engine)
    for cle, table in Base.metadata.tables.items():
        schema = table.schema
        assert schema is not None, f"{cle} ne déclare pas de schéma"
        assert inspecteur.has_table(table.name, schema=schema), (
            f"{cle} est mappée par un modèle mais n'existe pas en base : "
            "elle doit être créée par une migration, jamais par l'ORM"
        )


@pytest.mark.integration
def test_les_colonnes_mappees_existent_toutes_en_base() -> None:
    """Complète alembic check : aucune colonne inventée, aucune colonne oubliée."""
    inspecteur = inspect(engine)
    for cle, table in Base.metadata.tables.items():
        assert table.schema is not None
        reelles = {c["name"] for c in inspecteur.get_columns(table.name, schema=table.schema)}
        mappees = {c.name for c in table.columns}
        assert mappees == reelles, (
            f"{cle} : le modèle et la base divergent — "
            f"en trop dans le modèle {mappees - reelles}, absentes du modèle {reelles - mappees}"
        )


@pytest.mark.integration
def test_chaque_modele_est_interrogeable(db: Session) -> None:
    """Un SELECT réel sur chaque modèle : le mapping tient face aux types PostgreSQL.

    alembic check compare des métadonnées ; ce test exécute la requête. Il attraperait un
    type que psycopg refuse de convertir, ce qu'une comparaison statique laisserait passer.
    """
    for modele in MODELES:
        db.execute(select(modele).limit(1)).all()


@pytest.mark.integration
def test_aucune_derive_entre_les_modeles_et_la_base() -> None:
    """Lance le vrai « alembic check », env.py compris, pas une réimplémentation.

    Échoue si un modèle diverge des tables existantes — auquel cas il faut corriger le
    modèle, pas générer une migration.
    """
    resultat = subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        capture_output=True,
        text=True,
        check=False,
    )
    sortie = resultat.stdout + resultat.stderr
    assert resultat.returncode == 0, f"alembic check a détecté une dérive :\n{sortie}"
    assert "No new upgrade operations detected" in sortie


# --- audit.audit_logs : lecture seule ----------------------------------------------


# Mars 2026 : la partition audit_logs_2026_03 existe (migration 0001).
OCCURRED_AT = datetime(2026, 3, 15, 10, 0, tzinfo=UTC)


def _journal_factice() -> AuditLog:
    return AuditLog(
        id=uuid.uuid4(),
        occurred_at=datetime.now(UTC),
        action="test.interdit",
        chain_hash="0" * 64,
    )


def _inserer_puis_charger(db: Session) -> AuditLog:
    """Insère une ligne par SQL, puis la charge par l'ORM.

    L'insertion passe par du SQL explicite parce que l'ORM la refuse — c'est précisément
    ce qu'on vérifie par ailleurs. Ne jamais committer : une ligne d'audit committée
    serait indélébile (immuabilité, migration 0002). La fixture annule la transaction.
    """
    log_id = db.execute(
        text(
            "INSERT INTO audit.audit_logs (occurred_at, action, chain_hash) "
            "VALUES (:occurred_at, 'test.lecture_seule', :chain_hash) RETURNING id"
        ),
        {"occurred_at": OCCURRED_AT, "chain_hash": "b" * 64},
    ).scalar_one()
    return db.execute(
        select(AuditLog).where(AuditLog.id == log_id, AuditLog.occurred_at == OCCURRED_AT)
    ).scalar_one()


@pytest.mark.integration
def test_insert_dans_audit_logs_refuse_par_lorm(db: Session) -> None:
    # L'INSERT est ouvert en base — c'est par là que le service d'audit écrit. Le refus
    # vient donc de l'ORM, et il doit tomber avant d'atteindre PostgreSQL.
    db.add(_journal_factice())
    with pytest.raises(EcritureAuditInterditeError):
        db.flush()


@pytest.mark.integration
def test_update_dans_audit_logs_refuse_par_lorm(db: Session) -> None:
    entree = _inserer_puis_charger(db)
    entree.action = "test.modifie"
    with pytest.raises(EcritureAuditInterditeError):
        db.flush()


@pytest.mark.integration
def test_delete_dans_audit_logs_refuse_par_lorm(db: Session) -> None:
    entree = _inserer_puis_charger(db)
    db.delete(entree)
    with pytest.raises(EcritureAuditInterditeError):
        db.flush()


def test_le_repr_daudit_log_ne_revele_aucune_valeur() -> None:
    # old_values / new_values peuvent contenir des données personnelles : un __repr__
    # atterrit dans les logs et les traces d'exception.
    entree = _journal_factice()
    entree.new_values = {"email": "secret@example.com"}
    assert "secret@example.com" not in repr(entree)


# --- relations ---------------------------------------------------------------------


@pytest.mark.integration
def test_les_relations_se_configurent(db: Session) -> None:
    """Force la configuration des mappers : une relationship ambiguë échouerait ici.

    users est atteignable par plusieurs FK (created_by, updated_by, assigned_by,
    granted_by) ; sans foreign_keys explicite, SQLAlchemy refuserait de configurer.
    """
    from sqlalchemy.orm import configure_mappers

    configure_mappers()

    for relation in ("primary_agency", "user_roles", "user_agencies", "sessions", "roles"):
        assert hasattr(User, relation)
    assert hasattr(Role, "permissions")


@pytest.mark.integration
def test_les_raccourcis_dassociation_sont_en_lecture_seule() -> None:
    """User.roles et User.agencies doublent une association déjà écrite ailleurs.

    Sans viewonly, deux chemins d'écriture viseraient les mêmes lignes et se
    marcheraient dessus au flush.
    """
    assert inspect(User).relationships["roles"].viewonly
    assert inspect(User).relationships["agencies"].viewonly
    assert inspect(Role).relationships["permissions"].viewonly
