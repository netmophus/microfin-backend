"""Modèle ORM de l'index des documents archivés (migration 0067).

Index IMMUABLE (trigger : ni UPDATE ni DELETE) : un document archivé ne se modifie pas et ne se
supprime pas. Une génération qui échoue laisse une ligne `echec` (historique) ; sa régénération
POSE UNE NOUVELLE LIGNE `ok` — d'où l'unicité (type, clé naturelle) limitée aux lignes `ok`.
Les CHECK et le trigger ne sont pas redéclarés ici : la base les impose.
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
STATUT_OK = "ok"
STATUT_ECHEC = "echec"


class Archive(Base):
    """Un document PDF/A archivé (ou la trace d'une génération échouée)."""

    __tablename__ = "archives"
    __table_args__: tuple[Any, ...] = (
        sa.Index("ix_documents_archives_journee_module", "journee_comptable", "module"),
        sa.Index("ix_documents_archives_agence", "agency_id"),
        sa.Index("ix_documents_archives_piece", "numero_piece"),
        sa.Index(
            "uq_documents_archives_cle_ok",
            "type",
            "cle_naturelle",
            unique=True,
            postgresql_where=sa.text("statut = 'ok'"),
        ),
        sa.Index(
            "uq_documents_archives_chemin_ok",
            "chemin_relatif",
            unique=True,
            postgresql_where=sa.text("statut = 'ok'"),
        ),
        {"schema": "documents"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    type: Mapped[str] = mapped_column(sa.String(60), nullable=False)
    module: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    # Identité du document dans son type (ex. « <compte>:<pièce> ») : rend la génération
    # idempotente — une seconde demande pour la même clé retrouve le document, n'en crée pas un.
    cle_naturelle: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    # JOURNÉE COMPTABLE de l'opération : elle date le fichier et son dossier, jamais l'horloge.
    journee_comptable: Mapped[date] = mapped_column(sa.Date, nullable=False)
    chemin_relatif: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    sha256: Mapped[str | None] = mapped_column(sa.CHAR(64))
    taille: Mapped[int | None] = mapped_column(sa.BigInteger)
    numero_piece: Mapped[str | None] = mapped_column(sa.String(60))
    objet_lie_type: Mapped[str | None] = mapped_column(sa.String(60))
    objet_lie_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    agency_id: Mapped[uuid.UUID] = mapped_column(
        UUID, sa.ForeignKey("parameters.agencies.id"), nullable=False
    )
    acteur_id: Mapped[uuid.UUID | None] = mapped_column(UUID, sa.ForeignKey("security.users.id"))
    # Les données EXACTES du document (ce qui a été imprimé) : trace d'audit, et seule source pour
    # régénérer un document dont la génération a échoué.
    contexte: Mapped[dict[str, Any]] = mapped_column(postgresql.JSONB, nullable=False)
    statut: Mapped[str] = mapped_column(sa.String(10), nullable=False)
    erreur: Mapped[str | None] = mapped_column(sa.Text)
    # Horodatage RÉEL (audit) : n'entre jamais dans le contenu du document.
    cree_le: Mapped[datetime] = mapped_column(
        TS, nullable=False, server_default=sa.text("NOW()")
    )

    def __repr__(self) -> str:
        return f"<Archive {self.type} {self.cle_naturelle} {self.statut}>"


class Numerotation(Base):
    """Compteur des numéros de pièce propres aux documents (ex. arrêté de caisse AC-2026-000001),
    par préfixe et par année de journée comptable (migration 0068). Même mécanisme que la
    numérotation des pièces comptables : une seule instruction sous verrou de ligne."""

    __tablename__ = "numerotation"
    __table_args__: tuple[Any, ...] = ({"schema": "documents"},)

    prefixe: Mapped[str] = mapped_column(sa.String(10), primary_key=True)
    annee: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    last_value: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
