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


# Produits minimaux. PROVISOIRES. L'épargne à vue est le cas le plus courant ; DAT et programmée
# sont posés comme squelettes (règles à compléter par l'IMF).
PRODUITS: tuple[ProduitStandard, ...] = (
    ProduitStandard("EAV", "Épargne à vue", "a_vue", 0),
    ProduitStandard("DAT", "Dépôt à terme", "terme", 0),
    ProduitStandard("EPR", "Épargne programmée", "programmee", 0),
)


_UPSERT = text(
    """
    INSERT INTO epargne.products (code, name, type, min_balance, is_provisional)
    VALUES (:code, :name, :type, :min_balance, TRUE)
    ON CONFLICT (code) DO UPDATE SET
        name        = EXCLUDED.name,
        type        = EXCLUDED.type,
        min_balance = EXCLUDED.min_balance,
        updated_at  = NOW()
    """
)


def executer_seed_produits(db: Session) -> int:
    """Installe/actualise les produits d'épargne provisoires. Ne committe pas."""
    for produit in PRODUITS:
        db.execute(
            _UPSERT,
            {
                "code": produit.code,
                "name": produit.name,
                "type": produit.type,
                "min_balance": produit.min_balance,
            },
        )
    return len(PRODUITS)
