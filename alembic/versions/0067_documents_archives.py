"""Documents — index IMMUABLE des documents archivés par le serveur (PDF/A).

Schéma `documents`, table `archives` : un document archivé (type, module, journée comptable,
chemin relatif, empreinte SHA-256, pièce et objet liés, agence, acteur) ou la trace d'une
génération échouée (`statut = 'echec'`, avec le message technique).

Immuable : un trigger interdit UPDATE et DELETE (même mécanisme que les mouvements d'épargne). Une
régénération pose donc une NOUVELLE ligne `ok` ; l'unicité (type, clé naturelle) et celle du chemin
ne portent que sur les lignes `ok` (index uniques partiels) : plusieurs échecs historisés d'une même
clé sont permis, deux documents `ok` pour la même clé ne le sont jamais.

Revision ID: 0067
Revises: 0066
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0067"
down_revision: str | None = "0066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS documents")
    op.create_table(
        "archives",
        sa.Column("id", UUID, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("type", sa.String(60), nullable=False),
        sa.Column("module", sa.String(40), nullable=False),
        sa.Column("cle_naturelle", sa.String(200), nullable=False),
        sa.Column("journee_comptable", sa.Date(), nullable=False),
        sa.Column("chemin_relatif", sa.String(500), nullable=False),
        sa.Column("sha256", sa.CHAR(64)),
        sa.Column("taille", sa.BigInteger()),
        sa.Column("numero_piece", sa.String(60)),
        sa.Column("objet_lie_type", sa.String(60)),
        sa.Column("objet_lie_id", UUID),
        sa.Column("agency_id", UUID, nullable=False),
        sa.Column("acteur_id", UUID),
        sa.Column("contexte", postgresql.JSONB, nullable=False),
        sa.Column("statut", sa.String(10), nullable=False),
        sa.Column("erreur", sa.Text()),
        sa.Column("cree_le", TS, server_default=sa.text("NOW()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["agency_id"], ["parameters.agencies.id"]),
        sa.ForeignKeyConstraint(["acteur_id"], ["security.users.id"]),
        sa.CheckConstraint("statut IN ('ok', 'echec')", name="ck_archives_statut"),
        sa.CheckConstraint(
            "statut <> 'ok' OR (sha256 IS NOT NULL AND taille IS NOT NULL AND taille > 0)",
            name="ck_archives_ok_complet",
        ),
        sa.CheckConstraint(
            "statut <> 'echec' OR erreur IS NOT NULL", name="ck_archives_echec_explique"
        ),
        schema="documents",
    )
    op.create_index(
        "ix_documents_archives_journee_module",
        "archives",
        ["journee_comptable", "module"],
        schema="documents",
    )
    op.create_index("ix_documents_archives_agence", "archives", ["agency_id"], schema="documents")
    op.create_index(
        "ix_documents_archives_piece", "archives", ["numero_piece"], schema="documents"
    )
    op.create_index(
        "uq_documents_archives_cle_ok",
        "archives",
        ["type", "cle_naturelle"],
        unique=True,
        schema="documents",
        postgresql_where=sa.text("statut = 'ok'"),
    )
    op.create_index(
        "uq_documents_archives_chemin_ok",
        "archives",
        ["chemin_relatif"],
        unique=True,
        schema="documents",
        postgresql_where=sa.text("statut = 'ok'"),
    )
    op.execute(
        """
        CREATE FUNCTION documents.archive_immuable() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'Archive de documents immuable : ni modification ni suppression'
            USING ERRCODE = 'restrict_violation';
        END $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_archive_immuable
          BEFORE UPDATE OR DELETE ON documents.archives
          FOR EACH ROW EXECUTE FUNCTION documents.archive_immuable();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_archive_immuable ON documents.archives")
    op.execute("DROP FUNCTION IF EXISTS documents.archive_immuable()")
    op.drop_table("archives", schema="documents")
    op.execute("DROP SCHEMA IF EXISTS documents")
