"""Modèles du schéma « security » — mapping des tables créées par la migration 0001.

Référence : « Socle Sécurité & Administration — Conception validée » v1.0 (16/07/2026),
§3.1. Ces classes ne créent aucune table : elles se mappent sur l'existant. Toute
évolution de structure passe par une migration, jamais par ce fichier.

Synchrone de bout en bout (Session, pas AsyncSession), conformément à app/core/database.py.

Colonnes sensibles — password_hash, refresh_token_hash, secret_encrypted,
backup_codes_hashed — sont des colonnes ordinaires ici : le mapping doit refléter la
base. Leur non-exposition relève des schémas Pydantic, à venir. Aucun __repr__ de ce
fichier ne les affiche.

Import : ce module importe parameters.models (pour Agency) et jamais l'inverse — la
dépendance est à sens unique, donc sans cycle.
"""

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.modules.parameters.models import Agency

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")
ZERO = sa.text("0")


class User(Base):
    """Utilisateur — pierre angulaire du socle (173 FK pointent vers cette table).

    Suppression : toujours logique (deleted_at renseigné), jamais physique — exigence
    BCEAO de conservation de l'historique. Aucun code ne doit émettre de DELETE ici.
    """

    __tablename__ = "users"
    # Unicité PARTIELLE, limitée aux comptes vivants (migration 0006) : la suppression est
    # logique, donc une ligne supprimée resterait dans un index inconditionnel et
    # confisquerait son identifiant à jamais — un employé de retour ne pourrait pas
    # retrouver son matricule, une adresse de service ne serait jamais réattribuable.
    # Déclaré ici et non par unique=True sur la colonne : unique=True produirait une
    # CONTRAINTE inconditionnelle, et `alembic check` signalerait une dérive perpétuelle.
    __table_args__ = (
        sa.Index(
            "uq_users_matricule_vivants",
            "matricule",
            unique=True,
            postgresql_where=sa.text("deleted_at IS NULL"),
        ),
        sa.Index(
            "uq_users_email_vivants",
            "email",
            unique=True,
            postgresql_where=sa.text("deleted_at IS NULL"),
        ),
        sa.Index(
            "uq_users_username_vivants",
            "username",
            unique=True,
            postgresql_where=sa.text("deleted_at IS NULL"),
        ),
        {"schema": "security"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    matricule: Mapped[str] = mapped_column(sa.String(30), nullable=False)
    # CITEXT (C14) : deux comptes ne peuvent pas différer par la seule casse de l'email.
    email: Mapped[str] = mapped_column(postgresql.CITEXT(), nullable=False)
    username: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    # Argon2id (OWASP). Jamais sérialisé, jamais journalisé.
    password_hash: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    last_name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    first_name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    phone: Mapped[str | None] = mapped_column(sa.String(30), nullable=True)
    primary_agency_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("parameters.agencies.id"), nullable=True
    )

    is_active: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, server_default=sa.true())
    is_locked: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, server_default=sa.false())
    locked_until: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    # Verrou à 5 échecs.
    failed_attempts: Mapped[int] = mapped_column(sa.Integer(), nullable=False, server_default=ZERO)
    # C7 — verrouillage progressif 15/30/60/120 min, réinitialisé après 24 h.
    lockout_count: Mapped[int] = mapped_column(sa.Integer(), nullable=False, server_default=ZERO)
    last_lockout_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    # C8 — compteur 2FA distinct du compteur mot de passe.
    failed_2fa_attempts: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, server_default=ZERO
    )

    password_changed_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    must_change_password: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=sa.false()
    )
    # C17 — la 2FA peut aussi être imposée par le rôle (roles.requires_2fa).
    requires_2fa: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=sa.false()
    )

    last_login_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    last_login_ip: Mapped[str | None] = mapped_column(postgresql.INET(), nullable=True)

    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("security.users.id"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("security.users.id"), nullable=True
    )
    # Soft delete : NULL = actif. Jamais d'effacement physique.
    deleted_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)

    # --- relations -----------------------------------------------------------
    # foreign_keys est obligatoire partout où users est atteignable par plusieurs
    # chemins (created_by, updated_by, assigned_by, granted_by) : sans lui, SQLAlchemy
    # ne sait pas quelle FK porte la relation et refuse de configurer le mapper.
    primary_agency: Mapped[Agency | None] = relationship(
        Agency, foreign_keys=[primary_agency_id], lazy="selectin"
    )

    user_roles: Mapped[list["UserRole"]] = relationship(
        back_populates="user",
        foreign_keys="UserRole.user_id",
        cascade="all, delete-orphan",
    )
    user_agencies: Mapped[list["UserAgency"]] = relationship(
        back_populates="user",
        foreign_keys="UserAgency.user_id",
        cascade="all, delete-orphan",
    )
    sessions: Mapped[list["UserSession"]] = relationship(
        back_populates="user", foreign_keys="UserSession.user_id"
    )
    twofa_secret: Mapped["User2FASecret | None"] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    passwords_history: Mapped[list["UserPasswordHistory"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    # Raccourcis de lecture. viewonly=True est indispensable : la même association est
    # déjà écrite via user_roles / user_agencies, et deux chemins d'écriture vers les
    # mêmes lignes se marcheraient dessus au flush.
    roles: Mapped[list["Role"]] = relationship(
        secondary="security.user_roles",
        primaryjoin="User.id == UserRole.user_id",
        secondaryjoin="Role.id == UserRole.role_id",
        viewonly=True,
        lazy="selectin",
    )
    agencies: Mapped[list[Agency]] = relationship(
        secondary="security.user_agencies",
        primaryjoin="User.id == UserAgency.user_id",
        secondaryjoin="Agency.id == UserAgency.agency_id",
        viewonly=True,
    )

    def __repr__(self) -> str:
        return f"<User {self.matricule}>"


class Role(Base):
    """Profil métier. Les 11 rôles du seed portent is_system = true.

    is_system : le DELETE est refusé en base (trigger de la migration 0004). L'UPDATE
    reste techniquement possible — le seed s'en sert pour resynchroniser les définitions
    à chaque montée de version ; c'est au service Sécurité de refuser l'UPDATE illégitime.
    """

    __tablename__ = "roles"
    __table_args__ = ({"schema": "security"},)

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    code: Mapped[str] = mapped_column(sa.String(50), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    is_system: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, server_default=sa.false())
    # C17 — un rôle qui exige la 2FA en interdit la désactivation.
    requires_2fa: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, server_default=sa.false()
    )
    # C9 — 90 j par défaut, 60 j pour les profils sensibles.
    password_expiry_days: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, server_default=sa.text("90")
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("security.users.id"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("security.users.id"), nullable=True
    )

    role_permissions: Mapped[list["RolePermission"]] = relationship(
        back_populates="role", cascade="all, delete-orphan"
    )
    permissions: Mapped[list["Permission"]] = relationship(
        secondary="security.role_permissions", viewonly=True, lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<Role {self.code}>"


class Permission(Base):
    """Droit atomique, au format module.action (§5)."""

    __tablename__ = "permissions"
    __table_args__ = (
        # Nommée « scope_valide » : la convention de nommage préfixe en
        # ck_permissions_scope_valide, qui est le nom réel en base.
        sa.CheckConstraint("scope IN ('own', 'agency', 'all')", name="scope_valide"),
        {"schema": "security"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    code: Mapped[str] = mapped_column(sa.String(100), nullable=False, unique=True)
    # NULL sur les 17 permissions du socle : le document ne les porte à aucun périmètre.
    # Le cloisonnement par agence passe par le claim agency_id du JWT (C6).
    scope: Mapped[str | None] = mapped_column(sa.String(10), nullable=True)
    module: Mapped[str] = mapped_column(sa.String(50), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)

    def __repr__(self) -> str:
        return f"<Permission {self.code}>"


class RolePermission(Base):
    """Association rôle ↔ permission (PK composite)."""

    __tablename__ = "role_permissions"
    __table_args__ = ({"schema": "security"},)

    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("security.roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("security.permissions.id", ondelete="CASCADE"), primary_key=True
    )
    granted_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)

    role: Mapped[Role] = relationship(back_populates="role_permissions")
    permission: Mapped[Permission] = relationship()


class UserRole(Base):
    """Association utilisateur ↔ rôle (PK composite), avec traçabilité de l'attribution.

    Un utilisateur ne peut pas modifier ses propres rôles (§4, séparation des pouvoirs) :
    règle du service, pas de la base.
    """

    __tablename__ = "user_roles"
    __table_args__ = ({"schema": "security"},)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("security.users.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("security.roles.id", ondelete="CASCADE"), primary_key=True
    )
    assigned_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("security.users.id"), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="user_roles", foreign_keys=[user_id])
    role: Mapped[Role] = relationship()
    assigner: Mapped[User | None] = relationship(foreign_keys=[assigned_by])


class UserAgency(Base):
    """Habilitation réseau : accès d'un utilisateur à une agence (C6).

    Le mono-agence est une liste à une seule entrée. L'agence courante d'une session
    vient du claim agency_id du JWT, pas de cette table.
    """

    __tablename__ = "user_agencies"
    __table_args__ = ({"schema": "security"},)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("security.users.id", ondelete="CASCADE"), primary_key=True
    )
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("parameters.agencies.id"), primary_key=True
    )
    granted_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    granted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("security.users.id"), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="user_agencies", foreign_keys=[user_id])
    agency: Mapped[Agency] = relationship(Agency, foreign_keys=[agency_id])
    granter: Mapped[User | None] = relationship(foreign_keys=[granted_by])


class UserSession(Base):
    """Session / refresh token. Les sessions révoquées sont conservées.

    Conservation volontaire : la réutilisation d'un refresh déjà consommé signale un vol
    de jeton — l'effacer supprimerait la preuve. replaced_by_session_id chaîne les
    rotations successives.
    """

    __tablename__ = "user_sessions"
    __table_args__ = ({"schema": "security"},)

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("security.users.id", ondelete="CASCADE"), nullable=False
    )
    # Hash du refresh, jamais le jeton en clair. Non sérialisable.
    refresh_token_hash: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    expires_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    ip: Mapped[str | None] = mapped_column(postgresql.INET(), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    replaced_by_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("security.user_sessions.id"), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="sessions", foreign_keys=[user_id])
    replaced_by: Mapped["UserSession | None"] = relationship(
        remote_side=[id], foreign_keys=[replaced_by_session_id]
    )

    def __repr__(self) -> str:
        return f"<UserSession {self.id}>"


class User2FASecret(Base):
    """Secret TOTP d'un utilisateur (C4). user_id porte la PK : un secret par compte."""

    __tablename__ = "user_2fa_secrets"
    __table_args__ = ({"schema": "security"},)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("security.users.id", ondelete="CASCADE"), primary_key=True
    )
    # Chiffré Fernet avec FERNET_2FA_KEY, distincte de JWT_SECRET. Non sérialisable.
    secret_encrypted: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    backup_codes_hashed: Mapped[list[str]] = mapped_column(
        postgresql.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'")
    )
    # Parallèle à backup_codes_hashed : backup_codes_used[i] marque le code i consommé.
    backup_codes_used: Mapped[list[bool]] = mapped_column(
        postgresql.ARRAY(sa.Boolean()), nullable=False, server_default=sa.text("'{}'")
    )
    # NULL tant que l'utilisateur n'a pas confirmé son premier code.
    activated_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)

    user: Mapped[User] = relationship(back_populates="twofa_secret")


class UserPasswordHistory(Base):
    """Hash d'un ancien mot de passe (C12) — les 12 derniers sont refusés à la réutilisation.

    La purge au-delà de 12 relève du service : la base conserve ce qu'on y met.
    """

    __tablename__ = "user_passwords_history"
    __table_args__ = ({"schema": "security"},)

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("security.users.id", ondelete="CASCADE"), nullable=False
    )
    password_hash: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)

    user: Mapped[User] = relationship(back_populates="passwords_history")
