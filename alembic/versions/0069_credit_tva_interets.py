"""Crédit — TVA sur les intérêts : paramètre produit, TVA par échéance, TVA par remboursement,
compte d'État « TVA collectée à reverser ».

  - `credit.products.taux_tva_bp` (points de base, 0..10000) : les produits EXISTANTS reçoivent 0
    (exonérés) — rien ne change pour eux. Le défaut de 19 % n'est appliqué que par l'API, à la
    création d'un NOUVEAU produit.
  - `credit.products.compte_tva_id` (FK comptes) : rattachement du compte de TVA collectée.
  - `credit.installments.tva` : TVA de l'échéance ; `total` devient le total TTC
    (capital + intérêts + TVA). Les échéances existantes valent 0 : leur total est inchangé.
  - `credit.repayments.montant_tva` : TVA encaissée par ce versement ; la contrainte de cohérence
    devient montant_total = capital + intérêts + TVA.
  - compte 332100 « État, TVA collectée à reverser » (classe 3, crédit, imputable), sous 332, avec
    la même ligne de mapping d'états financiers que 332 (passif, « Créditeurs divers »). Sur une
    base neuve (plan pas encore importé) le compte est créé par l'import du plan : cette
    migration n'y fait rien. Idempotente.

Aucune donnée existante n'est modifiée ; aucun reversement n'est géré ici.

Revision ID: 0069
Revises: 0068
Create Date: 2026-10-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0069"
down_revision: str | None = "0068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NUMERO_COMPTE_TVA = "332100"
NOM_COMPTE_TVA = "État, TVA collectée à reverser"
NUMERO_PARENT = "332"


def upgrade() -> None:
    op.execute(
        f"""
        INSERT INTO comptabilite.accounts
            (account_number, name, account_class, parent_id, normal_side, is_posting, is_system)
        SELECT '{NUMERO_COMPTE_TVA}', '{NOM_COMPTE_TVA}', p.account_class, p.id, 'C', TRUE, TRUE
        FROM comptabilite.accounts p
        WHERE p.account_number = '{NUMERO_PARENT}'
        ON CONFLICT (account_number) DO NOTHING
        """
    )
    op.execute(
        f"""
        INSERT INTO comptabilite.financial_statement_mapping
            (account_id, etat, masse, poste_libelle, poste_ordre)
        SELECT n.id, m.etat, m.masse, m.poste_libelle, m.poste_ordre
        FROM comptabilite.accounts n
        JOIN comptabilite.accounts p ON p.account_number = '{NUMERO_PARENT}'
        JOIN comptabilite.financial_statement_mapping m ON m.account_id = p.id
        WHERE n.account_number = '{NUMERO_COMPTE_TVA}'
        ON CONFLICT (account_id) DO NOTHING
        """
    )

    op.execute(
        "ALTER TABLE credit.products "
        "ADD COLUMN IF NOT EXISTS taux_tva_bp INTEGER NOT NULL DEFAULT 0"
    )
    op.execute("ALTER TABLE credit.products ADD COLUMN IF NOT EXISTS compte_tva_id UUID")
    op.execute(
        "ALTER TABLE credit.products DROP CONSTRAINT IF EXISTS ck_products_taux_tva_bp"
    )
    op.execute(
        "ALTER TABLE credit.products ADD CONSTRAINT ck_products_taux_tva_bp "
        "CHECK (taux_tva_bp >= 0 AND taux_tva_bp <= 10000)"
    )
    op.execute(
        "ALTER TABLE credit.products DROP CONSTRAINT IF EXISTS fk_credit_products_compte_tva"
    )
    op.execute(
        "ALTER TABLE credit.products ADD CONSTRAINT fk_credit_products_compte_tva "
        "FOREIGN KEY (compte_tva_id) REFERENCES comptabilite.accounts (id)"
    )

    op.execute(
        "ALTER TABLE credit.installments ADD COLUMN IF NOT EXISTS tva BIGINT NOT NULL DEFAULT 0"
    )
    op.execute("ALTER TABLE credit.installments DROP CONSTRAINT IF EXISTS ck_installments_tva_non_negatif")
    op.execute(
        "ALTER TABLE credit.installments ADD CONSTRAINT ck_installments_tva_non_negatif "
        "CHECK (tva >= 0)"
    )

    op.execute(
        "ALTER TABLE credit.repayments "
        "ADD COLUMN IF NOT EXISTS montant_tva BIGINT NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE credit.repayments DROP CONSTRAINT IF EXISTS ck_repayments_montant_tva_non_negatif"
    )
    op.execute(
        "ALTER TABLE credit.repayments ADD CONSTRAINT ck_repayments_montant_tva_non_negatif "
        "CHECK (montant_tva >= 0)"
    )
    op.execute(
        "ALTER TABLE credit.repayments DROP CONSTRAINT IF EXISTS ck_repayments_montant_total_coherent"
    )
    op.execute(
        "ALTER TABLE credit.repayments ADD CONSTRAINT ck_repayments_montant_total_coherent "
        "CHECK (montant_total = montant_capital + montant_interets + montant_tva)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE credit.repayments DROP CONSTRAINT IF EXISTS ck_repayments_montant_total_coherent"
    )
    op.execute(
        "ALTER TABLE credit.repayments ADD CONSTRAINT ck_repayments_montant_total_coherent "
        "CHECK (montant_total = montant_capital + montant_interets)"
    )
    op.execute(
        "ALTER TABLE credit.repayments DROP CONSTRAINT IF EXISTS ck_repayments_montant_tva_non_negatif"
    )
    op.execute("ALTER TABLE credit.repayments DROP COLUMN IF EXISTS montant_tva")
    op.execute("ALTER TABLE credit.installments DROP CONSTRAINT IF EXISTS ck_installments_tva_non_negatif")
    op.execute("ALTER TABLE credit.installments DROP COLUMN IF EXISTS tva")
    op.execute("ALTER TABLE credit.products DROP CONSTRAINT IF EXISTS fk_credit_products_compte_tva")
    op.execute("ALTER TABLE credit.products DROP CONSTRAINT IF EXISTS ck_products_taux_tva_bp")
    op.execute("ALTER TABLE credit.products DROP COLUMN IF EXISTS compte_tva_id")
    op.execute("ALTER TABLE credit.products DROP COLUMN IF EXISTS taux_tva_bp")
    # Le compte 332100 et sa ligne de mapping ne sont pas supprimés : des écritures peuvent déjà
    # le référencer (jamais de suppression physique d'une donnée métier).
