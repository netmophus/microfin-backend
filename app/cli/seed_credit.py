"""Seed du produit de crédit de démonstration et des paliers de souffrance (CR5a) — DONNÉES
provisoires, comme le plan de comptes et les produits d'épargne (voir seed_epargne.py).

Un SEUL produit, taux NON NUL À DESSEIN : contrairement aux produits d'épargne (taux 0 par
défaut, jamais démontrés dans leur seed), celui-ci sert à voir un échéancier RÉEL se calculer au
décaissement (CR3+) — demande explicite de l'utilisateur pour tester le parcours, PAS une
donnée réglementaire. `is_provisional = TRUE` comme tout le reste : à valider par l'expert-
comptable/crédit avant production, taux inclus.

Paliers de souffrance : 4 lignes DE DÉPART (seuils/taux provisoires, vocabulaire BCEAO —
~6 mois pour « douteux »), comptes d'encours/dotation posés à NULL — pas codés en dur, remplis
via l'écran de paramétrage (Bloc 5), exactement comme les rattachements produit. Le nombre de
paliers n'est PAS figé par ce seed : l'écran permet d'en ajouter/retirer sans migration.
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


@dataclass(frozen=True)
class PalierDemo:
    code: str
    libelle: str
    seuil_jours: int
    taux_provision_bp: int  # PROVISOIRE — vocabulaire BCEAO, pas une valeur validée
    is_terminal: bool = False


# 4 paliers de départ (seuil_jours sert lui-même de clé de tri — voir migration 0036). Comptes
# d'encours/dotation VOLONTAIREMENT absents d'ici : posés à NULL, remplis via l'écran (jamais
# codés en dur dans un seed, même provisoire).
PALIERS: tuple[PalierDemo, ...] = (
    PalierDemo("IMPAYE", "Impayé simple", 1, 0),
    PalierDemo("SOUFFRANCE", "Créance en souffrance", 30, 1000),  # 10%, à valider
    PalierDemo("DOUTEUX", "Créance douteuse", 180, 5000),  # 50%, à valider
    PalierDemo("IRRECOUVRABLE", "Créance irrécouvrable", 365, 10000, is_terminal=True),
)

_UPSERT_PALIER = text(
    """
    INSERT INTO credit.delinquency_tiers
        (code, libelle, seuil_jours, taux_provision_bp, is_terminal, is_provisional)
    VALUES (:code, :libelle, :seuil_jours, :taux_provision_bp, :is_terminal, TRUE)
    ON CONFLICT (code) DO UPDATE SET
        libelle           = EXCLUDED.libelle,
        seuil_jours       = EXCLUDED.seuil_jours,
        taux_provision_bp = EXCLUDED.taux_provision_bp,
        is_terminal       = EXCLUDED.is_terminal,
        updated_at        = NOW()
    """
)


def executer_seed_paliers_souffrance(db: Session) -> int:
    """Installe/actualise les paliers de souffrance de départ. NE TOUCHE JAMAIS les comptes
    (compte_encours_id/compte_dotation_id) : un ré-import ne doit pas effacer un rattachement
    déjà posé par l'écran — même discipline que le plan de comptes (Bloc 5). Ne committe pas."""
    for palier in PALIERS:
        db.execute(
            _UPSERT_PALIER,
            {
                "code": palier.code,
                "libelle": palier.libelle,
                "seuil_jours": palier.seuil_jours,
                "taux_provision_bp": palier.taux_provision_bp,
                "is_terminal": palier.is_terminal,
            },
        )
    return len(PALIERS)
