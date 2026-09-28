"""Paramètres d'intérêt d'un produit d'épargne (écran de paramétrage, Bloc 5 comptable).

Distinct de `rattachements.py` (qui pointe vers des comptes) : ici on règle les VALEURS qui
alimentent le moteur de calcul (`interets.py`) — taux, méthode de calcul du solde, base jours,
règle d'arrondi, solde minimum rémunéré. Tant que `taux_bp` reste à 0 (valeur d'amorçage), le
versement d'intérêts ne crédite jamais rien : c'est le manque que cet écran comble.

`periodicite` n'est PAS réglable ici : voir le commentaire dans `schemas.py` au-dessus de
`ParametresInteretProduit` — champ non branché au moteur, exposé plus tard le jour où il le sera.

Ne touche jamais `is_provisional` : régler le taux ne lève pas le provisoire, exactement comme
`modifier_rattachements` ne le fait pas pour les comptes. Le taux reste affiché provisoire tant
qu'aucun acte de validation experte distinct (qui n'existe pas encore) ne lève le drapeau.
"""

import uuid

from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.epargne.models import Product

RESSOURCE = "epargne.product"


def modifier_parametres_interet(
    db: Session,
    produit: Product,
    *,
    taux_bp: int,
    methode_calcul_solde: str,
    base_jours: int,
    regle_arrondi: str,
    solde_minimum_remunere: int,
    motif: str,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> Product:
    """Remplace les 5 paramètres d'intérêt — MOTIF obligatoire, tracé avant/après.

    Les bornes (taux 0-10000 bp, valeurs autorisées de méthode/arrondi/base jours) sont déjà
    vérifiées par le schéma Pydantic avant d'arriver ici ; les CHECK constraints SQL de la
    migration 0023 sont le dernier rempart si ce service était appelé autrement.
    """
    avant = {
        "taux_bp": produit.taux_bp,
        "methode_calcul_solde": produit.methode_calcul_solde,
        "base_jours": produit.base_jours,
        "regle_arrondi": produit.regle_arrondi,
        "solde_minimum_remunere": produit.solde_minimum_remunere,
    }

    produit.taux_bp = taux_bp
    produit.methode_calcul_solde = methode_calcul_solde
    produit.base_jours = base_jours
    produit.regle_arrondi = regle_arrondi
    produit.solde_minimum_remunere = solde_minimum_remunere
    produit.updated_by = par
    db.flush()

    ecrire_audit(
        db,
        action="epargne.product.interets_updated",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=produit.id,
        old_values=avant,
        new_values={
            "taux_bp": taux_bp,
            "methode_calcul_solde": methode_calcul_solde,
            "base_jours": base_jours,
            "regle_arrondi": regle_arrondi,
            "solde_minimum_remunere": solde_minimum_remunere,
            "motif": motif,
        },
    )
    return produit
