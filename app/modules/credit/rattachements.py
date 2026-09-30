"""Rattachements comptables des produits de crédit (miroir de `epargne/rattachements.py`,
premier bloc « comptable » que le crédit n'avait pas encore — contrairement à l'épargne, qui
avait déjà ces deux écrans avant le chantier « gestion des produits »).

Distinct de `gestion_produits.py` (lot 3a, cycle de vie + paramètres métier, admin fonctionnel,
`credit.product.manage`) : ici, la LECTURE et l'ÉCRITURE des 3 comptes eux-mêmes, depuis
l'écran du comptable (`compta.plan.manage`). `decaissement.py`/`remboursement.py` gèrent déjà
proprement un rattachement absent (refus humain à la résolution, `RattachementManquantError`) —
vider un rattachement est donc une action LÉGITIME ici, pas une erreur.

GARDE-FOU : chaque compte soumis passe par `comptes.compte_saisie_actif` (comptabilite), qui
refuse tout compte de regroupement ou désactivé — même soumis directement à l'API, en
contournant le sélecteur. Même discipline que l'épargne.

Aucun impact sur le passé : les écritures déjà posées référencent directement un compte concret
(`journal_lines.account_id`, figé à la pose), jamais ce paramètre.
"""

import uuid

from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.comptabilite.comptes import compte_saisie_actif
from app.modules.comptabilite.models import Account
from app.modules.credit.models import Product

RESSOURCE = "credit.product"


def modifier_rattachements(
    db: Session,
    produit: Product,
    *,
    compte_credit_membre_number: str | None,
    compte_credit_client_number: str | None,
    compte_produits_interets_number: str | None,
    motif: str,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> Product:
    """Re-pointe les 3 rattachements — MOTIF obligatoire, tracé avant/après. Les garde-fous de
    `compte_saisie_actif` remontent TELS QUELS si l'un des comptes soumis est invalide.

    Aucune vérification de cohérence avec `is_provisional` ici : un produit déjà VALIDÉ garde
    ses rattachements modifiables (le comptable doit pouvoir corriger une erreur après coup,
    comme pour l'épargne) — seule `valider_produit` (gestion_produits.py, lot 3a) contrôle leur
    PRÉSENCE au moment de lever le provisoire, jamais cette fonction-ci.
    """
    nouveau_membre = (
        compte_saisie_actif(db, compte_credit_membre_number)
        if compte_credit_membre_number
        else None
    )
    nouveau_client = (
        compte_saisie_actif(db, compte_credit_client_number)
        if compte_credit_client_number
        else None
    )
    nouveau_interets = (
        compte_saisie_actif(db, compte_produits_interets_number)
        if compte_produits_interets_number
        else None
    )

    def _numero(account_id: uuid.UUID | None) -> str | None:
        if account_id is None:
            return None
        compte = db.get(Account, account_id)
        return compte.account_number if compte else None

    avant = {
        "compte_credit_membre": _numero(produit.compte_credit_membre_id),
        "compte_credit_client": _numero(produit.compte_credit_client_id),
        "compte_produits_interets": _numero(produit.compte_produits_interets_id),
    }

    produit.compte_credit_membre_id = nouveau_membre.id if nouveau_membre else None
    produit.compte_credit_client_id = nouveau_client.id if nouveau_client else None
    produit.compte_produits_interets_id = nouveau_interets.id if nouveau_interets else None
    produit.updated_by = par
    db.flush()

    ecrire_audit(
        db,
        action="credit.product.comptes_updated",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=produit.id,
        old_values=avant,
        new_values={
            "compte_credit_membre": compte_credit_membre_number,
            "compte_credit_client": compte_credit_client_number,
            "compte_produits_interets": compte_produits_interets_number,
            "motif": motif,
        },
    )
    return produit
