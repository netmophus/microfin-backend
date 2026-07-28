"""Rapprochement collectif ↔ auxiliaire — LA loi qui relie les deux modules.

Le compte général (3111) porte le TOTAL de l'épargne à vue de tous les membres ; l'auxiliaire
(les comptes d'épargne, un par membre) porte le DÉTAIL. L'invariant BCEAO :

    Σ (soldes des comptes d'épargne rattachés à 3111)  ==  solde comptable de 3111

Un écart entre les deux est le premier signe d'une fraude ou d'un bug — c'est ce qu'un contrôleur
vérifie en premier. On veut le détecter NOUS-MÊMES. Cette fonction calcule les deux côtés et
compare ; elle sert de contrôle de cohérence (rapprochement), à lancer périodiquement.

FONDATIONS posées ici (E1) : la fonction existe, le compte général est identifiable. Elle MORDRA
pour de vrai en E3, quand les dépôts/retraits produiront ensemble le mouvement (auxiliaire) et la
pièce (général) dans la même transaction. Le solde-cache d'un compte est déjà couvert, lui, par
`verifier_coherence_solde` (cache vs Σ mouvements du compte) ; ici c'est l'échelon AU-DESSUS,
entre modules.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.modules.comptabilite.models import Account


@dataclass(frozen=True)
class Rapprochement:
    """Résultat d'un rapprochement entre l'auxiliaire (épargne) et le général (comptabilité)."""

    compte_general: str  # numéro du compte du plan (ex. 3111)
    auxiliaire: int  # Σ des soldes des comptes d'épargne rattachés
    general: int  # solde comptable du compte (écritures validées)
    concordant: bool
    ecart: int  # auxiliaire moins general (0 si concordant)


def rapprocher(db: Session, compte_general_id: uuid.UUID) -> Rapprochement:
    """Rapproche l'épargne auxiliaire du compte général `compte_general_id` (ex. 3111).

    - auxiliaire : Σ des soldes-cache des comptes d'épargne dont le produit pointe sur ce compte ;
    - general : solde comptable du compte, calculé sur les écritures VALIDÉES. Le compte d'épargne
      est de sens crédit (une dette) : solde = somme des crédits moins somme des débits.
    """
    auxiliaire = db.execute(
        text(
            "SELECT COALESCE(SUM(a.balance), 0) FROM epargne.accounts a "
            "JOIN epargne.products p ON p.id = a.product_id "
            "WHERE p.compte_epargne_id = :c"
        ),
        {"c": compte_general_id},
    ).scalar_one()

    general = db.execute(
        text(
            "SELECT COALESCE(SUM(CASE WHEN l.side = 'C' THEN l.amount ELSE -l.amount END), 0) "
            "FROM comptabilite.journal_lines l "
            "JOIN comptabilite.journal_entries e ON e.id = l.entry_id "
            "WHERE l.account_id = :c AND e.status = 'validee'"
        ),
        {"c": compte_general_id},
    ).scalar_one()

    numero = db.execute(
        select(Account.account_number).where(Account.id == compte_general_id)
    ).scalar_one()

    return Rapprochement(
        compte_general=numero,
        auxiliaire=int(auxiliaire),
        general=int(general),
        concordant=(auxiliaire == general),
        ecart=int(auxiliaire) - int(general),
    )


def rapprocher_tout(db: Session) -> list[Rapprochement]:
    """Rapproche CHAQUE compte collectif rattaché à un produit d'épargne (3111, 3121, 3131…).

    Un contrôleur veut la vue d'ensemble, pas un compte à la fois : on liste les comptes généraux
    DISTINCTS pointés par les produits et on rapproche chacun. Trié par numéro pour une lecture
    stable. Un produit sans rattachement (compte_epargne_id NULL, provisoire) n'a pas de général à
    rapprocher : il est ignoré ici.
    """
    ids = db.execute(
        text(
            "SELECT DISTINCT compte_epargne_id FROM epargne.products "
            "WHERE compte_epargne_id IS NOT NULL"
        )
    ).scalars()
    resultats = [rapprocher(db, compte_id) for compte_id in ids]
    return sorted(resultats, key=lambda r: r.compte_general)
