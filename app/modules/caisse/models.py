"""Modèles ORM du schéma « caisse » — postes (Bloc A, migration 0041), sessions (CA0/CA1,
migration 0040) et paramètres (CA2, migration 0043).

Mappent l'existant, ne créent rien. FK et CHECK reflètent EXACTEMENT la migration (exigence
d'alembic check pour les FK — les CHECK/index partiels ne sont pas comparés, la base les impose).

Module Caisse : ouverture/fermeture de journée PAR CAISSIER (jamais deux sessions ouvertes à la
fois pour le même caissier — UNIQUE partiel en base, `uq_caisse_sessions_caissier_ouverte`) ET,
depuis le Bloc A, PAR POSTE (`uq_caisse_sessions_poste_ouverte` — un poste n'a qu'une session
ouverte à la fois, quel que soit le caissier). Le solde théorique n'est jamais stocké en
continu — voir service.py::calculer_solde_theorique, calcul dérivé des écritures validées, même
philosophie que epargne.accounts.balance (cache, jamais une seconde vérité).

CA2 (migration 0043) : `CaisseParametres` porte le seuil de tolérance sur l'écart — SINGLETON,
même patron que `tiers.ShareParameters`. `CaisseSession.motif_ecart`/`valide_le`/`valide_par`
tracent le motif saisi à la fermeture et la validation a posteriori du responsable — AUCUNE
colonne de statut « à valider » : ce statut se DÉRIVE (fermée + écart significatif + non
validée), calculé par service.py, jamais stocké."""

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


class Poste(Base):
    """Un poste de caisse (guichet physique) — PLUSIEURS par agence (Bloc A). Le compte
    historique de chaque agence rattachée est devenu son premier poste (migration 0041,
    backfill). `compte_caisse_id` nullable, même discipline que l'ancien
    `Agency.compte_caisse_id` : un poste sans compte rattaché est un état légitime.

    CRUD (créer/renommer/(dés)activer) + rattachement comptable + assignation des guichetiers :
    `postes.py` (Bloc B), réservés respectivement à `caisse.poste.manage` et `compta.plan.manage`.

    `ouvrir_session()` (Bloc C) exige désormais un `poste_id` explicite, TOUJOURS soumis par le
    client — jamais déduit du côté serveur, même quand l'acteur n'a qu'un seul poste assigné.
    Le poste doit être actif, assigné à l'acteur (`PosteAssignation`) et dans l'agence courante
    de sa session."""

    __tablename__ = "postes"
    __table_args__: tuple[Any, ...] = (
        sa.UniqueConstraint("agency_id", "code", name="uq_caisse_postes_agency_code"),
        sa.Index("ix_caisse_postes_agency", "agency_id"),
        {"schema": "caisse"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("parameters.agencies.id"), nullable=False
    )
    code: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    libelle: Mapped[str] = mapped_column(sa.String(150), nullable=False)
    compte_caisse_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("comptabilite.accounts.id")
    )
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.true()
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))

    def __repr__(self) -> str:
        return f"<Poste {self.agency_id} {self.code}>"


class PosteAssignation(Base):
    """Affectation d'un guichetier à un poste (Bloc B) — mirroring EXACT de
    `security.UserAgency` (habilitation réseau, C6), même forme, précédent déjà en production.
    Association pure, clé composite, pas de surrogate id.

    Distincte de l'HABILITATION (`user_agencies` : qui peut se connecter où) — ceci est
    l'AFFECTATION opérationnelle (qui travaille à quel poste aujourd'hui), délibérément dans le
    schéma `caisse`, pas `security`."""

    __tablename__ = "poste_assignations"
    __table_args__: tuple[Any, ...] = (
        sa.Index("ix_caisse_poste_assignations_user", "user_id"),
        {"schema": "caisse"},
    )

    poste_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("caisse.postes.id"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey(FK_USER, ondelete="CASCADE"), primary_key=True
    )
    granted_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    granted_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))

    def __repr__(self) -> str:
        return f"<PosteAssignation {self.poste_id} {self.user_id}>"


class CaisseSession(Base):
    """Une session de caisse — CA1 : ouverture, fermeture, calcul/affichage de l'écart. AUCUNE
    écriture comptable posée par ce module en CA1 (voir CA3), aucun blocage sur écart (CA2).

    `poste_id` : le poste dont dépend cette session (Bloc A). `compte_caisse_id` : ANCRÉ à
    l'ouverture (copié depuis `poste.compte_caisse_id` à cet instant, plus depuis
    `Agency.compte_caisse_id` directement — voir migration 0041), jamais recalculé ensuite —
    même discipline que `compte_credit_id`/`compte_collectif_id` ailleurs dans ce projet."""

    __tablename__ = "sessions"
    __table_args__: tuple[Any, ...] = (
        sa.Index("ix_caisse_sessions_agency", "agency_id"),
        sa.Index("ix_caisse_sessions_caissier", "caissier_id"),
        sa.Index("ix_caisse_sessions_poste", "poste_id"),
        sa.Index(
            "uq_caisse_sessions_caissier_ouverte",
            "caissier_id",
            unique=True,
            postgresql_where=sa.text("status = 'ouverte'"),
        ),
        sa.Index(
            "uq_caisse_sessions_poste_ouverte",
            "poste_id",
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
        sa.CheckConstraint(
            "valide_le IS NULL OR status = 'fermee'", name="validation_apres_fermeture"
        ),
        {"schema": "caisse"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("parameters.agencies.id"), nullable=False
    )
    caissier_id: Mapped[uuid.UUID] = mapped_column(UUID, sa.ForeignKey(FK_USER), nullable=False)
    poste_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("caisse.postes.id"), nullable=False
    )
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
    # CA2 (migration 0043) : motif saisi à la fermeture (obligatoire si |ecart| > seuil, imposé
    # par service.py, jamais par un CHECK — le seuil est modifiable) ; validation a posteriori du
    # responsable, tracée mais jamais un blocage.
    motif_ecart: Mapped[str | None] = mapped_column(sa.Text)
    valide_le: Mapped[datetime | None] = mapped_column(TS)
    valide_par: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))

    def __repr__(self) -> str:
        return f"<CaisseSession {self.caissier_id} {self.status}>"


class CaisseParametres(Base):
    """Config d'INSTITUTION du module Caisse — une ligne, PROVISOIRE, même patron que
    `tiers.ShareParameters` : `seuil_tolerance` (F, CA2, migration 0043) comparé à `abs(ecart)`
    à la fermeture d'une session. `compte_ecart_manquant_id`/`compte_ecart_excedent_id` (CA3,
    migration 0044) : DEUX comptes distincts, jamais un signe négatif sur un seul — la charge
    quand le réel est INFÉRIEUR au théorique, le produit quand il est SUPÉRIEUR. Nullable : un
    rattachement absent est un état LÉGITIME (paramétrage incomplet), refusé proprement par
    `service.py::poser_ecriture_ecart` au moment de poser l'écriture — jamais deviné.

    `singleton` garantit l'unicité de la ligne DÈS LA CRÉATION (CHECK+UNIQUE posés dans la
    migration elle-même — pas en retrofit comme `share_parameters`, qui l'a reçu après coup en
    0029). Jamais lu ni écrit depuis Python."""

    __tablename__ = "parametres"
    __table_args__: tuple[Any, ...] = (
        sa.CheckConstraint("seuil_tolerance >= 0", name="seuil_tolerance_positif"),
        sa.CheckConstraint("singleton", name="caisse_parametres_singleton_check"),
        sa.UniqueConstraint("singleton", name="caisse_parametres_singleton_unique"),
        {"schema": "caisse"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    seuil_tolerance: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, server_default=sa.text("500")
    )
    is_provisional: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.true()
    )
    singleton: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=NOW)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey(FK_USER))
    compte_ecart_manquant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("comptabilite.accounts.id")
    )
    compte_ecart_excedent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("comptabilite.accounts.id")
    )

    def __repr__(self) -> str:
        return f"<CaisseParametres seuil={self.seuil_tolerance}>"
