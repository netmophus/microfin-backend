"""Modèles ORM du schéma « credit » — référentiel produit (migration 0031, CR0).

Mappe la table créée par la migration ; cette classe ne crée rien. FK et CHECK reflètent
EXACTEMENT 0031 (exigence d'alembic check pour les FK — les CHECK ne sont pas comparés, la
base les impose).

Premier bloc du module Crédit (individuel simple, échéances fixes, membres ET clients).
Demandes, décision, décaissement, échéancier : blocs suivants (CR1+), pas encore de modèles ici.
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
FK_ACCOUNT = "comptabilite.accounts.id"


class Product(Base):
    """Un produit de crédit (individuel simple, échéances fixes). Donnée provisoire."""

    __tablename__ = "products"
    __table_args__: tuple[Any, ...] = ({"schema": "credit"},)

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    code: Mapped[str] = mapped_column(sa.String(20), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(sa.String(150), nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    is_provisional: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.true()
    )
    # Rattachement classe 20 (extension à 6 chiffres, ex. 202211/203111) — compte MEMBRE.
    compte_credit_membre_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey(FK_ACCOUNT)
    )
    # Rattachement CLIENT (ex. 202221/203121). NULL = pas encore configuré.
    compte_credit_client_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey(FK_ACCOUNT)
    )
    # Taux/échéancier (PROVISOIRES) — taux en POINTS DE BASE entiers, jamais de flottant.
    taux_bp: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))
    periodicite: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, server_default=sa.text("'mensuelle'")
    )
    methode_amortissement: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, server_default=sa.text("'echeance_constante'")
    )
    base_jours: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("360")
    )
    regle_arrondi: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, server_default=sa.text("'plus_proche'")
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))

    def __repr__(self) -> str:
        return f"<Product {self.code} {self.name!r}>"
