"""Seed des produits d'épargne — DONNÉES provisoires, comme le plan de comptes.

Livrés à l'installation, marqués provisoires : l'IMF les valide/complète (taux, règles) via la
gestion des produits. Idempotent (upsert par code).
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class ProduitStandard:
    code: str
    name: str
    type: str
    min_balance: int
    compte: str  # numéro du compte de dette du plan (rattachement PROVISOIRE)


# Produits minimaux. PROVISOIRES. Le rattachement comptable pointe vers le compte MEMBRE du plan
# (cas courant mutualiste) ; le cas client (3112) est une question ouverte (voir conformité).
PRODUITS: tuple[ProduitStandard, ...] = (
    ProduitStandard("EAV", "Épargne à vue", "a_vue", 0, "3111"),
    ProduitStandard("DAT", "Dépôt à terme", "terme", 0, "3121"),
    ProduitStandard("EPR", "Épargne programmée", "programmee", 0, "3131"),
)


# Rattache le produit au compte du plan par son NUMÉRO (sous-requête) : si le compte n'existe pas
# (plan non importé), compte_epargne_id reste NULL, provisoire, sans faire échouer le seed.
_UPSERT = text(
    """
    INSERT INTO epargne.products (code, name, type, min_balance, is_provisional, compte_epargne_id)
    VALUES (
        :code, :name, :type, :min_balance, TRUE,
        (SELECT id FROM comptabilite.accounts WHERE account_number = :compte)
    )
    ON CONFLICT (code) DO UPDATE SET
        name              = EXCLUDED.name,
        type              = EXCLUDED.type,
        min_balance       = EXCLUDED.min_balance,
        compte_epargne_id = EXCLUDED.compte_epargne_id,
        updated_at        = NOW()
    """
)


def executer_seed_produits(db: Session) -> int:
    """Installe/actualise les produits provisoires + rattachement comptable. Ne committe pas."""
    for produit in PRODUITS:
        db.execute(
            _UPSERT,
            {
                "code": produit.code,
                "name": produit.name,
                "type": produit.type,
                "min_balance": produit.min_balance,
                "compte": produit.compte,
            },
        )
    return len(PRODUITS)
