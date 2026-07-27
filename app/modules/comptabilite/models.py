"""Modèles ORM du schéma « comptabilite » — socle comptable C0.

Mappe la table créée par la migration 0015 ; cette classe ne crée rien, la structure vient
de la migration. Les FK et index déclarés ici reflètent EXACTEMENT 0015 — c'est ce que le
méta-test « alembic check » exige. Les CHECK ne sont pas comparés par alembic : on ne les
redéclare pas, la base les impose.

Le plan de comptes est une DONNÉE : la table est peuplée par la commande d'import (CSV RCSFD),
gérée ensuite par le comptable. Aucune relationship ici (pas de navigation ORM à ce stade) ;
la self-FK parent_id ne sert qu'à la parité et à l'intégrité.
"""

import uuid
from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")
FK_USER = "security.users.id"
FK_JOURNAL = "comptabilite.journals.id"
FK_EXERCICE = "comptabilite.exercices.id"
FK_ENTRY = "comptabilite.journal_entries.id"
FK_ACCOUNT = "comptabilite.accounts.id"


class Account(Base):
    """Un compte du plan comptable RCSFD (saisie ou regroupement)."""

    __tablename__ = "accounts"
    __table_args__: tuple[Any, ...] = (
        sa.Index("ix_accounts_parent_id", "parent_id"),
        sa.Index(
            "ix_accounts_class",
            "account_class",
            postgresql_where=sa.text("is_active"),
        ),
        {"schema": "comptabilite"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    account_number: Mapped[str] = mapped_column(sa.String(20), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    short_name: Mapped[str | None] = mapped_column(sa.String(50))
    account_class: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("comptabilite.accounts.id")
    )
    normal_side: Mapped[str] = mapped_column(sa.CHAR(1), nullable=False)
    is_posting: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    is_system: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    is_provisional: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.true()
    )
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    notes: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))

    def __repr__(self) -> str:
        return f"<Account {self.account_number} {self.name!r}>"


class Exercice(Base):
    """Un exercice comptable (période). La clôture est reportée : par défaut « ouvert »."""

    __tablename__ = "exercices"
    __table_args__: tuple[Any, ...] = ({"schema": "comptabilite"},)

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    code: Mapped[str] = mapped_column(sa.String(20), nullable=False, unique=True)
    label: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    date_debut: Mapped[date] = mapped_column(sa.Date, nullable=False)
    date_fin: Mapped[date] = mapped_column(sa.Date, nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(10), nullable=False, server_default=sa.text("'ouvert'")
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))

    def __repr__(self) -> str:
        return f"<Exercice {self.code} {self.status}>"


class Journal(Base):
    """Un journal comptable (caisse, banque, opérations diverses…). Donnée provisoire."""

    __tablename__ = "journals"
    __table_args__: tuple[Any, ...] = ({"schema": "comptabilite"},)

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    code: Mapped[str] = mapped_column(sa.String(10), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    type: Mapped[str] = mapped_column(sa.String(30), nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    is_provisional: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.true()
    )
    is_system: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))

    def __repr__(self) -> str:
        return f"<Journal {self.code} {self.name!r}>"


class NumberingSequence(Base):
    """Compteur atomique du numéro de pièce, par (journal, exercice) — repart à 1 par exercice."""

    __tablename__ = "numbering_sequences"
    __table_args__: tuple[Any, ...] = ({"schema": "comptabilite"},)

    journal_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey(FK_JOURNAL), primary_key=True
    )
    exercice_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey(FK_EXERCICE), primary_key=True
    )
    last_value: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, server_default=sa.text("0")
    )
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)


class JournalEntry(Base):
    """La PIÈCE comptable (en-tête). Frontière brouillon/validé imposée par un CHECK + triggers."""

    __tablename__ = "journal_entries"
    __table_args__: tuple[Any, ...] = (
        sa.UniqueConstraint(
            "journal_id", "exercice_id", "entry_number", name="numero_unique"
        ),
        sa.Index("ix_entries_journal_exercice", "journal_id", "exercice_id"),
        sa.Index("ix_entries_reversal_of", "reversal_of_id"),
        {"schema": "comptabilite"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    entry_number: Mapped[str | None] = mapped_column(sa.String(30))
    journal_id: Mapped[uuid.UUID] = mapped_column(UUID, sa.ForeignKey(FK_JOURNAL), nullable=False)
    exercice_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey(FK_EXERCICE), nullable=False
    )
    entry_date: Mapped[date] = mapped_column(sa.Date, nullable=False)
    description: Mapped[str] = mapped_column(sa.String(300), nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(10), nullable=False, server_default=sa.text("'brouillon'")
    )
    posted_at: Mapped[datetime | None] = mapped_column(TS)
    posted_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    reversal_of_id: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_ENTRY))
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))

    def __repr__(self) -> str:
        return f"<JournalEntry {self.entry_number or '(brouillon)'} {self.status}>"


class EntrySchema(Base):
    """Modèle d'écriture d'une opération métier (« epargne.depot »…). Donnée provisoire."""

    __tablename__ = "entry_schemas"
    __table_args__: tuple[Any, ...] = ({"schema": "comptabilite"},)

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    code: Mapped[str] = mapped_column(sa.String(50), nullable=False, unique=True)
    label: Mapped[str] = mapped_column(sa.String(150), nullable=False)
    journal_id: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_JOURNAL))
    is_provisional: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.true()
    )
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))

    def __repr__(self) -> str:
        return f"<EntrySchema {self.code}>"


class EntrySchemaLine(Base):
    """Une ligne du modèle : un rôle abstrait (CAISSE, EPARGNE…) et un sens (D/C)."""

    __tablename__ = "entry_schema_lines"
    __table_args__: tuple[Any, ...] = (
        sa.UniqueConstraint("schema_id", "line_order", name="ordre_unique"),
        sa.Index("ix_schema_lines_schema", "schema_id"),
        {"schema": "comptabilite"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    schema_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("comptabilite.entry_schemas.id", ondelete="CASCADE"), nullable=False
    )
    line_order: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False)
    role: Mapped[str] = mapped_column(sa.String(30), nullable=False)
    side: Mapped[str] = mapped_column(sa.CHAR(1), nullable=False)


class JournalLine(Base):
    """Une ligne d'écriture : un compte de saisie, un sens (D/C), un montant entier (XOF)."""

    __tablename__ = "journal_lines"
    __table_args__: tuple[Any, ...] = (
        sa.Index("ix_lines_entry_id", "entry_id"),
        sa.Index("ix_lines_account_id", "account_id"),
        {"schema": "comptabilite"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey(FK_ENTRY, ondelete="CASCADE"), nullable=False
    )
    line_number: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(UUID, sa.ForeignKey(FK_ACCOUNT), nullable=False)
    side: Mapped[str] = mapped_column(sa.CHAR(1), nullable=False)
    amount: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    label: Mapped[str | None] = mapped_column(sa.String(300))
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
