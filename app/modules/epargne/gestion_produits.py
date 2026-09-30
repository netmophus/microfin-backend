"""Cycle de vie du référentiel produit d'épargne : création, modification métier, validation
(lever le provisoire), activation/désactivation du catalogue.

Distinct de `rattachements.py` (comptes comptables) et `parametres_interet.py` (taux/calcul) :
ici, l'EXISTENCE et l'ÉTAT du produit lui-même. Gardé `epargne.product.manage`
(ADMIN_FONCTIONNEL) au routeur — jamais `compta.plan.manage`, qui reste réservé au comptable
pour les 2 fichiers ci-dessus. Même patron d'écriture que le reste du module : pré-contrôle
métier (erreur typée, jamais un IntegrityError brut en 500), `db.flush()`, audit, pas de
`db.commit()` ici (fait par le routeur).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.epargne.models import Product

RESSOURCE = "epargne.product"


class CodeDejaUtiliseError(Exception):
    """Le code produit existe déjà."""


class ComptesNonRattachesError(Exception):
    """Le compte membre (indispensable) n'est pas rattaché : validation refusée."""


def creer_produit(
    db: Session,
    *,
    code: str,
    name: str,
    type: str,
    currency: str,
    taux_bp: int,
    periodicite: str,
    methode_calcul_solde: str,
    base_jours: int,
    regle_arrondi: str,
    solde_minimum_remunere: int,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> Product:
    """Crée un produit PROVISOIRE (is_provisional/is_active = TRUE), sans aucun compte rattaché
    — ça reste le rôle de `rattachements.py`, à faire ensuite depuis l'écran du comptable.

    Pré-contrôle d'unicité (message clair) ; l'UNIQUE en base reste le filet de sécurité pour
    une course rare entre deux créations simultanées du même code (l'appelant traduit
    l'IntegrityError, même patron que `comptabilite.comptes.creer`).
    """
    deja = db.execute(select(Product.id).where(Product.code == code)).scalar_one_or_none()
    if deja is not None:
        raise CodeDejaUtiliseError(
            f"Le code « {code} » est déjà utilisé par un autre produit d'épargne."
        )

    produit = Product(
        code=code,
        name=name,
        type=type,
        currency=currency,
        taux_bp=taux_bp,
        periodicite=periodicite,
        methode_calcul_solde=methode_calcul_solde,
        base_jours=base_jours,
        regle_arrondi=regle_arrondi,
        solde_minimum_remunere=solde_minimum_remunere,
        created_by=par,
        updated_by=par,
    )
    db.add(produit)
    db.flush()

    ecrire_audit(
        db,
        action="epargne.product.created",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=produit.id,
        new_values={"code": code, "name": name, "type": type, "taux_bp": taux_bp},
    )
    return produit


def modifier_produit(
    db: Session,
    produit: Product,
    *,
    name: str,
    type: str,
    taux_bp: int,
    periodicite: str,
    methode_calcul_solde: str,
    base_jours: int,
    regle_arrondi: str,
    solde_minimum_remunere: int,
    motif: str,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> Product:
    """Remplace les champs MÉTIER (nom, type, paramètres d'intérêt) — jamais les comptes
    rattachés (`rattachements.py`, écran distinct, permission distincte). MOTIF obligatoire,
    tracé avant/après. Ne touche pas à `is_active`/`is_provisional`."""
    avant = {
        "name": produit.name,
        "type": produit.type,
        "taux_bp": produit.taux_bp,
        "periodicite": produit.periodicite,
        "methode_calcul_solde": produit.methode_calcul_solde,
        "base_jours": produit.base_jours,
        "regle_arrondi": produit.regle_arrondi,
        "solde_minimum_remunere": produit.solde_minimum_remunere,
    }

    produit.name = name
    produit.type = type
    produit.taux_bp = taux_bp
    produit.periodicite = periodicite
    produit.methode_calcul_solde = methode_calcul_solde
    produit.base_jours = base_jours
    produit.regle_arrondi = regle_arrondi
    produit.solde_minimum_remunere = solde_minimum_remunere
    produit.updated_by = par
    db.flush()

    ecrire_audit(
        db,
        action="epargne.product.updated",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=produit.id,
        old_values=avant,
        new_values={
            "name": name,
            "type": type,
            "taux_bp": taux_bp,
            "periodicite": periodicite,
            "methode_calcul_solde": methode_calcul_solde,
            "base_jours": base_jours,
            "regle_arrondi": regle_arrondi,
            "solde_minimum_remunere": solde_minimum_remunere,
            "motif": motif,
        },
    )
    return produit


def valider_produit(
    db: Session,
    produit: Product,
    *,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> list[str]:
    """Lève `is_provisional` — un produit ne devient définitif que si sa comptabilité permet de
    fonctionner. Seul le compte MEMBRE (`compte_epargne_id`) est indispensable : le compte
    CLIENT (`compte_epargne_client_id`, PS3) est optionnel par construction (une IMCEC n'a que
    des membres — voir `service.ouvrir_compte`, repli sur le compte membre si absent). Son
    absence ne bloque donc PAS la validation, mais produit un AVERTISSEMENT non bloquant : la
    bascule client -> membre ne doit jamais être silencieuse (risque d'imputation en audit
    BCEAO). Retourne la liste des avertissements (vide si aucun)."""
    if produit.compte_epargne_id is None:
        raise ComptesNonRattachesError(
            "Impossible de valider ce produit : aucun compte membre rattaché "
            "(voir l'écran de rattachement comptable)."
        )

    avertissements: list[str] = []
    if produit.compte_epargne_client_id is None:
        avertissements.append(
            "Compte client non rattaché — toutes les opérations (membres et clients) seront "
            "imputées sur le compte membre."
        )

    avant = produit.is_provisional
    produit.is_provisional = False
    produit.updated_by = par
    db.flush()

    ecrire_audit(
        db,
        action="epargne.product.validated",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=produit.id,
        old_values={"is_provisional": avant},
        new_values={"is_provisional": False, "avertissements": avertissements},
    )
    return avertissements


def changer_activation_produit(
    db: Session,
    produit: Product,
    *,
    is_active: bool,
    motif: str,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> Product:
    """(Dés)active le produit — le retire/rétablit dans le catalogue proposé à l'ouverture d'un
    compte (`consultation.lister_produits` ne renvoie que les actifs), sans jamais le supprimer
    (§15). MOTIF obligatoire dans les deux sens, patron exact de
    `caisse.postes.changer_activation`."""
    avant = produit.is_active
    produit.is_active = is_active
    produit.updated_by = par
    db.flush()

    ecrire_audit(
        db,
        action="epargne.product.activated" if is_active else "epargne.product.deactivated",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=produit.id,
        old_values={"is_active": avant},
        new_values={"is_active": is_active, "motif": motif},
    )
    return produit
