"""Socle Sécurité & Administration : schémas security / audit / parameters (minimal).

Référence : « Socle Sécurité & Administration — Conception validée » v1.0 (16/07/2026),
§3.1 (security), §3.2 (audit), §3.3 (parameters minimal). En cas de divergence, ce
document prime sur les spécifications .docx d'origine (rédigées en async/PG15/Py3.11).

Périmètre de cette migration : extension citext, les 3 schémas, les tables du socle et
les 12 partitions mensuelles 2026 de audit.audit_logs.

Hors périmètre, étapes suivantes : trigger d'immuabilité audit_logs_immutable(),
job Dramatiq de partitionnement 2027+ (C13), seed des 11 rôles système et des
17 permissions Sécurité (C16), index de performance non spécifiés par le document.

Revision ID: 0001
Revises:
Create Date: 2026-07-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Ordre de création ; le downgrade les supprime en sens inverse.
SCHEMAS: tuple[str, ...] = ("parameters", "security", "audit")

# C13 — seules les partitions 2026 sont créées ici.
AUDIT_YEAR = 2026

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")


def _audit_partition_bounds(month: int) -> tuple[str, str, str]:
    """Nom et bornes UTC d'une partition mensuelle.

    Les bornes portent un fuseau explicite : sans lui, PostgreSQL les interpréterait
    dans le TimeZone de la session qui applique le DDL, et les frontières de mois
    dépendraient de la machine qui migre.
    """
    name = f"audit_logs_{AUDIT_YEAR}_{month:02d}"
    start = f"{AUDIT_YEAR}-{month:02d}-01 00:00:00+00"
    end_year, end_month = (AUDIT_YEAR + 1, 1) if month == 12 else (AUDIT_YEAR, month + 1)
    end = f"{end_year}-{end_month:02d}-01 00:00:00+00"
    return name, start, end


def upgrade() -> None:
    # C14 — email insensible à la casse. gen_random_uuid() est natif en PG16.
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    for schema in SCHEMAS:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")

    # --- parameters (minimal) ------------------------------------------------
    # Créée en premier : security.users.primary_agency_id la référence. Ses propres
    # FK created_by/updated_by -> security.users sont ajoutées après users, la
    # dépendance entre les deux tables étant circulaire.
    op.create_table(
        "agencies",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("code", sa.String(30), nullable=False),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
        schema="parameters",
    )

    # --- security.users — pierre angulaire (173 FK) ---------------------------
    op.create_table(
        "users",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("matricule", sa.String(30), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("username", sa.String(50), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("last_name", sa.String(100), nullable=False),
        sa.Column("first_name", sa.String(100), nullable=False),
        sa.Column("phone", sa.String(30), nullable=True),
        sa.Column("primary_agency_id", UUID, nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_locked", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("locked_until", TS, nullable=True),
        sa.Column("failed_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        # C7 — verrouillage progressif 15/30/60/120 min.
        sa.Column("lockout_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_lockout_at", TS, nullable=True),
        # C8 — compteur 2FA séparé du compteur mot de passe.
        sa.Column("failed_2fa_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("password_changed_at", TS, server_default=NOW, nullable=False),
        sa.Column("must_change_password", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("requires_2fa", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("last_login_at", TS, nullable=True),
        sa.Column("last_login_ip", postgresql.INET(), nullable=True),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_by", UUID, nullable=True),
        # Soft delete : jamais d'effacement physique (conservation BCEAO).
        sa.Column("deleted_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("matricule"),
        sa.UniqueConstraint("email"),
        sa.UniqueConstraint("username"),
        sa.ForeignKeyConstraint(["primary_agency_id"], ["parameters.agencies.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["security.users.id"]),
        sa.ForeignKeyConstraint(["updated_by"], ["security.users.id"]),
        schema="security",
    )

    # Fermeture du cycle agencies <-> users (cf. commentaire sur agencies).
    op.create_foreign_key(
        "fk_agencies_created_by_users",
        "agencies",
        "users",
        ["created_by"],
        ["id"],
        source_schema="parameters",
        referent_schema="security",
    )
    op.create_foreign_key(
        "fk_agencies_updated_by_users",
        "agencies",
        "users",
        ["updated_by"],
        ["id"],
        source_schema="parameters",
        referent_schema="security",
    )

    # --- security : rôles et permissions --------------------------------------
    op.create_table(
        "roles",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("code", sa.String(50), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        # is_system : rôle non modifiable et non supprimable (les 11 rôles du seed).
        sa.Column("is_system", sa.Boolean(), server_default=sa.false(), nullable=False),
        # C17 — 2FA imposable par rôle.
        sa.Column("requires_2fa", sa.Boolean(), server_default=sa.false(), nullable=False),
        # C9 — 90 j par défaut, 60 j pour les profils sensibles.
        sa.Column(
            "password_expiry_days", sa.Integer(), server_default=sa.text("90"), nullable=False
        ),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
        sa.ForeignKeyConstraint(["created_by"], ["security.users.id"]),
        sa.ForeignKeyConstraint(["updated_by"], ["security.users.id"]),
        schema="security",
    )

    op.create_table(
        "permissions",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        # Format module.action, ex. users.read.
        sa.Column("code", sa.String(100), nullable=False),
        # Scope optionnel (§5) : NULL = permission non portée par un périmètre.
        sa.Column("scope", sa.String(10), nullable=True),
        sa.Column("module", sa.String(50), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
        sa.CheckConstraint("scope IN ('own', 'agency', 'all')", name="scope_valide"),
        schema="security",
    )

    op.create_table(
        "role_permissions",
        sa.Column("role_id", UUID, nullable=False),
        sa.Column("permission_id", UUID, nullable=False),
        sa.Column("granted_at", TS, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("role_id", "permission_id"),
        sa.ForeignKeyConstraint(["role_id"], ["security.roles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["permission_id"], ["security.permissions.id"], ondelete="CASCADE"),
        schema="security",
    )

    op.create_table(
        "user_roles",
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("role_id", UUID, nullable=False),
        sa.Column("assigned_at", TS, server_default=NOW, nullable=False),
        sa.Column("assigned_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("user_id", "role_id"),
        sa.ForeignKeyConstraint(["user_id"], ["security.users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["security.roles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assigned_by"], ["security.users.id"]),
        schema="security",
    )

    # C6 — habilitations réseau. Le mono-agence est une liste à une seule entrée.
    op.create_table(
        "user_agencies",
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("agency_id", UUID, nullable=False),
        sa.Column("granted_at", TS, server_default=NOW, nullable=False),
        sa.Column("granted_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("user_id", "agency_id"),
        sa.ForeignKeyConstraint(["user_id"], ["security.users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agency_id"], ["parameters.agencies.id"]),
        sa.ForeignKeyConstraint(["granted_by"], ["security.users.id"]),
        schema="security",
    )

    # --- security : sessions, 2FA, historique ---------------------------------
    # Les sessions révoquées sont conservées : la réutilisation d'un refresh déjà
    # consommé signale un vol de jeton.
    op.create_table(
        "user_sessions",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("refresh_token_hash", sa.Text(), nullable=False),
        sa.Column("issued_at", TS, server_default=NOW, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("revoked_at", TS, nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("replaced_by_session_id", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["security.users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["replaced_by_session_id"], ["security.user_sessions.id"]),
        schema="security",
    )

    # C4 — secret chiffré Fernet (clé FERNET_2FA_KEY, distincte de JWT_SECRET).
    # Un secret par utilisateur : user_id porte la PK.
    op.create_table(
        "user_2fa_secrets",
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("secret_encrypted", sa.Text(), nullable=False),
        sa.Column(
            "backup_codes_hashed",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "backup_codes_used",
            postgresql.ARRAY(sa.Boolean()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        # NULL tant que l'utilisateur n'a pas confirmé son premier code.
        sa.Column("activated_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("user_id"),
        sa.ForeignKeyConstraint(["user_id"], ["security.users.id"], ondelete="CASCADE"),
        schema="security",
    )

    # C12 — 12 derniers hashs refusés. La purge au-delà de 12 relève du service.
    op.create_table(
        "user_passwords_history",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["security.users.id"], ondelete="CASCADE"),
        schema="security",
    )

    # --- audit.audit_logs — immuable, chaînée, partitionnée -------------------
    # PK composite obligatoire : la clé de partition doit en faire partie.
    # agency_id et resource_id restent sans FK (§3.2) : le journal doit survivre à
    # la disparition de l'objet audité.
    op.create_table(
        "audit_logs",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("occurred_at", TS, server_default=NOW, nullable=False),
        # NULL = échec de login sur un compte inconnu.
        sa.Column("user_id", UUID, nullable=True),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("resource_type", sa.String(50), nullable=True),
        sa.Column("resource_id", UUID, nullable=True),
        sa.Column("old_values", postgresql.JSONB(), nullable=True),
        sa.Column("new_values", postgresql.JSONB(), nullable=True),
        sa.Column("agency_id", UUID, nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("request_id", UUID, nullable=True),
        # C15 — poste de travail (BCEAO). Réservé, non alimenté pour l'instant.
        sa.Column("workstation_id", sa.Text(), nullable=True),
        sa.Column("chain_hash", sa.CHAR(64), nullable=False),
        # NULL sur le tout premier maillon de la chaîne.
        sa.Column("previous_chain_hash", sa.CHAR(64), nullable=True),
        sa.PrimaryKeyConstraint("id", "occurred_at"),
        sa.ForeignKeyConstraint(["user_id"], ["security.users.id"]),
        schema="audit",
        postgresql_partition_by="RANGE (occurred_at)",
    )

    for month in range(1, 13):
        name, start, end = _audit_partition_bounds(month)
        op.execute(
            f"CREATE TABLE audit.{name} PARTITION OF audit.audit_logs "
            f"FOR VALUES FROM ('{start}') TO ('{end}')"
        )


def downgrade() -> None:
    for month in range(12, 0, -1):
        name, _, _ = _audit_partition_bounds(month)
        op.execute(f"DROP TABLE IF EXISTS audit.{name}")
    op.drop_table("audit_logs", schema="audit")

    op.drop_table("user_passwords_history", schema="security")
    op.drop_table("user_2fa_secrets", schema="security")
    op.drop_table("user_sessions", schema="security")
    op.drop_table("user_agencies", schema="security")
    op.drop_table("user_roles", schema="security")
    op.drop_table("role_permissions", schema="security")
    op.drop_table("permissions", schema="security")
    op.drop_table("roles", schema="security")

    # Le cycle doit être rouvert avant de pouvoir supprimer users.
    op.drop_constraint(
        "fk_agencies_updated_by_users", "agencies", schema="parameters", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_agencies_created_by_users", "agencies", schema="parameters", type_="foreignkey"
    )

    op.drop_table("users", schema="security")
    op.drop_table("agencies", schema="parameters")

    # Sans CASCADE : la suppression échoue si un objet a été oublié.
    for schema in reversed(SCHEMAS):
        op.execute(f"DROP SCHEMA IF EXISTS {schema}")

    op.execute("DROP EXTENSION IF EXISTS citext")
