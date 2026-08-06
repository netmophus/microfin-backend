"""Modèles ORM du schéma « credit » — référentiel produit (migration 0031), demandes et
décision (migration 0032), décaissement et échéancier persisté (migration 0033), remboursements
(migration 0034), décaissement multi-mode (migration 0035), paliers de souffrance CR5a
(migration 0036), paiement partiel CR5b (migration 0037).

Mappent l'existant, ne créent rien. FK et CHECK reflètent EXACTEMENT les migrations (exigence
d'alembic check pour les FK — les CHECK/triggers ne sont pas comparés, la base les impose).

Module Crédit (individuel simple, échéances fixes, membres ET clients). Impayés/
provisionnement (CR5) : CR5a (paramétrage des paliers) et CR5b (paiement partiel, ce module)
posés ; reclassification automatique (CR5c) bloquée en attendant les règles de l'expert-
comptable (voir docs/conformite-credit.md §2).
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
    # Produit d'intérêts (CR4) — UN SEUL compte, PAS de split membre/client : les intérêts
    # perçus sont un produit de l'institution, pas une dette envers le tiers (7021, officiel
    # direct, contrairement au capital qui a un vrai trou membre/client à combler).
    compte_produits_interets_id: Mapped[uuid.UUID | None] = mapped_column(
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
    # Mode de décaissement (migration 0035) : 'caisse' (D crédit/C caisse) ou 'epargne' (D
    # crédit/C un compte epargne.accounts du tiers, choisi au décaissement — n'importe quel
    # produit). compte_destination_id est rempli dans LES DEUX cas (miroir de compte_credit_id
    # côté créance) : la caisse utilisée, ou le compte du tiers crédité.
    mode_decaissement: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, server_default=sa.text("'caisse'")
    )
    compte_destination_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey(FK_ACCOUNT)
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))

    def __repr__(self) -> str:
        return f"<Application {self.application_number} {self.status}>"


class Installment(Base):
    """Une échéance PERSISTÉE d'un crédit décaissé — résultat figé de generer_echeancier()
    (CR2, pur) au moment du décaissement. Reste le PLAN prévisionnel, immuable après coup
    (voir Repayment pour le registre de ce qui a réellement été encaissé).

    due_date : date calendaire, calculée par pas de période depuis la date de décaissement
    (stdlib, voir decaissement._ajouter_periode).

    status : 'a_echoir' -> 'partiellement_paye' -> 'paye' (CR5b — paiement partiel, migration
    0037). `montant_paye` est le cumul encaissé ; `solde_du` (total - montant_paye) n'est
    JAMAIS stocké, calculé à la lecture. Toujours RIEN pour « en retard » — condition calculée
    à la lecture (due_date < aujourd'hui AND status != 'paye'), pas un état stocké."""

    __tablename__ = "installments"
    __table_args__: tuple[Any, ...] = (
        sa.UniqueConstraint("application_id", "numero"),
        sa.Index("ix_credit_installments_application", "application_id"),
        sa.CheckConstraint(
            "status IN ('a_echoir', 'partiellement_paye', 'paye')", name="status"
        ),
        sa.CheckConstraint(
            "montant_paye >= 0 AND montant_paye <= total", name="montant_paye_borne"
        ),
        sa.CheckConstraint(
            "(status = 'a_echoir' AND montant_paye = 0) OR "
            "(status = 'partiellement_paye' AND montant_paye > 0 AND montant_paye < total) OR "
            "(status = 'paye' AND montant_paye = total)",
            name="statut_coherent_avec_montant_paye",
        ),
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
    montant_paye: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, server_default=sa.text("0")
    )
    paid_at: Mapped[datetime | None] = mapped_column(TS)
    paid_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)

    def __repr__(self) -> str:
        return f"<Installment {self.application_id} #{self.numero}>"


class Repayment(Base):
    """Un paiement RÉELLEMENT encaissé — registre append-only (miroir epargne.movements).

    installment_id N'EST PLUS unique depuis CR5b (migration 0037) : plusieurs paiements
    successifs peuvent viser la même échéance (paiement partiel, jusqu'à solde). Chaque ligne
    représente UN paiement, avec SA propre ventilation capital/intérêts (montant_capital/
    montant_interets décrivent ce que CE paiement a couvert, pas l'échéance entière). entry_id
    référence la pièce comptable qui l'a posé (D CAISSE / C CREDIT / C PRODUITS_INTERETS)."""

    __tablename__ = "repayments"
    __table_args__: tuple[Any, ...] = (
        sa.Index("ix_credit_repayments_application", "application_id"),
        sa.Index("ix_credit_repayments_installment", "installment_id"),
        {"schema": "credit"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    installment_id: Mapped[uuid.UUID] = mapped_column(
        UUID,
        sa.ForeignKey("credit.installments.id", ondelete="CASCADE"),
        nullable=False,
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey(FK_APPLICATION), nullable=False
    )
    montant_capital: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    montant_interets: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    montant_total: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("comptabilite.journal_entries.id"), nullable=False
    )
    paid_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    paid_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)

    def __repr__(self) -> str:
        return f"<Repayment {self.installment_id} {self.montant_total}>"


class DelinquencyTier(Base):
    """Un PALIER de souffrance (CR5a, migration 0036) — paramétrage institution, PLUSIEURS
    lignes (pas un singleton comme ShareParameters). La machine à états créance saine ->
    impayée -> douteuse -> irrécouvrable se lit en triant sur `seuil_jours`, qui sert LUI-MÊME
    de clé d'ordre : pas de colonne `ordre` séparée, aucune chance qu'elle diverge du seuil.

    `compte_encours_id`/`compte_dotation_id` sont VOLONTAIREMENT découplés (voir
    docs/conformite-credit.md §2 : la classe 29 a 3 tranches officielles, la classe 664 en a 4,
    aucune correspondance terme à terme) — deux paliers peuvent partager le même compte
    d'encours tout en ayant des comptes de dotation distincts. Comptes NULL par défaut, remplis
    via l'écran de paramétrage (Bloc 5), jamais codés en dur.

    Aucun comportement automatique ne lit encore cette table en CR5a — paramétrage seul."""

    __tablename__ = "delinquency_tiers"
    __table_args__: tuple[Any, ...] = ({"schema": "credit"},)

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    code: Mapped[str] = mapped_column(sa.String(20), nullable=False, unique=True)
    libelle: Mapped[str] = mapped_column(sa.String(150), nullable=False)
    seuil_jours: Mapped[int] = mapped_column(sa.Integer, nullable=False, unique=True)
    taux_provision_bp: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0")
    )
    compte_encours_id: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_ACCOUNT))
    compte_dotation_id: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_ACCOUNT))
    is_terminal: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )
    is_provisional: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.true()
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))

    def __repr__(self) -> str:
        return f"<DelinquencyTier {self.code} seuil={self.seuil_jours}j>"
