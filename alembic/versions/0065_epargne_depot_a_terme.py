"""Épargne — le dépôt à terme (DAT) : durée, échéance, conditions figées, blocage en base.

Produit : bornes de durée (`duree_min_mois`/`duree_max_mois`) et pénalité de retrait anticipé
(`penalite_anticipee_bp`, en points de base des intérêts COURUS : 10 000 = tous les intérêts, le
capital n'est jamais entamé).

Compte : la durée choisie à l'ouverture, l'échéance (dérivée de la JOURNÉE comptable, jamais de la
date du poste), le capital placé et les QUATRE paramètres de calcul figés au contrat (taux,
pénalité, base de jours, règle d'arrondi) : un changement du produit ne modifie jamais un DAT en
cours, le calcul reste rejouable.

Dernier rempart en base (le service est la première garde) :
  - un compte d'un produit « terme » porte ses conditions ; les conditions sont immuables ;
  - le type d'un produit ne change plus dès qu'il porte des comptes ;
  - sur un compte à terme, les seuls mouvements admis sont : le dépôt du capital (premier
    mouvement), UN crédit d'intérêts, le dénouement (à partir de l'échéance) et le retrait
    anticipé (avant l'échéance), tous deux pour la totalité du solde. La date comparée est la date
    de valeur du mouvement, c'est-à-dire celle de la pièce : la journée comptable.

La migration s'arrête, avant toute modification, s'il existe déjà des comptes d'un produit
« terme » avec solde ou mouvements : on ne devine pas leur échéance.

Revision ID: 0065
Revises: 0064
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0065"
down_revision: str | None = "0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHAMPS_TERME = (
    "term_months",
    "term_start_date",
    "maturity_date",
    "principal",
    "taux_bp_contrat",
    "penalite_bp_contrat",
    "base_jours_contrat",
    "regle_arrondi_contrat",
)

FONCTIONS_ET_TRIGGERS = """
CREATE FUNCTION epargne.compte_terme_conditions() RETURNS trigger AS $$
DECLARE type_produit text;
BEGIN
  IF TG_OP = 'INSERT' THEN
    SELECT type INTO type_produit FROM epargne.products WHERE id = NEW.product_id;
    IF (type_produit = 'terme') IS DISTINCT FROM (NEW.term_months IS NOT NULL) THEN
      RAISE EXCEPTION
        'Compte a terme : les conditions du depot (duree, echeance, capital) sont '
        'obligatoires sur un produit a terme et interdites sinon'
        USING ERRCODE = 'check_violation';
    END IF;
  ELSIF (NEW.term_months, NEW.term_start_date, NEW.maturity_date, NEW.principal,
         NEW.taux_bp_contrat, NEW.penalite_bp_contrat, NEW.base_jours_contrat,
         NEW.regle_arrondi_contrat)
        IS DISTINCT FROM
        (OLD.term_months, OLD.term_start_date, OLD.maturity_date, OLD.principal,
         OLD.taux_bp_contrat, OLD.penalite_bp_contrat, OLD.base_jours_contrat,
         OLD.regle_arrondi_contrat) THEN
    RAISE EXCEPTION 'Compte a terme %: les conditions du depot sont immuables',
      OLD.account_number USING ERRCODE = 'restrict_violation';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_compte_terme_conditions
  BEFORE INSERT OR UPDATE ON epargne.accounts
  FOR EACH ROW EXECUTE FUNCTION epargne.compte_terme_conditions();

CREATE FUNCTION epargne.produit_type_fige() RETURNS trigger AS $$
BEGIN
  IF NEW.type IS DISTINCT FROM OLD.type
     AND EXISTS (SELECT 1 FROM epargne.accounts WHERE product_id = OLD.id) THEN
    RAISE EXCEPTION 'Produit %: le type ne change plus des qu''il porte des comptes',
      OLD.code USING ERRCODE = 'restrict_violation';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_produit_type_fige
  BEFORE UPDATE ON epargne.products
  FOR EACH ROW EXECUTE FUNCTION epargne.produit_type_fige();

CREATE FUNCTION epargne.mouvement_compte_terme() RETURNS trigger AS $$
DECLARE c record;
BEGIN
  SELECT a.term_months, a.maturity_date, p.type AS type_produit
    INTO c
    FROM epargne.accounts a JOIN epargne.products p ON p.id = a.product_id
   WHERE a.id = NEW.account_id;
  IF c.type_produit IS DISTINCT FROM 'terme' THEN
    RETURN NEW;
  END IF;
  IF c.term_months IS NULL THEN
    RAISE EXCEPTION 'Compte a terme sans conditions : aucun mouvement possible'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.sens = 'credit' AND NEW.operation_type = 'depot' THEN
    IF EXISTS (SELECT 1 FROM epargne.movements WHERE account_id = NEW.account_id) THEN
      RAISE EXCEPTION 'Depot a terme : le capital se place une seule fois'
        USING ERRCODE = 'check_violation';
    END IF;
  ELSIF NEW.sens = 'credit' AND NEW.operation_type = 'interet' THEN
    IF EXISTS (SELECT 1 FROM epargne.movements
                WHERE account_id = NEW.account_id AND operation_type = 'interet') THEN
      RAISE EXCEPTION 'Depot a terme : un seul versement d''interets, au denouement'
        USING ERRCODE = 'check_violation';
    END IF;
  ELSIF NEW.sens = 'debit' AND NEW.operation_type = 'denouement' THEN
    IF NEW.date_valeur < c.maturity_date OR NEW.balance_after <> 0 THEN
      RAISE EXCEPTION
        'Depot a terme : denouement impossible avant l''echeance (%) ou partiel',
        c.maturity_date USING ERRCODE = 'check_violation';
    END IF;
  ELSIF NEW.sens = 'debit' AND NEW.operation_type = 'retrait_anticipe' THEN
    IF NEW.date_valeur >= c.maturity_date OR NEW.balance_after <> 0 THEN
      RAISE EXCEPTION
        'Depot a terme : retrait anticipe impossible a partir de l''echeance (%) ou partiel',
        c.maturity_date USING ERRCODE = 'check_violation';
    END IF;
  ELSE
    RAISE EXCEPTION 'Depot a terme : mouvement % refuse (bloque jusqu''a l''echeance)',
      NEW.operation_type USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_mouvement_compte_terme
  BEFORE INSERT ON epargne.movements
  FOR EACH ROW EXECUTE FUNCTION epargne.mouvement_compte_terme();
"""


def upgrade() -> None:
    en_cause = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM epargne.accounts a "
                "JOIN epargne.products p ON p.id = a.product_id "
                "WHERE p.type = 'terme' AND (a.balance <> 0 "
                "OR EXISTS (SELECT 1 FROM epargne.movements m WHERE m.account_id = a.id))"
            )
        )
        .scalar_one()
    )
    if en_cause:
        raise RuntimeError(
            f"{en_cause} compte(s) à terme avec solde ou mouvements : à examiner avant "
            "d'introduire le dépôt à terme (la migration ne devine aucune échéance)."
        )

    op.add_column("products", sa.Column("duree_min_mois", sa.Integer()), schema="epargne")
    op.add_column("products", sa.Column("duree_max_mois", sa.Integer()), schema="epargne")
    op.add_column(
        "products",
        sa.Column(
            "penalite_anticipee_bp", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        schema="epargne",
    )
    op.create_check_constraint(
        "ck_products_duree_terme",
        "products",
        "(duree_min_mois IS NULL) = (duree_max_mois IS NULL) "
        "AND (duree_min_mois IS NULL "
        "OR (duree_min_mois >= 1 AND duree_min_mois <= duree_max_mois))",
        schema="epargne",
    )
    op.create_check_constraint(
        "ck_products_penalite_anticipee",
        "products",
        "penalite_anticipee_bp BETWEEN 0 AND 10000",
        schema="epargne",
    )

    op.add_column("accounts", sa.Column("term_months", sa.Integer()), schema="epargne")
    op.add_column("accounts", sa.Column("term_start_date", sa.Date()), schema="epargne")
    op.add_column("accounts", sa.Column("maturity_date", sa.Date()), schema="epargne")
    op.add_column("accounts", sa.Column("principal", sa.BigInteger()), schema="epargne")
    op.add_column("accounts", sa.Column("taux_bp_contrat", sa.Integer()), schema="epargne")
    op.add_column("accounts", sa.Column("penalite_bp_contrat", sa.Integer()), schema="epargne")
    op.add_column("accounts", sa.Column("base_jours_contrat", sa.Integer()), schema="epargne")
    op.add_column("accounts", sa.Column("regle_arrondi_contrat", sa.String(20)), schema="epargne")
    op.create_check_constraint(
        "ck_accounts_conditions_terme",
        "accounts",
        f"num_nonnulls({', '.join(CHAMPS_TERME)}) IN (0, {len(CHAMPS_TERME)})",
        schema="epargne",
    )
    op.create_check_constraint(
        "ck_accounts_terme_coherent",
        "accounts",
        "term_months IS NULL OR (term_months >= 1 AND maturity_date > term_start_date "
        "AND principal > 0 AND taux_bp_contrat >= 0 "
        "AND penalite_bp_contrat BETWEEN 0 AND 10000 AND base_jours_contrat > 0)",
        schema="epargne",
    )

    op.execute(FONCTIONS_ET_TRIGGERS)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_mouvement_compte_terme ON epargne.movements")
    op.execute("DROP FUNCTION IF EXISTS epargne.mouvement_compte_terme()")
    op.execute("DROP TRIGGER IF EXISTS trg_produit_type_fige ON epargne.products")
    op.execute("DROP FUNCTION IF EXISTS epargne.produit_type_fige()")
    op.execute("DROP TRIGGER IF EXISTS trg_compte_terme_conditions ON epargne.accounts")
    op.execute("DROP FUNCTION IF EXISTS epargne.compte_terme_conditions()")
    op.drop_constraint("ck_accounts_terme_coherent", "accounts", schema="epargne")
    op.drop_constraint("ck_accounts_conditions_terme", "accounts", schema="epargne")
    for colonne in reversed(CHAMPS_TERME):
        op.drop_column("accounts", colonne, schema="epargne")
    op.drop_constraint("ck_products_penalite_anticipee", "products", schema="epargne")
    op.drop_constraint("ck_products_duree_terme", "products", schema="epargne")
    op.drop_column("products", "penalite_anticipee_bp", schema="epargne")
    op.drop_column("products", "duree_max_mois", schema="epargne")
    op.drop_column("products", "duree_min_mois", schema="epargne")
