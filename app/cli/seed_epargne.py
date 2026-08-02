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


# Produits minimaux. PROVISOIRES. Comptes du plan RCSFD OFFICIEL (classe 2, "Comptes des
# membres, bénéficiaires ou clients") + extensions membre/client à 6 chiffres (structure
# proposée par le chantier paramétrage comptable, à valider par l'expert comme le reste) :
# 2511 Comptes ordinaires -> EAV ; 2521 Dépôts à terme reçus -> DAT ;
# 2531 Compte d'épargne sur livret (régime spécial) -> EPR. 2512 (Comptes ordinaires sur
# livret) reste en réserve, sans produit rattaché pour l'instant.
# Charge d'intérêts : feuilles déjà granulaires du plan officiel (602511/60252/60253), pas
# d'extension nécessaire là.
PRODUITS: tuple[ProduitStandard, ...] = (
    ProduitStandard("EAV", "Épargne à vue", "a_vue", 0, "251111", "251121", "602511"),
    ProduitStandard("DAT", "Dépôt à terme", "terme", 0, "252111", "252121", "60252"),
    ProduitStandard("EPR", "Épargne programmée", "programmee", 0, "253111", "253121", "60253"),
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
