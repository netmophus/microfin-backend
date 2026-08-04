"""Modèles ORM du schéma « credit » — référentiel produit (migration 0031), demandes et
décision (migration 0032), décaissement et échéancier persisté (migration 0033).

Mappent l'existant, ne créent rien. FK et CHECK reflètent EXACTEMENT les migrations (exigence
d'alembic check pour les FK — les CHECK/triggers ne sont pas comparés, la base les impose).

Module Crédit (individuel simple, échéances fixes, membres ET clients). Remboursements
(retards/provisionnement) : blocs suivants (CR4+).
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
FK_ACCOUNT = "comptabilite.accounts.id"
FK_TIER = "tiers.tiers.id"
FK_AGENCY = "parameters.agencies.id"
FK_PRODUCT = "credit.products.id"
FK_APPLICATION = "credit.applications.id"


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


class NumberingSequence(Base):
    """Compteur atomique du numéro de dossier, par (prefix, year) — patron épargne/tiers."""

    __tablename__ = "numbering_sequences"
    __table_args__: tuple[Any, ...] = ({"schema": "credit"},)

    prefix: Mapped[str] = mapped_column(sa.String(10), primary_key=True)
    year: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    last_value: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, server_default=sa.text("0")
    )
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)


class Application(Base):
    """Une demande de crédit — de la création au décaissement. États : en_instruction ->
    approuve | refuse -> decaisse (CR3, uniquement depuis approuve). La décision est UNIQUE
    et définitive (voir demandes.decider) ; le décaissement aussi (voir decaissement.decaisser).

    compte_credit_id : l'ANCRAGE membre/client (202211/202221 ou 203111/203121 selon le
    produit), résolu une fois au décaissement, jamais re-routé ensuite — miroir exact de
    epargne.accounts.compte_collectif_id (PS3)."""

    __tablename__ = "applications"
    __table_args__: tuple[Any, ...] = (
        sa.Index("ix_credit_applications_tier", "tier_id"),
        sa.Index("ix_credit_applications_agency", "agency_id"),
        {"schema": "credit"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    application_number: Mapped[str] = mapped_column(sa.String(30), nullable=False, unique=True)
    tier_id: Mapped[uuid.UUID] = mapped_column(UUID, sa.ForeignKey(FK_TIER), nullable=False)
    agency_id: Mapped[uuid.UUID] = mapped_column(UUID, sa.ForeignKey(FK_AGENCY), nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(UUID, sa.ForeignKey(FK_PRODUCT), nullable=False)
    montant_demande: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    duree_echeances: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    objet: Mapped[str | None] = mapped_column(sa.Text)
    status: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, server_default=sa.text("'en_instruction'")
    )
    montant_decide: Mapped[int | None] = mapped_column(sa.BigInteger)
    decided_at: Mapped[datetime | None] = mapped_column(TS)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    motif_decision: Mapped[str | None] = mapped_column(sa.Text)
    disbursed_at: Mapped[datetime | None] = mapped_column(TS)
    disbursed_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    compte_credit_id: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_ACCOUNT))
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))

    def __repr__(self) -> str:
        return f"<Application {self.application_number} {self.status}>"


class Installment(Base):
    """Une échéance PERSISTÉE d'un crédit décaissé — résultat figé de generer_echeancier()
    (CR2, pur) au moment du décaissement. due_date : date calendaire, calculée par pas de
    période depuis la date de décaissement (stdlib, voir decaissement._ajouter_periode).

    status sans CHECK pour l'instant : le vocabulaire des états (payé/en retard/...) est
    celui de CR4 (remboursements), pas encore décidé — colonne posée en avance pour éviter
    une migration disruptive plus tard, sa contrainte attend les règles réelles."""

    __tablename__ = "installments"
    __table_args__: tuple[Any, ...] = (
        sa.UniqueConstraint("application_id", "numero"),
        sa.Index("ix_credit_installments_application", "application_id"),
        {"schema": "credit"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey(FK_APPLICATION, ondelete="CASCADE"), nullable=False
    )
    numero: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    due_date: Mapped[date] = mapped_column(sa.Date, nullable=False)
    capital: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    interets: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    total: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    capital_restant_du: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, server_default=sa.text("'a_echoir'")
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)

    def __repr__(self) -> str:
        return f"<Installment {self.application_id} #{self.numero}>"
