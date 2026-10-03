"""Conformité — socle des ratios prudentiels RCSFD (lot P2.1.a), ENTIÈREMENT PARAMÉTRABLE.

Nouveau schéma `conformite`. PRINCIPE CARDINAL : aucune formule, aucun seuil, aucune
composition d'agrégat codée en dur dans l'application — tout vit dans ces cinq tables. Un
changement de formule, de seuil, ou un 11e ratio se fait par une ligne de donnée (écran de
paramétrage, lot P2.1.d), jamais par un déploiement.

MODÈLE :
  - agregat_prudentiel : définit un agrégat réglementaire. `type = 'BALANCE'` -> somme signée
    de soldes de comptes RCSFD par préfixe, à une date d'arrêté (composition dans
    agregat_compte). `type = 'SPECIAL'` -> calcul dédié, non réductible à une somme de comptes
    (ex. encours du plus gros emprunteur, agrégation par tiers) ; `calcul_special` nomme alors
    la fonction que le moteur (lot P2.1.b) devra dispatcher.
  - agregat_compte : la composition d'un agrégat BALANCE — un préfixe de numéro de compte
    RCSFD (`account_number LIKE prefixe || '%'`, lot P2.1.b) + un sens (+1 absorbe, -1 déduit —
    modélise les comptes contra et les déductions réglementaires, ex. capital non appelé en
    moins des fonds propres).
  - ratio_prudentiel : un ratio = deux agrégats (numérateur, dénominateur) + un opérateur de
    conformité (GE = le ratio DOIT être >= seuil ; LE = DOIT être <= seuil).
  - ratio_seuil : le ou les seuils d'un ratio — `categorie_sfd NULL` = seuil unique pour toute
    institution, sinon un seuil PROPRE à chaque catégorie réglementaire (un même ratio peut
    avoir un seuil différent pour un SFD affilié à un réseau, par exemple).
  - parametre_institution : SINGLETON — un déploiement = une institution (mono-tenant confirmé
    par le diagnostic préalable, voir app/core/config.py). Porte la catégorie réglementaire de
    CETTE institution (déterminant quel ratio_seuil s'applique) et un complément de provisions
    sous tutelle (ajustement administratif des fonds propres effectifs, montant en F CFA entier
    — 0 par défaut, jamais déduit de rien tant qu'il n'est pas renseigné).

ADOSSÉ AUX NUMÉROS DE COMPTES RCSFD, PAS au mapping bilan/compte de résultat du lot P1
(`comptabilite.financial_statement_mapping.poste_libelle` est du texte libre non validé en
base — trop fragile pour porter une définition réglementaire qui doit rester stable). Le
moteur (lot P2.1.b) réutilisera `rapports.balance()` pour les soldes et filtrera par préfixe ;
ce lot ne pose QUE les données, aucun calcul.

SINGLETON : même patron que `caisse.parametres` (migration 0043, CA2) — colonne `singleton`
forcée à `TRUE` par CHECK, UNIQUE sur cette colonne : au plus une ligne possible en base,
appliqué par la base elle-même, pas seulement par une discipline applicative.

Revision ID: 0057
Revises: 0056
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0057"
down_revision: str | None = "0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")
FK_USER = "security.users.id"


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS conformite")

    # --- agregat_prudentiel ---------------------------------------------------------------
    op.create_table(
        "agregat_prudentiel",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("code", sa.String(40), nullable=False),
        sa.Column("libelle", sa.String(200), nullable=False),
        sa.Column("reference", sa.String(200), nullable=True),
        sa.Column("type", sa.String(10), nullable=False),
        sa.Column("calcul_special", sa.String(50), nullable=True),
        sa.Column("nets_de_provisions", sa.Boolean(), server_default=sa.false(), nullable=False),
        # Ajusté au seed (lot P2.1.b/c, pas dans le schéma initial) : seul FONDS_PROPRES le
        # porte à TRUE aujourd'hui, mais c'est un booléen générique — n'importe quel agrégat
        # BALANCE pourrait l'activer, aucun code câblé sur le code 'FONDS_PROPRES'.
        sa.Column(
            "applique_complement_provisions_tutelle",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("is_system", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
        sa.ForeignKeyConstraint(["created_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["updated_by"], [FK_USER]),
        sa.CheckConstraint("type IN ('BALANCE', 'SPECIAL')", name="ck_agregat_type"),
        sa.CheckConstraint(
            "(type = 'SPECIAL' AND calcul_special IS NOT NULL) OR "
            "(type = 'BALANCE' AND calcul_special IS NULL)",
            name="ck_agregat_calcul_special_coherent",
        ),
        schema="conformite",
    )

    # --- agregat_compte --------------------------------------------------------------------
    op.create_table(
        "agregat_compte",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("agregat_id", UUID, nullable=False),
        sa.Column("prefixe_compte", sa.String(20), nullable=False),
        sa.Column("sens", sa.SmallInteger(), nullable=False),
        sa.Column("is_system", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agregat_id", "prefixe_compte"),
        sa.ForeignKeyConstraint(
            ["agregat_id"], ["conformite.agregat_prudentiel.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["created_by"], [FK_USER]),
        sa.CheckConstraint("sens IN (1, -1)", name="ck_agregat_compte_sens"),
        schema="conformite",
    )
    op.create_index(
        "ix_conformite_agregat_compte_agregat",
        "agregat_compte",
        ["agregat_id"],
        schema="conformite",
    )

    # --- ratio_prudentiel ------------------------------------------------------------------
    op.create_table(
        "ratio_prudentiel",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("code", sa.String(40), nullable=False),
        sa.Column("libelle", sa.String(200), nullable=False),
        sa.Column("reference_reglementaire", sa.String(200), nullable=True),
        sa.Column("agregat_numerateur_id", UUID, nullable=False),
        sa.Column("agregat_denominateur_id", UUID, nullable=False),
        sa.Column("operateur", sa.String(2), nullable=False),
        sa.Column("actif", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("ordre", sa.Integer(), nullable=False),
        sa.Column("is_system", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
        sa.UniqueConstraint("ordre"),
        sa.ForeignKeyConstraint(
            ["agregat_numerateur_id"], ["conformite.agregat_prudentiel.id"]
        ),
        sa.ForeignKeyConstraint(
            ["agregat_denominateur_id"], ["conformite.agregat_prudentiel.id"]
        ),
        sa.ForeignKeyConstraint(["created_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["updated_by"], [FK_USER]),
        sa.CheckConstraint("operateur IN ('GE', 'LE')", name="ck_ratio_operateur"),
        schema="conformite",
    )

    # --- ratio_seuil -------------------------------------------------------------------------
    op.create_table(
        "ratio_seuil",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("ratio_id", UUID, nullable=False),
        sa.Column("categorie_sfd", sa.String(20), nullable=True),
        sa.Column("valeur_seuil", sa.Numeric(7, 4), nullable=False),
        sa.Column("is_system", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["ratio_id"], ["conformite.ratio_prudentiel.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["created_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["updated_by"], [FK_USER]),
        sa.CheckConstraint(
            "categorie_sfd IS NULL OR categorie_sfd IN ('SANS_DEPOTS', 'AFFILIE', 'NON_AFFILIE')",
            name="ck_ratio_seuil_categorie",
        ),
        schema="conformite",
    )
    # NULL n'est jamais égal à NULL : un index unique ordinaire laisserait passer plusieurs
    # lignes "categorie_sfd IS NULL" pour le même ratio. COALESCE force une valeur comparable
    # ('*' = seuil unique tous SFD) pour que l'unicité tienne aussi dans ce cas.
    op.create_index(
        "uq_conformite_ratio_seuil_categorie",
        "ratio_seuil",
        ["ratio_id", sa.text("COALESCE(categorie_sfd, '*')")],
        unique=True,
        schema="conformite",
    )

    # --- parametre_institution (SINGLETON) --------------------------------------------------
    op.create_table(
        "parametre_institution",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("categorie_sfd", sa.String(20), nullable=False),
        sa.Column(
            "complement_provisions_tutelle",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("singleton", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_by", UUID, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["created_by"], [FK_USER]),
        sa.ForeignKeyConstraint(["updated_by"], [FK_USER]),
        sa.CheckConstraint(
            "categorie_sfd IN ('SANS_DEPOTS', 'AFFILIE', 'NON_AFFILIE')",
            name="ck_parametre_institution_categorie",
        ),
        sa.CheckConstraint("singleton", name="ck_parametre_institution_singleton"),
        sa.UniqueConstraint("singleton", name="uq_parametre_institution_singleton"),
        schema="conformite",
    )


def downgrade() -> None:
    op.drop_table("parametre_institution", schema="conformite")
    op.drop_index(
        "uq_conformite_ratio_seuil_categorie", table_name="ratio_seuil", schema="conformite"
    )
    op.drop_table("ratio_seuil", schema="conformite")
    op.drop_table("ratio_prudentiel", schema="conformite")
    op.drop_index(
        "ix_conformite_agregat_compte_agregat", table_name="agregat_compte", schema="conformite"
    )
    op.drop_table("agregat_compte", schema="conformite")
    op.drop_table("agregat_prudentiel", schema="conformite")
    op.execute("DROP SCHEMA IF EXISTS conformite")
