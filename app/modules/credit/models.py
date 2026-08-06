"""Modèles ORM du schéma « credit » — référentiel produit (migration 0031), demandes et
décision (migration 0032), décaissement et échéancier persisté (migration 0033), remboursements
(migration 0034), décaissement multi-mode (migration 0035), paliers de souffrance CR5a
(migration 0036), paiement partiel CR5b (migration 0037), reclassification automatique CR5c
(migration 0038), prélèvement automatique CR5d (migration 0039).

Mappent l'existant, ne créent rien. FK et CHECK reflètent EXACTEMENT les migrations (exigence
d'alembic check pour les FK — les CHECK/triggers ne sont pas comparés, la base les impose).

Module Crédit (individuel simple, échéances fixes, membres ET clients). Impayés/
provisionnement (CR5) complet : CR5a (paliers), CR5b (paiement partiel), CR5c (reclassification
automatique), CR5d (prélèvement automatique, ce module).
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
        sa.Index("ix_credit_applications_delinquency_tier", "delinquency_tier_id"),
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
    # CR5c (migration 0038) : classification COURANTE, posée par le job de reclassification —
    # NULL = sain. Jamais recalculée à la lecture (contrairement à « soldé », dérivé des
    # installments) : c'est justement ce que ce champ trace, un état qui persiste entre deux
    # exécutions du job. `rembourser()` s'en sert pour créditer le compte courant de l'encours
    # (le palier si classé, sinon `compte_credit_id`) plutôt que toujours l'ancrage figé.
    delinquency_tier_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("credit.delinquency_tiers.id")
    )
    # CR5d (migration 0039) : le compte epargne.accounts À DÉBITER pour le prélèvement
    # automatique — rempli EXPLICITEMENT par prelevement.configurer_prelevement(), jamais
    # recalculé. NULL = ce crédit n'est pas éligible (guichet CR6d uniquement).
    compte_prelevement_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("epargne.accounts.id")
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

    `compte_provision_id`/`compte_reprise_id` (CR5c, migration 0038) : deux comptes de plus,
    oubliés en CR5a (paramétrage seul, rien ne les lisait encore) — `compte_provision_id` est
    le compte de BILAN (299x, contra-actif) qui porte la provision accumulée elle-même,
    `compte_reprise_id` sert au mouvement inverse de la dotation quand la provision diminue ou
    s'annule. Le référentiel RCSFD n'a qu'un compte de reprise (764, sans sous-tranches) mais
    reste paramétrable par palier — même discipline que les autres, aucune exception codée en
    dur.

    Lu depuis CR5c par le job de reclassification (`app/modules/credit/reclassification.py`)."""

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
    compte_provision_id: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_ACCOUNT))
    compte_reprise_id: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_ACCOUNT))
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


class DelinquencyEvent(Base):
    """Un reclassement (CR5c, migration 0038) — IMMUABLE, jamais modifié après coup (registre,
    même philosophie que Repayment). `tier_avant_id`/`tier_apres_id` NULL = sain à ce moment-là.

    `entry_id_encours`/`entry_id_reprise`/`entry_id_dotation` peuvent être NULL : le moteur
    comptable refuse toute ligne à montant nul, donc une ligne n'est postée QUE si son montant
    calculé est > 0 — règle unique, jamais de cas spécial. La provision n'est JAMAIS nettée en
    delta (chaque palier a son propre compte 299x, pas de pool commun) : `entry_id_reprise`
    reprend intégralement la provision de l'ancien palier, `entry_id_dotation` redote
    intégralement celle du nouveau — les deux peuvent coexister. L'événement est écrit dans
    tous les cas : il documente le reclassement même quand aucune écriture n'a été nécessaire.
    """

    __tablename__ = "delinquency_events"
    __table_args__: tuple[Any, ...] = (
        sa.Index("ix_credit_delinquency_events_application", "application_id"),
        {"schema": "credit"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey(FK_APPLICATION), nullable=False
    )
    executed_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    executed_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    jours_retard: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    tier_avant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("credit.delinquency_tiers.id")
    )
    tier_apres_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("credit.delinquency_tiers.id")
    )
    encours_actuel: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    montant_encours_reclasse: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, server_default=sa.text("0")
    )
    provision_avant: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    provision_apres: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    entry_id_encours: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("comptabilite.journal_entries.id")
    )
    entry_id_reprise: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("comptabilite.journal_entries.id")
    )
    entry_id_dotation: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("comptabilite.journal_entries.id")
    )

    def __repr__(self) -> str:
        return f"<DelinquencyEvent {self.application_id} jours_retard={self.jours_retard}>"


class PrelevementTentative(Base):
    """Une tentative de prélèvement automatique (CR5d, migration 0039) — IMMUABLE, append-only
    (trigger, miroir InteretCalcul). LE garde-fou anti-double-prélèvement : UNIQUE(installment_id,
    date_tentative) — une échéance ne peut avoir qu'UNE tentative par jour de traitement, mais
    reste retentable les jours suivants tant qu'elle n'est pas soldée. `montant_preleve` peut
    être 0 (rien de disponible ce jour-là : compte à sec ou fermé) — la ligne existe quand même,
    elle documente la tentative."""

    __tablename__ = "prelevement_tentatives"
    __table_args__: tuple[Any, ...] = (
        sa.UniqueConstraint("installment_id", "date_tentative", name="uq_prelevement_tentative"),
        sa.Index("ix_credit_prelevement_tentatives_installment", "installment_id"),
        sa.CheckConstraint("montant_preleve >= 0", name="montant_preleve_positif"),
        {"schema": "credit"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    installment_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("credit.installments.id", ondelete="CASCADE"), nullable=False
    )
    date_tentative: Mapped[date] = mapped_column(sa.Date, nullable=False)
    montant_preleve: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))

    def __repr__(self) -> str:
        return f"<PrelevementTentative {self.installment_id} {self.date_tentative}>"
