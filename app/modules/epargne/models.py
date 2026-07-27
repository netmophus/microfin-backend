"""Modèles ORM du schéma « epargne » — produits, comptes, mouvements (migration 0018).

Ces classes mappent l'existant, elles ne créent rien. FK et index reflètent EXACTEMENT 0018
(exigence d'alembic check). Les CHECK et triggers ne sont pas redéclarés : la base les impose.

Le solde `balance` est un CACHE : la vérité est la somme des mouvements (voir
`service.verifier_coherence_solde`). Les mouvements sont append-only (trigger d'immuabilité).
"""

import uuid
from datetime import datetime
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


class Product(Base):
    """Un produit d'épargne (à vue / terme / programmée). Donnée provisoire."""

    __tablename__ = "products"
    __table_args__: tuple[Any, ...] = ({"schema": "epargne"},)

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    code: Mapped[str] = mapped_column(sa.String(20), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(sa.String(150), nullable=False)
    type: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    currency: Mapped[str] = mapped_column(
        sa.CHAR(3), nullable=False, server_default=sa.text("'XOF'")
    )
    min_balance: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, server_default=sa.text("0")
    )
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    is_provisional: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.true()
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))

    def __repr__(self) -> str:
        return f"<Product {self.code} {self.name!r}>"


class NumberingSequence(Base):
    """Compteur atomique du numéro de compte, par (prefix, year) — patron des tiers."""

    __tablename__ = "numbering_sequences"
    __table_args__: tuple[Any, ...] = ({"schema": "epargne"},)

    prefix: Mapped[str] = mapped_column(sa.String(10), primary_key=True)
    year: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    last_value: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, server_default=sa.text("0")
    )
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)


class SavingsAccount(Base):
    """Le compte d'épargne d'un membre. `balance` est un CACHE (la vérité = Σ mouvements)."""

    __tablename__ = "accounts"
    __table_args__: tuple[Any, ...] = (
        sa.Index("ix_epargne_accounts_tier", "tier_id"),
        sa.Index("ix_epargne_accounts_agency", "agency_id"),
        {"schema": "epargne"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    account_number: Mapped[str] = mapped_column(sa.String(30), nullable=False, unique=True)
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("epargne.products.id"), nullable=False
    )
    tier_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("tiers.tiers.id"), nullable=False
    )
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("parameters.agencies.id"), nullable=False
    )
    currency: Mapped[str] = mapped_column(
        sa.CHAR(3), nullable=False, server_default=sa.text("'XOF'")
    )
    status: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, server_default=sa.text("'actif'")
    )
    balance: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, server_default=sa.text("0"))
    opened_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    opened_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    closed_at: Mapped[datetime | None] = mapped_column(TS)
    closed_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    closure_reason: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))

    def __repr__(self) -> str:
        return f"<SavingsAccount {self.account_number} {self.status} solde={self.balance}>"


class SavingsMovement(Base):
    """Un mouvement du compte : credit augmente le solde membre, debit le diminue. Append-only."""

    __tablename__ = "movements"
    __table_args__: tuple[Any, ...] = (
        sa.Index("ix_epargne_movements_account", "account_id"),
        {"schema": "epargne"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("epargne.accounts.id"), nullable=False
    )
    sens: Mapped[str] = mapped_column(sa.String(6), nullable=False)  # 'credit' | 'debit'
    amount: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    balance_after: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    operation_type: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("comptabilite.journal_entries.id")
    )
    label: Mapped[str | None] = mapped_column(sa.String(300))
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
