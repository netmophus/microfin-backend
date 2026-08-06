"""Crédit CR5d — prélèvement automatique des échéances.

  - applications.compte_prelevement_id : le compte epargne.accounts À DÉBITER pour le
    prélèvement automatique, rempli EXPLICITEMENT (jamais recalculé) par
    credit.prelevement.configurer_prelevement() — PAS forcément au décaissement lui-même :
    un dossier déjà décaissé peut acquérir ce compte plus tard (aucun flux ne l'impose à cet
    instant précis). NULL = ce crédit n'est pas éligible au prélèvement automatique (guichet
    uniquement, CR6d).

  - credit.prelevement_tentatives : append-only, LE garde-fou anti-double-prélèvement —
    UNIQUE(installment_id, date_tentative). Un jour, une échéance, une seule tentative
    (montant_preleve peut être 0 si rien n'était disponible ce jour-là : le reliquat reste dû,
    retentable les jours suivants tant que l'échéance n'est pas soldée). Immuable (trigger),
    miroir exact de epargne.interet_calculs (E5) : mêmes garanties, même mécanique.

Revision ID: 0039
Revises: 0038
Create Date: 2026-08-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")
FK_USER = "security.users.id"


def upgrade() -> None:
    # --- Compte de prélèvement, ancré sur le dossier (miroir compte_destination_id, 0035) -----
    op.add_column(
        "applications",
        sa.Column("compte_prelevement_id", UUID, nullable=True),
        schema="credit",
    )
    op.create_foreign_key(
        "fk_credit_applications_compte_prelevement",
        "applications",
        "accounts",
        ["compte_prelevement_id"],
        ["id"],
        source_schema="credit",
        referent_schema="epargne",
    )

    # --- Anti-double-prélèvement, append-only (miroir epargne.interet_calculs, 0023) ----------
    op.create_table(
        "prelevement_tentatives",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("installment_id", UUID, nullable=False),
        sa.Column("date_tentative", sa.Date(), nullable=False),
        # Diagnostic — 0 si rien n'était disponible ce jour-là (compte à sec ou fermé).
        sa.Column("montant_preleve", sa.BigInteger(), nullable=False),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        # LE garde-fou anti-double-prélèvement : une échéance, un jour, une seule tentative.
        sa.UniqueConstraint("installment_id", "date_tentative", name="uq_prelevement_tentative"),
        sa.ForeignKeyConstraint(
            ["installment_id"], ["credit.installments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["created_by"], [FK_USER]),
        sa.CheckConstraint("montant_preleve >= 0", name="montant_preleve_positif"),
        schema="credit",
    )
    op.create_index(
        "ix_credit_prelevement_tentatives_installment",
        "prelevement_tentatives",
        ["installment_id"],
        schema="credit",
    )

    # --- Immuabilité (append-only) : une tentative archivée ne se réécrit pas -----------------
    op.execute(
        """
        CREATE FUNCTION credit.prelevement_tentative_immuable() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'Tentative de prelevement immuable : ni modification ni suppression'
            USING ERRCODE = 'restrict_violation';
        END $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_prelevement_tentative_immuable
          BEFORE UPDATE OR DELETE ON credit.prelevement_tentatives
          FOR EACH ROW EXECUTE FUNCTION credit.prelevement_tentative_immuable();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_prelevement_tentative_immuable "
        "ON credit.prelevement_tentatives"
    )
    op.execute("DROP FUNCTION IF EXISTS credit.prelevement_tentative_immuable()")
    op.drop_index(
        "ix_credit_prelevement_tentatives_installment",
        table_name="prelevement_tentatives",
        schema="credit",
    )
    op.drop_table("prelevement_tentatives", schema="credit")
    op.drop_constraint(
        "fk_credit_applications_compte_prelevement",
        "applications",
        schema="credit",
        type_="foreignkey",
    )
    op.drop_column("applications", "compte_prelevement_id", schema="credit")
