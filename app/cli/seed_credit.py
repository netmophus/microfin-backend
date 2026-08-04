"""Seed du produit de crédit de démonstration — DONNÉE provisoire, comme le plan de comptes et
les produits d'épargne (voir seed_epargne.py).

Un SEUL produit, taux NON NUL À DESSEIN : contrairement aux produits d'épargne (taux 0 par
défaut, jamais démontrés dans leur seed), celui-ci sert à voir un échéancier RÉEL se calculer au
décaissement (CR3+) — demande explicite de l'utilisateur pour tester le parcours, PAS une
donnée réglementaire. `is_provisional = TRUE` comme tout le reste : à valider par l'expert-
comptable/crédit avant production, taux inclus.
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class ProduitCreditDemo:
    code: str
    name: str
    compte_membre: str  # extension à 6 chiffres, classe 20 (PROVISOIRE)
    compte_client: str  # extension à 6 chiffres, classe 20 (PROVISOIRE)
    compte_produits_interets: str  # 7021, officiel direct — pas d'extension (voir CR4)
    taux_bp: int  # DÉMONSTRATION, pas une valeur réglementaire
    periodicite: str
    methode_amortissement: str


# Court terme (2022), pour voir un échéancier avec un taux non nul.
PRODUITS: tuple[ProduitCreditDemo, ...] = (
    ProduitCreditDemo(
        "CCT", "Crédit court terme", "202211", "202221", "7021",
        1200, "mensuelle", "echeance_constante",
    ),
)


# Rattache le produit aux comptes du plan par leur NUMÉRO (sous-requêtes) : si un compte n'existe
# pas (plan non importé), le rattachement reste NULL, provisoire, sans faire échouer le seed.
_UPSERT = text(
    """
    INSERT INTO credit.products
        (code, name, is_provisional, compte_credit_membre_id, compte_credit_client_id,
         compte_produits_interets_id, taux_bp, periodicite, methode_amortissement)
    VALUES (
        :code, :name, TRUE,
        (SELECT id FROM comptabilite.accounts WHERE account_number = :compte_membre),
        (SELECT id FROM comptabilite.accounts WHERE account_number = :compte_client),
        (SELECT id FROM comptabilite.accounts WHERE account_number = :compte_produits_interets),
        :taux_bp, :periodicite, :methode_amortissement
    )
    ON CONFLICT (code) DO UPDATE SET
        name                         = EXCLUDED.name,
        compte_credit_membre_id      = EXCLUDED.compte_credit_membre_id,
        compte_credit_client_id      = EXCLUDED.compte_credit_client_id,
        compte_produits_interets_id  = EXCLUDED.compte_produits_interets_id,
        taux_bp                      = EXCLUDED.taux_bp,
        periodicite                  = EXCLUDED.periodicite,
        methode_amortissement        = EXCLUDED.methode_amortissement,
        updated_at                   = NOW()
    """
)


def executer_seed_produits_credit(db: Session) -> int:
    """Installe/actualise le produit de crédit de démonstration. Ne committe pas."""
    for produit in PRODUITS:
        db.execute(
            _UPSERT,
            {
                "code": produit.code,
                "name": produit.name,
                "compte_membre": produit.compte_membre,
                "compte_client": produit.compte_client,
                "compte_produits_interets": produit.compte_produits_interets,
                "taux_bp": produit.taux_bp,
                "periodicite": produit.periodicite,
                "methode_amortissement": produit.methode_amortissement,
            },
        )
    return len(PRODUITS)
