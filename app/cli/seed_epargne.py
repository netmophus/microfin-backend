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
    compte: str  # compte de dette MEMBRE du plan (xxx1), rattachement PROVISOIRE
    compte_client: str  # compte de dette CLIENT (xxx2, PS3), rattachement PROVISOIRE
    compte_charge: str  # compte de charge d'intérêts (603/604), rattachement PROVISOIRE


# Produits minimaux. PROVISOIRES. Le plan dédouble chaque nature : compte MEMBRE (xxx1) et compte
# CLIENT (xxx2) ; le routage est ancré à l'ouverture selon le statut du tiers (PS3).
# Le taux d'intérêt reste 0 (défaut) tant que l'expert ne l'a pas fixé.
PRODUITS: tuple[ProduitStandard, ...] = (
    ProduitStandard("EAV", "Épargne à vue", "a_vue", 0, "3111", "3112", "603"),
    ProduitStandard("DAT", "Dépôt à terme", "terme", 0, "3121", "3122", "604"),
    ProduitStandard("EPR", "Épargne programmée", "programmee", 0, "3131", "3132", "604"),
)


# Rattache le produit aux comptes du plan par leur NUMÉRO (sous-requêtes) : si un compte n'existe
# pas (plan non importé), le rattachement reste NULL, provisoire, sans faire échouer le seed.
_UPSERT = text(
    """
    INSERT INTO epargne.products
        (code, name, type, min_balance, is_provisional, compte_epargne_id,
         compte_epargne_client_id, compte_charge_interet_id)
    VALUES (
        :code, :name, :type, :min_balance, TRUE,
        (SELECT id FROM comptabilite.accounts WHERE account_number = :compte),
        (SELECT id FROM comptabilite.accounts WHERE account_number = :compte_client),
        (SELECT id FROM comptabilite.accounts WHERE account_number = :compte_charge)
    )
    ON CONFLICT (code) DO UPDATE SET
        name                     = EXCLUDED.name,
        type                     = EXCLUDED.type,
        min_balance              = EXCLUDED.min_balance,
        compte_epargne_id        = EXCLUDED.compte_epargne_id,
        compte_epargne_client_id = EXCLUDED.compte_epargne_client_id,
        compte_charge_interet_id = EXCLUDED.compte_charge_interet_id,
        updated_at               = NOW()
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
                "compte_client": produit.compte_client,
                "compte_charge": produit.compte_charge,
            },
        )
    return len(PRODUITS)
