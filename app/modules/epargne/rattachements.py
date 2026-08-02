"""Rattachements comptables des produits d'épargne (Bloc 5 du paramétrage comptable).

Distinct de `operations.py` (résolution AU MOMENT de poser une pièce) : ici, c'est la LECTURE et
l'ÉCRITURE du paramètre lui-même, depuis l'écran du comptable. `operations.py` gère déjà
proprement un rattachement absent (refus humain à la première opération, RattachementManquantError)
— vider un rattachement est donc une action LÉGITIME ici, pas une erreur.

GARDE-FOU : chaque compte soumis passe par `comptes.compte_saisie_actif` (comptabilite), qui
refuse tout compte de regroupement ou désactivé — même soumis directement à l'API, en
contournant le sélecteur.

Aucun impact sur le passé : les écritures déjà posées référencent directement un compte
concret (`journal_lines.account_id`, figé à la pose), jamais ce paramètre. Changer un
rattachement ne peut affecter que les PROCHAINES opérations, par construction.
"""

import uuid

from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.comptabilite.comptes import compte_saisie_actif
from app.modules.comptabilite.models import Account
from app.modules.epargne.models import Product

RESSOURCE = "epargne.product"


def modifier_rattachements(
    db: Session,
    produit: Product,
    *,
    compte_epargne_number: str | None,
    compte_epargne_client_number: str | None,
    compte_charge_interet_number: str | None,
    motif: str,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> Product:
    """Re-pointe les 3 rattachements — MOTIF obligatoire, tracé avant/après. Les garde-fous de
    `compte_saisie_actif` remontent TELS QUELS si l'un des comptes soumis est invalide."""
    nouveau_epargne = (
        compte_saisie_actif(db, compte_epargne_number) if compte_epargne_number else None
    )
    nouveau_client = (
        compte_saisie_actif(db, compte_epargne_client_number)
        if compte_epargne_client_number
        else None
    )
    nouveau_interet = (
        compte_saisie_actif(db, compte_charge_interet_number)
        if compte_charge_interet_number
        else None
    )

    def _numero(account_id: uuid.UUID | None) -> str | None:
        if account_id is None:
            return None
        compte = db.get(Account, account_id)
        return compte.account_number if compte else None

    avant = {
        "compte_epargne": _numero(produit.compte_epargne_id),
        "compte_epargne_client": _numero(produit.compte_epargne_client_id),
        "compte_charge_interet": _numero(produit.compte_charge_interet_id),
    }

    produit.compte_epargne_id = nouveau_epargne.id if nouveau_epargne else None
    produit.compte_epargne_client_id = nouveau_client.id if nouveau_client else None
    produit.compte_charge_interet_id = nouveau_interet.id if nouveau_interet else None
    produit.updated_by = par
    db.flush()

    ecrire_audit(
        db,
        action="epargne.product.comptes_updated",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=produit.id,
        old_values=avant,
        new_values={
            "compte_epargne": compte_epargne_number,
            "compte_epargne_client": compte_epargne_client_number,
            "compte_charge_interet": compte_charge_interet_number,
            "motif": motif,
        },
    )
    return produit
