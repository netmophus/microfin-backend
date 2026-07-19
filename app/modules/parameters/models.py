"""Modèles du schéma « parameters » — version minimale requise par le socle Sécurité.

Mappe des tables créées par la migration 0001 (§3.3 du document de décisions v1.0). Ces
classes ne créent rien : toute structure vient des migrations.

Ce module est volontairement sans dépendance : ses FK vers security.users sont déclarées
par chaîne (« security.users.id »), et aucune relationship ne remonte vers User. Il peut
donc être importé seul, et security/audit peuvent l'importer sans cycle.
"""

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")


class Agency(Base):
    """Agence — cible de users.primary_agency_id et de user_agencies (§3.3).

    Version minimale : juste de quoi honorer les FK. Le CRUD complet et les colonnes
    métier (adresse, responsable, horaires…) relèvent du module Paramétrage, à venir.
    Les colonnes created_by / updated_by portent la traçabilité sans relationship : ce
    sont des références d'audit, pas des liens de navigation.

    use_alter sur ces deux FK : agencies et users se référencent mutuellement
    (users.primary_agency_id -> agencies, agencies.created_by -> users). Sans use_alter,
    SQLAlchemy ne sait pas ordonner les deux tables et avertit d'un cycle irrésoluble.
    Le marqueur décrit exactement ce que fait la migration 0001 — créer agencies sans ces
    FK, puis les ajouter par ALTER une fois users créée.
    """

    __tablename__ = "agencies"
    __table_args__ = ({"schema": "parameters"},)

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    code: Mapped[str] = mapped_column(sa.String(30), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(sa.String(150), nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, server_default=sa.true())
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("security.users.id", use_alter=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("security.users.id", use_alter=True), nullable=True
    )

    def __repr__(self) -> str:
        return f"<Agency {self.code}>"
