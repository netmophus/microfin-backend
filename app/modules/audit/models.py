"""Modèle du schéma « audit » — audit.audit_logs, en LECTURE SEULE côté ORM.

Référence : « Socle Sécurité & Administration — Conception validée » v1.0, §3.2.

Pourquoi la lecture seule est imposée ici, en Python, alors que la base se défend déjà :

  - UPDATE et DELETE sont rejetés par le trigger audit_logs_immutable() (migration 0002).
    L'ORM n'a rien à ajouter, sinon un message d'erreur lisible au lieu d'une
    DBAPIError opaque remontée du serveur.
  - L'INSERT, lui, reste ouvert en base — il le doit, c'est par là que le journal se
    remplit. Rien n'empêcherait donc un développeur d'écrire session.add(AuditLog(...)).
    Ce serait une erreur : le chain_hash est calculé par le trigger de la 0003, et une
    ligne insérée hors du service d'audit contournerait le contrôle transactionnel du
    C5 (« pas de trace, pas d'opération »). Le garde-fou ci-dessous ferme cette porte au
    flush, avant d'atteindre la base.

Écrire dans le journal passe donc par le service d'audit (à venir), jamais par ce modèle.

Ce module n'importe pas security.models : sa FK vers users est déclarée par chaîne. Il
ne déclare pas non plus de relationship vers User — le journal doit rester lisible même
si le compte a disparu, et le service de lecture joindra explicitement s'il en a besoin.
"""

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, Mapper, mapped_column

from app.core.database import Base

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")


class EcritureAuditInterditeError(RuntimeError):
    """Levée quand du code tente d'écrire dans audit.audit_logs via l'ORM."""


class AuditLog(Base):
    """Entrée du journal d'audit — immuable, chaînée, partitionnée par mois.

    Mappe la table PARENTE. Les partitions mensuelles (audit_logs_2026_01…12) ne sont pas
    mappées et n'ont pas à l'être : PostgreSQL route les lectures et les écritures depuis
    le parent. Les partitions 2027+ viendront du job C13.

    PK composite (id, occurred_at) : la clé de partition doit en faire partie.

    agency_id et resource_id n'ont volontairement pas de FK (§3.2) : le journal doit
    survivre à la disparition de l'objet audité.
    """

    __tablename__ = "audit_logs"
    __table_args__ = ({"schema": "audit"},)

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, server_default=GEN_UUID)
    # Clé de partition. NOW() = heure de DÉBUT de transaction : cet ordre n'est pas
    # l'ordre d'insertion (cf. audit_chain_head, migration 0003).
    occurred_at: Mapped[datetime] = mapped_column(TS, primary_key=True, server_default=NOW)
    # NULL = échec de login sur un compte inconnu.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, sa.ForeignKey("security.users.id"), nullable=True
    )
    action: Mapped[str] = mapped_column(sa.String(60), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(sa.String(50), nullable=True)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID, nullable=True)
    old_values: Mapped[dict[str, Any] | None] = mapped_column(postgresql.JSONB(), nullable=True)
    new_values: Mapped[dict[str, Any] | None] = mapped_column(postgresql.JSONB(), nullable=True)
    agency_id: Mapped[uuid.UUID | None] = mapped_column(UUID, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(postgresql.INET(), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    request_id: Mapped[uuid.UUID | None] = mapped_column(UUID, nullable=True)
    # C15 — poste de travail (BCEAO). Réservé, non alimenté pour l'instant.
    workstation_id: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    # Calculés par le trigger de la 0003 ; toute valeur fournie par l'appelant est écrasée.
    chain_hash: Mapped[str] = mapped_column(sa.CHAR(64), nullable=False)
    previous_chain_hash: Mapped[str | None] = mapped_column(sa.CHAR(64), nullable=True)

    def __repr__(self) -> str:
        return f"<AuditLog {self.action} {self.occurred_at:%Y-%m-%d %H:%M:%S%z}>"


def _refuser_ecriture(operation: str) -> str:
    return (
        f"audit.audit_logs est en lecture seule via l'ORM ({operation} refusé). "
        "Le journal s'écrit par le service d'audit, qui laisse le trigger de chaînage "
        "calculer chain_hash ; UPDATE et DELETE sont de toute façon rejetés en base "
        "(immuabilité, migration 0002)."
    )


@event.listens_for(AuditLog, "before_insert", propagate=True)
def _interdire_insert(mapper: Mapper[AuditLog], connection: Connection, target: AuditLog) -> None:
    raise EcritureAuditInterditeError(_refuser_ecriture("INSERT"))


@event.listens_for(AuditLog, "before_update", propagate=True)
def _interdire_update(mapper: Mapper[AuditLog], connection: Connection, target: AuditLog) -> None:
    raise EcritureAuditInterditeError(_refuser_ecriture("UPDATE"))


@event.listens_for(AuditLog, "before_delete", propagate=True)
def _interdire_delete(mapper: Mapper[AuditLog], connection: Connection, target: AuditLog) -> None:
    raise EcritureAuditInterditeError(_refuser_ecriture("DELETE"))
