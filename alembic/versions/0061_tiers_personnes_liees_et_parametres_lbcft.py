"""Tiers — personnes liées aux personnes morales et groupements, et paramètres LBC/FT.

Révélé par MIZNAS : une SARL ou un groupement n'avait aucun moyen d'enregistrer ses dirigeants,
son bureau, ses bénéficiaires effectifs ni ses signataires.

1. `tiers.tier_related_persons` : lien entre une PM / un groupement (`tier_id`) et une PERSONNE
   PHYSIQUE (`individual_tier_id`), avec un rôle, une fonction, un pourcentage de détention
   (bénéficiaire effectif), un pouvoir de signature et une période (date_debut / date_fin).
   date_debut est une date MÉTIER, obligatoire et sans valeur par défaut : nomination au PV,
   entrée en fonction, jamais la date de saisie.
   Suppression logique (motif conservé), colonnes d'audit comme `identity_documents`.
   La base garantit ce qu'elle peut : rôle connu, pourcentage entre 0 et 100 et obligatoire pour un
   bénéficiaire effectif, période cohérente, pas de lien d'une fiche avec elle-même, et un seul lien
   ouvert par (fiche, personne, rôle). Le TYPE des deux fiches (PM/groupement d'un côté, personne
   physique de l'autre) est contrôlé par le service : un CHECK ne peut pas lire une autre table.

2. `tiers.parametres_lbcft` : paramètres de conformité LBC/FT, une seule ligne (id = 1). Porte pour
   l'instant le seuil de détention au-delà duquel une personne est signalée bénéficiaire effectif.
   `is_provisional` dit que la valeur attend la confirmation de la loi uniforme LBC/FT UEMOA. La
   ligne n'est PAS créée ici : elle est semée par le script de données ; sans ligne, aucun
   indicateur n'est affiché (on n'invente pas de seuil).

Revision ID: 0061
Revises: 0060
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0061"
down_revision: str | None = "0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLES = "('representant_legal','dirigeant','membre_bureau','beneficiaire_effectif','signataire')"
_UUID = postgresql.UUID(as_uuid=True)
_NOW = sa.text("NOW()")


def upgrade() -> None:
    op.create_table(
        "tier_related_persons",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tier_id", _UUID, sa.ForeignKey("tiers.tiers.id"), nullable=False),
        sa.Column("individual_tier_id", _UUID, sa.ForeignKey("tiers.tiers.id"), nullable=False),
        sa.Column("role", sa.String(30), nullable=False),
        sa.Column("fonction", sa.String(100)),
        sa.Column("pourcentage_detention", sa.Numeric(5, 2)),
        sa.Column("pouvoir_signature", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("date_debut", sa.Date(), nullable=False),  # date métier, jamais la saisie
        sa.Column("date_fin", sa.Date()),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("created_by", _UUID, sa.ForeignKey("security.users.id")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("updated_by", _UUID, sa.ForeignKey("security.users.id")),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("deleted_by", _UUID, sa.ForeignKey("security.users.id")),
        sa.Column("deletion_reason", sa.Text()),
        sa.CheckConstraint(f"role IN {_ROLES}", name="role_valide"),
        sa.CheckConstraint(
            "pourcentage_detention IS NULL OR (pourcentage_detention >= 0 "
            "AND pourcentage_detention <= 100)",
            name="pourcentage_entre_0_et_100",
        ),
        sa.CheckConstraint(
            "role <> 'beneficiaire_effectif' OR pourcentage_detention IS NOT NULL",
            name="beneficiaire_effectif_a_un_pourcentage",
        ),
        sa.CheckConstraint("tier_id <> individual_tier_id", name="pas_de_lien_avec_soi_meme"),
        sa.CheckConstraint(
            "date_fin IS NULL OR date_fin >= date_debut", name="periode_coherente"
        ),
        schema="tiers",
    )
    op.create_index(
        "ix_tier_related_persons_tier_id",
        "tier_related_persons",
        ["tier_id"],
        schema="tiers",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_tier_related_persons_individual_tier_id",
        "tier_related_persons",
        ["individual_tier_id"],
        schema="tiers",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    # Un seul lien OUVERT (sans date de fin) par fiche, personne et rôle. Un lien terminé ne
    # bloque pas un nouveau lien (une personne peut redevenir dirigeant).
    op.create_index(
        "uq_tier_related_persons_ouvert",
        "tier_related_persons",
        ["tier_id", "individual_tier_id", "role"],
        unique=True,
        schema="tiers",
        postgresql_where=sa.text("deleted_at IS NULL AND date_fin IS NULL"),
    )

    op.create_table(
        "parametres_lbcft",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("seuil_beneficiaire_effectif", sa.Numeric(5, 2), nullable=False),
        sa.Column("is_provisional", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("updated_by", _UUID, sa.ForeignKey("security.users.id")),
        sa.CheckConstraint("id = 1", name="ligne_unique"),
        sa.CheckConstraint(
            "seuil_beneficiaire_effectif > 0 AND seuil_beneficiaire_effectif <= 100",
            name="seuil_entre_0_et_100",
        ),
        schema="tiers",
    )


def downgrade() -> None:
    op.drop_table("parametres_lbcft", schema="tiers")
    op.drop_table("tier_related_persons", schema="tiers")  # emporte ses index
