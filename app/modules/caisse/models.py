"""Modèles ORM du schéma « caisse » — sessions (CA0/CA1, migration 0040).

Mappent l'existant, ne créent rien. FK et CHECK reflètent EXACTEMENT la migration (exigence
d'alembic check pour les FK — les CHECK/index partiels ne sont pas comparés, la base les impose).

Module Caisse : ouverture/fermeture de journée PAR CAISSIER (jamais deux sessions ouvertes à la
fois pour le même caissier — UNIQUE partiel en base, `uq_caisse_sessions_caissier_ouverte`). Le
solde théorique n'est jamais stocké en continu — voir service.py::calculer_solde_theorique,
calcul dérivé des écritures validées, même philosophie que epargne.accounts.balance (cache,
jamais une seconde vérité)."""

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


class CaisseSession(Base):
    """Une session de caisse — CA1 : ouverture, fermeture, calcul/affichage de l'écart. AUCUNE
    écriture comptable posée par ce module en CA1 (voir CA3), aucun blocage sur écart (CA2).

    `compte_caisse_id` : ANCRÉ à l'ouverture (copié depuis `Agency.compte_caisse_id` à cet
    instant), jamais recalculé ensuite — même discipline que `compte_credit_id`/
    `compte_collectif_id` ailleurs dans ce projet."""

    __tablename__ = "sessions"
    __table_args__: tuple[Any, ...] = (
        sa.Index("ix_caisse_sessions_agency", "agency_id"),
        sa.Index("ix_caisse_sessions_caissier", "caissier_id"),
        sa.Index(
            "uq_caisse_sessions_caissier_ouverte",
            "caissier_id",
            unique=True,
            postgresql_where=sa.text("status = 'ouverte'"),
        ),
        sa.CheckConstraint("status IN ('ouverte', 'fermee')", name="status"),
        sa.CheckConstraint("fonds_initial >= 0", name="fonds_initial_positif"),
        sa.CheckConstraint(
            "(status = 'ouverte' AND closed_at IS NULL AND montant_reel_cloture IS NULL "
            "AND solde_theorique_cloture IS NULL AND ecart IS NULL) "
            "OR "
            "(status = 'fermee' AND closed_at IS NOT NULL AND montant_reel_cloture IS NOT NULL "
            "AND solde_theorique_cloture IS NOT NULL AND ecart IS NOT NULL)",
            name="statut_coherent_avec_cloture",
        ),
        {"schema": "caisse"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("parameters.agencies.id"), nullable=False
    )
    caissier_id: Mapped[uuid.UUID] = mapped_column(UUID, sa.ForeignKey(FK_USER), nullable=False)
    compte_caisse_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("comptabilite.accounts.id"), nullable=False
    )
    fonds_initial: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    closed_at: Mapped[datetime | None] = mapped_column(TS)
    montant_reel_cloture: Mapped[int | None] = mapped_column(sa.BigInteger)
    solde_theorique_cloture: Mapped[int | None] = mapped_column(sa.BigInteger)
    ecart: Mapped[int | None] = mapped_column(sa.BigInteger)
    status: Mapped[str] = mapped_column(
        sa.String(10), nullable=False, server_default=sa.text("'ouverte'")
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))

    def __repr__(self) -> str:
        return f"<CaisseSession {self.caissier_id} {self.status}>"
