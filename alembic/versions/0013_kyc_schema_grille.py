"""KYC (T3a) — données KYC, état de risque, et grille de scoring PARAMÉTRABLE.

Trois volets :

1. DONNÉES KYC sur individual_profiles : origine des fonds, secteur d'activité, statut PPE
   (+ relation + fonction), mode d'entrée en relation. La profession et les revenus existent déjà.

2. RISQUE = LE FILM, PAS LA PHOTO. L'historique tiers.risk_assessments (append-only) archive
   CHAQUE évaluation avec son détail complet — c'est la vérité de conformité. La fiche tiers ne
   garde qu'un REFLET du dernier calcul (risk_level / risk_score / risk_computed_at /
   risk_grid_version) pour l'affichage et le filtrage. On doit pouvoir prouver « au 25/07, cette
   fiche était à 45 points / risque moyen », et le snapshot le permet même après refonte de grille.

3. GRILLE DE RISQUE PARAMÉTRABLE (schéma parameters), à TROIS types de règles — un score seul
   raterait l'obligation réglementaire :
     - contributive : ajoute des points (somme -> barème -> niveau de base) ;
     - plancher     : impose un niveau MINIMUM quel que soit le score (PPE -> renforcée) ;
     - couperet     : bloque l'activation (sanctions -> refus), pas un niveau élevé.

VALEURS PROVISOIRES. Tout le seed est marqué provisoire : la grille porte is_provisional=TRUE, et
CHAQUE valeur (points, bornes, planchers, secteurs à risque) est « À VALIDER » par le Responsable
LBC/FT (voir docs/conformite-lbcft.md). Le paramétrage est en base : un durcissement réglementaire
se règle sans redéploiement. Aucune valeur n'est présentée comme définitive.

DOWNGRADE : retire les colonnes puis DROP les tables (ordre inverse des dépendances).

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("NOW()")
GEN_UUID = sa.text("gen_random_uuid()")

# Grille v1, id fixe (seed déterministe) : les règles et bornes y sont rattachées.
GRID_V1 = "a0000000-0000-4000-8000-000000000001"

_NIVEAUX = "('faible', 'moyen', 'eleve')"


def _colonnes_audit() -> tuple[sa.Column, ...]:
    return (
        sa.Column("created_at", TS, server_default=NOW, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", TS, server_default=NOW, nullable=False),
        sa.Column("updated_by", UUID, nullable=True),
    )


def _fk_audit(table: str) -> tuple[sa.ForeignKeyConstraint, ...]:
    return (
        sa.ForeignKeyConstraint(["created_by"], ["security.users.id"]),
        sa.ForeignKeyConstraint(["updated_by"], ["security.users.id"]),
    )


def upgrade() -> None:
    # --- référentiel secteurs d'activité -------------------------------------
    # is_a_risque alimente la règle contributive « activité à risque » (change, métaux, jeux…).
    # QUELS secteurs sont à risque = décision réglementaire -> valeurs provisoires « À VALIDER ».
    op.create_table(
        "secteurs_activite",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("code", sa.String(30), nullable=False),
        sa.Column("libelle", sa.String(200), nullable=False),
        sa.Column("is_a_risque", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("display_order", sa.Integer(), server_default=sa.text("100"), nullable=False),
        *_colonnes_audit(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
        *_fk_audit("secteurs_activite"),
        schema="parameters",
    )

    # --- grille de risque (conteneur versionné) ------------------------------
    # is_provisional : TOUTE la grille est provisoire tant que le LBC/FT ne l'a pas validée.
    # L'écran lira ce flag pour afficher partout « valeurs provisoires — à valider ».
    op.create_table(
        "kyc_risk_grid",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("libelle", sa.String(200), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_provisional", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        *_colonnes_audit(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version"),
        *_fk_audit("kyc_risk_grid"),
        schema="parameters",
    )

    # --- règles de la grille (les TROIS types) -------------------------------
    op.create_table(
        "kyc_risk_rules",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("grid_id", UUID, nullable=False),
        sa.Column("code", sa.String(40), nullable=False),
        sa.Column("libelle", sa.String(200), nullable=False),
        sa.Column("rule_type", sa.String(15), nullable=False),
        sa.Column("critere", sa.String(40), nullable=False),  # ce que le moteur évalue
        sa.Column("points", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("niveau_impose", sa.String(10), nullable=True),  # plancher
        sa.Column("bloquant", sa.Boolean(), server_default=sa.false(), nullable=False),  # couperet
        sa.Column("actif", sa.Boolean(), server_default=sa.true(), nullable=False),
        *_colonnes_audit(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("grid_id", "code"),
        sa.ForeignKeyConstraint(["grid_id"], ["parameters.kyc_risk_grid.id"]),
        sa.CheckConstraint(
            "rule_type IN ('contributive', 'plancher', 'couperet')", name="rule_type"
        ),
        sa.CheckConstraint(f"niveau_impose IS NULL OR niveau_impose IN {_NIVEAUX}", name="niveau"),
        *_fk_audit("kyc_risk_rules"),
        schema="parameters",
    )

    # --- barème : score -> niveau (une borne basse par niveau) ----------------
    op.create_table(
        "kyc_risk_thresholds",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("grid_id", UUID, nullable=False),
        sa.Column("niveau", sa.String(10), nullable=False),
        sa.Column("score_min", sa.Integer(), nullable=False),
        *_colonnes_audit(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("grid_id", "niveau"),
        sa.ForeignKeyConstraint(["grid_id"], ["parameters.kyc_risk_grid.id"]),
        sa.CheckConstraint(f"niveau IN {_NIVEAUX}", name="niveau"),
        *_fk_audit("kyc_risk_thresholds"),
        schema="parameters",
    )

    # --- colonnes KYC sur individual_profiles --------------------------------
    op.add_column(
        "individual_profiles",
        sa.Column("origine_fonds", sa.Text(), nullable=True),
        schema="tiers",
    )
    op.add_column(
        "individual_profiles",
        sa.Column("secteur_activite_id", UUID, nullable=True),
        schema="tiers",
    )
    op.add_column(
        "individual_profiles",
        sa.Column("ppe_status", sa.Boolean(), server_default=sa.false(), nullable=False),
        schema="tiers",
    )
    op.add_column(
        "individual_profiles",
        sa.Column("ppe_relation", sa.String(10), nullable=True),  # direct | entourage
        schema="tiers",
    )
    op.add_column(
        "individual_profiles",
        sa.Column("ppe_fonction", sa.String(200), nullable=True),
        schema="tiers",
    )
    op.add_column(
        "individual_profiles",
        sa.Column("mode_entree_relation", sa.String(20), nullable=True),
        schema="tiers",
    )
    op.create_foreign_key(
        "fk_individual_profiles_secteur_activite_id_secteurs_activite",
        "individual_profiles",
        "secteurs_activite",
        ["secteur_activite_id"],
        ["id"],
        source_schema="tiers",
        referent_schema="parameters",
    )
    op.create_check_constraint(
        "ck_individual_profiles_ppe_relation",
        "individual_profiles",
        "ppe_relation IS NULL OR ppe_relation IN ('direct', 'entourage')",
        schema="tiers",
    )
    op.create_check_constraint(
        "ck_individual_profiles_mode_entree",
        "individual_profiles",
        "mode_entree_relation IS NULL OR mode_entree_relation IN "
        "('presentiel', 'tiers_confiance', 'distance')",
        schema="tiers",
    )

    # --- reflet du risque COURANT sur tiers (cache, pas la vérité) -----------
    # Ces colonnes sont le REFLET du dernier calcul archivé (risk_assessments), pour l'affichage
    # et le filtrage. La vérité — le FILM — est dans risk_assessments, jamais écrasé.
    op.add_column("tiers", sa.Column("risk_level", sa.String(10), nullable=True), schema="tiers")
    op.add_column("tiers", sa.Column("risk_score", sa.Integer(), nullable=True), schema="tiers")
    op.add_column("tiers", sa.Column("risk_computed_at", TS, nullable=True), schema="tiers")
    op.add_column("tiers", sa.Column("risk_grid_version", sa.Integer(), nullable=True), schema="tiers")
    op.create_check_constraint(
        "ck_tiers_risk_level",
        "tiers",
        f"risk_level IS NULL OR risk_level IN {_NIVEAUX}",
        schema="tiers",
    )

    # --- historique des évaluations de risque (append-only, conformité) ------
    # Le FILM, pas la photo. Chaque calcul est archivé avec son DÉTAIL COMPLET dans `detail`
    # (contributions, planchers, couperets, barème utilisé, version de grille) : reproductible
    # SEUL, sans la grille vivante — défendable devant un inspecteur même après modification de la
    # grille. Table append-only : le service n'y fait que des INSERT, jamais d'UPDATE/DELETE.
    op.create_table(
        "risk_assessments",
        sa.Column("id", UUID, server_default=GEN_UUID, nullable=False),
        sa.Column("tier_id", UUID, nullable=False),
        sa.Column("assessed_at", TS, server_default=NOW, nullable=False),
        sa.Column("assessed_by", UUID, nullable=True),  # null = calcul système (ex. à la création)
        sa.Column("trigger_event", sa.String(30), nullable=False),  # creation | maj_kyc | ...
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("niveau_bareme", sa.String(10), nullable=False),
        sa.Column("niveau_effectif", sa.String(10), nullable=False),
        sa.Column("grid_id", UUID, nullable=False),
        sa.Column("grid_version", sa.Integer(), nullable=False),
        sa.Column("is_provisional", sa.Boolean(), nullable=False),  # grille provisoire à l'instant T
        sa.Column("couperet_declenche", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=False),  # snapshot COMPLET du calcul
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tier_id"], ["tiers.tiers.id"]),
        sa.ForeignKeyConstraint(["grid_id"], ["parameters.kyc_risk_grid.id"]),
        sa.ForeignKeyConstraint(["assessed_by"], ["security.users.id"]),
        sa.CheckConstraint(f"niveau_bareme IN {_NIVEAUX}", name="niveau_bareme"),
        sa.CheckConstraint(f"niveau_effectif IN {_NIVEAUX}", name="niveau_effectif"),
        schema="tiers",
    )
    op.create_index(
        "ix_risk_assessments_tier_id_assessed_at",
        "risk_assessments",
        ["tier_id", "assessed_at"],
        schema="tiers",
    )

    _seed(op.get_bind())


def _seed(conn: sa.engine.Connection) -> None:
    secteurs = sa.table(
        "secteurs_activite",
        sa.column("code", sa.String),
        sa.column("libelle", sa.String),
        sa.column("is_a_risque", sa.Boolean),
        sa.column("display_order", sa.Integer),
        schema="parameters",
    )
    # is_a_risque PROVISOIRE : liste indicative (activités souvent flaguées LBC/FT), à valider.
    op.bulk_insert(
        secteurs,
        [
            {"code": "AGRICULTURE", "libelle": "Agriculture, élevage, pêche", "is_a_risque": False, "display_order": 10},
            {"code": "COMMERCE", "libelle": "Commerce de détail", "is_a_risque": False, "display_order": 20},
            {"code": "ARTISANAT", "libelle": "Artisanat", "is_a_risque": False, "display_order": 30},
            {"code": "SERVICES", "libelle": "Services", "is_a_risque": False, "display_order": 40},
            {"code": "TRANSPORT", "libelle": "Transport", "is_a_risque": False, "display_order": 50},
            {"code": "BTP", "libelle": "Bâtiment et travaux publics", "is_a_risque": False, "display_order": 60},
            {"code": "CHANGE", "libelle": "Change manuel de devises", "is_a_risque": True, "display_order": 70},
            {"code": "METAUX_PRECIEUX", "libelle": "Métaux et pierres précieuses", "is_a_risque": True, "display_order": 80},
            {"code": "JEUX", "libelle": "Jeux, casinos, paris", "is_a_risque": True, "display_order": 90},
            {"code": "IMMOBILIER", "libelle": "Immobilier", "is_a_risque": True, "display_order": 100},
            {"code": "AUTRE", "libelle": "Autre", "is_a_risque": False, "display_order": 900},
        ],
    )

    conn.execute(
        sa.text(
            "INSERT INTO parameters.kyc_risk_grid (id, version, libelle, is_active, is_provisional, notes) "
            "VALUES (:id, 1, :lib, TRUE, TRUE, :notes)"
        ),
        {
            "id": GRID_V1,
            "lib": "Grille de risque LBC/FT — PROVISOIRE (À VALIDER)",
            "notes": "Valeurs par défaut provisoires. À valider par le Responsable LBC/FT "
            "(voir docs/conformite-lbcft.md). Ne pas présenter comme réglementaires.",
        },
    )

    rules = sa.table(
        "kyc_risk_rules",
        sa.column("grid_id", UUID),
        sa.column("code", sa.String),
        sa.column("libelle", sa.String),
        sa.column("rule_type", sa.String),
        sa.column("critere", sa.String),
        sa.column("points", sa.Integer),
        sa.column("niveau_impose", sa.String),
        sa.column("bloquant", sa.Boolean),
        schema="parameters",
    )
    # PROVISOIRE — points, niveaux et blocages À VALIDER (docs/conformite-lbcft.md).
    op.bulk_insert(
        rules,
        [
            {"grid_id": GRID_V1, "code": "PAYS_GAFI", "libelle": "Pays à risque GAFI (gris/noir)",
             "rule_type": "contributive", "critere": "pays_gafi", "points": 40, "niveau_impose": None, "bloquant": False},
            {"grid_id": GRID_V1, "code": "SECTEUR_RISQUE", "libelle": "Secteur d'activité à risque",
             "rule_type": "contributive", "critere": "secteur_risque", "points": 30, "niveau_impose": None, "bloquant": False},
            {"grid_id": GRID_V1, "code": "VOLUME_ELEVE", "libelle": "Volume d'activité élevé",
             "rule_type": "contributive", "critere": "volume_eleve", "points": 20, "niveau_impose": None, "bloquant": False},
            {"grid_id": GRID_V1, "code": "MODE_DISTANCE", "libelle": "Entrée en relation à distance",
             "rule_type": "contributive", "critere": "mode_distance", "points": 25, "niveau_impose": None, "bloquant": False},
            {"grid_id": GRID_V1, "code": "MODE_TIERS", "libelle": "Entrée par tiers de confiance",
             "rule_type": "contributive", "critere": "mode_tiers", "points": 10, "niveau_impose": None, "bloquant": False},
            {"grid_id": GRID_V1, "code": "PPE", "libelle": "Personne politiquement exposée (soi ou entourage)",
             "rule_type": "plancher", "critere": "ppe", "points": 0, "niveau_impose": "eleve", "bloquant": False},
            {"grid_id": GRID_V1, "code": "SANCTIONS", "libelle": "Correspondance liste de sanctions",
             "rule_type": "couperet", "critere": "sanctions", "points": 0, "niveau_impose": None, "bloquant": True},
        ],
    )

    thresholds = sa.table(
        "kyc_risk_thresholds",
        sa.column("grid_id", UUID),
        sa.column("niveau", sa.String),
        sa.column("score_min", sa.Integer),
        schema="parameters",
    )
    # Barème PROVISOIRE : < 30 faible, 30–59 moyen, >= 60 élevé. À VALIDER.
    op.bulk_insert(
        thresholds,
        [
            {"grid_id": GRID_V1, "niveau": "faible", "score_min": 0},
            {"grid_id": GRID_V1, "niveau": "moyen", "score_min": 30},
            {"grid_id": GRID_V1, "niveau": "eleve", "score_min": 60},
        ],
    )


def downgrade() -> None:
    op.drop_table("risk_assessments", schema="tiers")  # avant kyc_risk_grid (FK)

    op.drop_constraint("ck_tiers_risk_level", "tiers", schema="tiers", type_="check")
    for col in ("risk_grid_version", "risk_computed_at", "risk_score", "risk_level"):
        op.drop_column("tiers", col, schema="tiers")

    op.drop_constraint(
        "ck_individual_profiles_mode_entree", "individual_profiles", schema="tiers", type_="check"
    )
    op.drop_constraint(
        "ck_individual_profiles_ppe_relation", "individual_profiles", schema="tiers", type_="check"
    )
    op.drop_constraint(
        "fk_individual_profiles_secteur_activite_id_secteurs_activite",
        "individual_profiles",
        schema="tiers",
        type_="foreignkey",
    )
    for col in (
        "mode_entree_relation",
        "ppe_fonction",
        "ppe_relation",
        "ppe_status",
        "secteur_activite_id",
        "origine_fonds",
    ):
        op.drop_column("individual_profiles", col, schema="tiers")

    op.drop_table("kyc_risk_thresholds", schema="parameters")
    op.drop_table("kyc_risk_rules", schema="parameters")
    op.drop_table("kyc_risk_grid", schema="parameters")
    op.drop_table("secteurs_activite", schema="parameters")
