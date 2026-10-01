"""Seed du produit de crédit de démonstration et des paliers de souffrance (CR5a) — DONNÉES
provisoires, comme le plan de comptes et les produits d'épargne (voir seed_epargne.py).

Un SEUL produit, taux NON NUL À DESSEIN : contrairement aux produits d'épargne (taux 0 par
défaut, jamais démontrés dans leur seed), celui-ci sert à voir un échéancier RÉEL se calculer au
décaissement (CR3+) — demande explicite de l'utilisateur pour tester le parcours, PAS une
donnée réglementaire. `is_provisional = TRUE` comme tout le reste : à valider par l'expert-
comptable/crédit avant production, taux inclus.

Paliers de souffrance : 4 lignes DE DÉPART (chantier supervision de la souffrance, lot 1) —
barème provisoire « à valider » (régime SFD UEMOA, à confirmer contre le texte officiel) :
souffrance à partir de 90 jours, quotités de provision 40 % / 80 % / 100 % selon l'ancienneté.
RETARD (seuil 1 jour, taux 0 %) est un palier NON PROVISIONNÉ : il étiquette un retard court
pour la supervision SANS déclencher aucune écriture (voir reclassification.py, section « PALIER
NON PROVISIONNÉ ») — aucun compte ne lui est rattaché, aucun n'est exigé.

Comptes RCSFD rattachés aux 3 paliers RÉELLEMENT provisionnés — résolus ici par NUMÉRO (même
patron que les comptes du produit ci-dessus), PAS par UUID codé en dur : si le plan n'est pas
importé, le rattachement reste NULL plutôt que de faire échouer le seed. Appariement PAR
LIBELLÉ de tranche d'ancienneté (292/66412/2991 ↔ « 6 mois au plus », 293/6642/2992 ↔ « 6-12
mois », 294/6643/2993 ↔ « 12-24 mois »), compte de reprise 764 unique et partagé par
construction (pas de sous-tranche dans le référentiel officiel) — voir docs/conformite-credit.md
§2/§2bis pour la question réglementaire NON TRANCHÉE sur la correspondance 3 tranches (classe
29/299) vs 4 tranches (classe 664).

LE SEED NE TOUCHE JAMAIS LES COMPTES D'UN PALIER DÉJÀ EXISTANT (clause ON CONFLICT) : un
ré-import met à jour libellé/seuil/taux/is_terminal, jamais les 4 rattachements — un rattachement
déjà posé/corrigé via l'écran de paramétrage (Bloc 5) ne doit jamais être écrasé silencieusement.
Le nombre de paliers n'est PAS figé par ce seed : l'écran permet d'en ajouter/retirer sans
migration.
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
    taux_provision_bp: int  # PROVISOIRE — barème SFD UEMOA, à confirmer contre le texte officiel
    is_terminal: bool = False
    # Numéros de compte RCSFD (PAS d'UUID codé en dur, résolus par le SELECT de l'upsert) —
    # absents (None) pour un palier NON PROVISIONNÉ (taux 0) : aucun compte n'est exigé.
    compte_encours: str | None = None
    compte_dotation: str | None = None
    compte_provision: str | None = None
    compte_reprise: str | None = None


# 4 paliers de départ (seuil_jours sert lui-même de clé de tri — voir migration 0036). Barème
# provisoire (voir docstring module) : RETARD n'est pas provisionné (aucun compte, aucune
# écriture — voir reclassification.py) ; les 3 autres sont rattachés aux comptes RCSFD déjà
# identifiés pour le scénario de test CR5 (docs/conformite-credit.md §2bis).
PALIERS: tuple[PalierDemo, ...] = (
    PalierDemo("RETARD", "Retard simple", 1, 0),
    PalierDemo(
        "SOUFFRANCE", "Créance en souffrance", 90, 4000,  # 40 %, à valider
        compte_encours="292", compte_dotation="66412",
        compte_provision="2991", compte_reprise="764",
    ),
    PalierDemo(
        "DOUTEUX", "Créance douteuse", 180, 8000,  # 80 %, à valider
        compte_encours="293", compte_dotation="6642",
        compte_provision="2992", compte_reprise="764",
    ),
    PalierDemo(
        "IRRECOUVRABLE", "Créance irrécouvrable", 360, 10000, is_terminal=True,
        compte_encours="294", compte_dotation="6643",
        compte_provision="2993", compte_reprise="764",
    ),
)

# Rebaptise l'ancien palier de démonstration IMPAYE (seuil 1 j, barème précédent) en RETARD, SI
# présent : même ligne (même id, donc même historique DelinquencyEvent/Application.
# delinquency_tier_id), seul le code change. Sans ce renommage, l'upsert ci-dessous entrerait en
# conflit sur seuil_jours (colonne UNIQUE) avec l'ancien IMPAYE encore en base. No-op sur une
# base neuve (ni IMPAYE ni RETARD n'existent encore).
_RENOMMER_IMPAYE_EN_RETARD = text(
    """
    UPDATE credit.delinquency_tiers SET code = 'RETARD', updated_at = NOW()
    WHERE code = 'IMPAYE'
      AND NOT EXISTS (SELECT 1 FROM credit.delinquency_tiers WHERE code = 'RETARD')
    """
)

_UPSERT_PALIER = text(
    """
    INSERT INTO credit.delinquency_tiers
        (code, libelle, seuil_jours, taux_provision_bp, is_terminal, is_provisional,
         compte_encours_id, compte_dotation_id, compte_provision_id, compte_reprise_id)
    VALUES (
        :code, :libelle, :seuil_jours, :taux_provision_bp, :is_terminal, TRUE,
        (SELECT id FROM comptabilite.accounts WHERE account_number = :compte_encours),
        (SELECT id FROM comptabilite.accounts WHERE account_number = :compte_dotation),
        (SELECT id FROM comptabilite.accounts WHERE account_number = :compte_provision),
        (SELECT id FROM comptabilite.accounts WHERE account_number = :compte_reprise)
    )
    ON CONFLICT (code) DO UPDATE SET
        libelle           = EXCLUDED.libelle,
        seuil_jours       = EXCLUDED.seuil_jours,
        taux_provision_bp = EXCLUDED.taux_provision_bp,
        is_terminal       = EXCLUDED.is_terminal,
        updated_at        = NOW()
    -- Comptes volontairement ABSENTS de cette clause : un ré-import ne doit jamais écraser un
    -- rattachement déjà posé/corrigé via l'écran de paramétrage (Bloc 5).
    """
)


def executer_seed_paliers_souffrance(db: Session) -> int:
    """Installe/actualise les paliers de souffrance de départ et leurs comptes RCSFD (sur
    PREMIÈRE création seulement — voir _UPSERT_PALIER). Ne committe pas."""
    db.execute(_RENOMMER_IMPAYE_EN_RETARD)
    for palier in PALIERS:
        db.execute(
            _UPSERT_PALIER,
            {
                "code": palier.code,
                "libelle": palier.libelle,
                "seuil_jours": palier.seuil_jours,
                "taux_provision_bp": palier.taux_provision_bp,
                "is_terminal": palier.is_terminal,
                "compte_encours": palier.compte_encours,
                "compte_dotation": palier.compte_dotation,
                "compte_provision": palier.compte_provision,
                "compte_reprise": palier.compte_reprise,
            },
        )
    return len(PALIERS)
