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
