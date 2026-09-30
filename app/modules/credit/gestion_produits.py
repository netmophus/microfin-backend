"""Cycle de vie du référentiel produit de crédit : création, modification métier, validation
(lever le provisoire), activation/désactivation du catalogue.

Miroir de `epargne/gestion_produits.py`, avec DEUX différences de fond (garde-fou de
validation STRICT, décidé pour le crédit) :

  1. Un produit crédit a normalement vocation à porter un taux non nul (contrairement à
     l'épargne à vue, qui peut légitimement rester à taux 0 pour toujours) : si `taux_bp > 0`,
     `compte_produits_interets_id` doit être rattaché, SINON le produit échouera au premier
     remboursement portant une part d'intérêts (`remboursement.py`, `RattachementManquantError`).
     La validation le refuse donc AUSSI dans ce cas — pas seulement sur le compte membre absent.
  2. `taux_usure_max_bp` (migration 0050, plafond paramétrable, NULL = pas de plafond) est
     revérifié À LA CRÉATION ET À LA MODIFICATION, pas seulement à la validation : un produit ne
     doit jamais exister, même provisoire, avec un taux qui dépasse son propre plafond déclaré.

Distinct de `rattachements.py` (comptes comptables, `compta.plan.manage`, lot 3b) : ici,
l'EXISTENCE, l'ÉTAT et les PARAMÈTRES MÉTIER (taux, amortissement) du produit — gardé
`credit.product.manage` (ADMIN_FONCTIONNEL) au routeur, jamais `compta.plan.manage`. Pas
d'endpoint `parametres-interet` côté crédit, à dessein : un seul chemin d'écriture par champ,
symétrique de l'épargne (dont l'équivalent a été retiré pour la même raison — voir
`epargne/gestion_produits.py`). Même patron d'écriture que le reste du projet : pré-contrôle
métier (erreur typée, jamais un IntegrityError brut en 500), `db.flush()`, audit, pas de
`db.commit()` ici (fait par le routeur).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.credit.models import Product

RESSOURCE = "credit.product"


class CodeDejaUtiliseError(Exception):
    """Le code produit existe déjà."""


class ComptesNonRattachesError(Exception):
    """Le compte membre (indispensable) n'est pas rattaché, ou le taux est non nul sans compte
    d'intérêts rattaché : validation refusée."""


class TauxDepasseUsureError(Exception):
    """Le taux dépasse le plafond d'usure déclaré par le produit lui-même."""


def _verifier_taux_usure(taux_bp: int, taux_usure_max_bp: int | None) -> None:
    if taux_usure_max_bp is not None and taux_bp > taux_usure_max_bp:
        raise TauxDepasseUsureError(
            f"Le taux ({taux_bp} bp) dépasse le plafond d'usure déclaré pour ce produit "
            f"({taux_usure_max_bp} bp)."
        )


def creer_produit(
    db: Session,
    *,
    code: str,
    name: str,
    taux_bp: int,
    periodicite: str,
    methode_amortissement: str,
    base_jours: int,
    regle_arrondi: str,
    taux_usure_max_bp: int | None,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> Product:
    """Crée un produit PROVISOIRE (is_provisional/is_active = TRUE), sans aucun compte
    rattaché — ça reste le rôle de l'écran de rattachement, à faire ensuite.

    Pré-contrôle d'unicité (message clair) ; l'UNIQUE en base reste le filet de sécurité pour
    une course rare entre deux créations simultanées du même code (l'appelant traduit
    l'IntegrityError, même patron que `epargne.gestion_produits.creer_produit`).
    """
    _verifier_taux_usure(taux_bp, taux_usure_max_bp)

    deja = db.execute(select(Product.id).where(Product.code == code)).scalar_one_or_none()
    if deja is not None:
        raise CodeDejaUtiliseError(
            f"Le code « {code} » est déjà utilisé par un autre produit de crédit."
        )

    produit = Product(
        code=code,
        name=name,
        taux_bp=taux_bp,
        periodicite=periodicite,
        methode_amortissement=methode_amortissement,
        base_jours=base_jours,
        regle_arrondi=regle_arrondi,
        taux_usure_max_bp=taux_usure_max_bp,
        created_by=par,
        updated_by=par,
    )
    db.add(produit)
    db.flush()

    ecrire_audit(
        db,
        action="credit.product.created",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=produit.id,
        new_values={"code": code, "name": name, "taux_bp": taux_bp},
    )
    return produit


def modifier_produit(
    db: Session,
    produit: Product,
    *,
    name: str,
    taux_bp: int,
    periodicite: str,
    methode_amortissement: str,
    base_jours: int,
    regle_arrondi: str,
    taux_usure_max_bp: int | None,
    motif: str,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> Product:
    """Remplace les champs MÉTIER (nom, taux, amortissement) — jamais les comptes rattachés.
    MOTIF obligatoire, tracé avant/après. Ne touche pas à `is_active`/`is_provisional`."""
    _verifier_taux_usure(taux_bp, taux_usure_max_bp)

    avant = {
        "name": produit.name,
        "taux_bp": produit.taux_bp,
        "periodicite": produit.periodicite,
        "methode_amortissement": produit.methode_amortissement,
        "base_jours": produit.base_jours,
        "regle_arrondi": produit.regle_arrondi,
        "taux_usure_max_bp": produit.taux_usure_max_bp,
    }

    produit.name = name
    produit.taux_bp = taux_bp
    produit.periodicite = periodicite
    produit.methode_amortissement = methode_amortissement
    produit.base_jours = base_jours
    produit.regle_arrondi = regle_arrondi
    produit.taux_usure_max_bp = taux_usure_max_bp
    produit.updated_by = par
    db.flush()

    ecrire_audit(
        db,
        action="credit.product.updated",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=produit.id,
        old_values=avant,
        new_values={
            "name": name,
            "taux_bp": taux_bp,
            "periodicite": periodicite,
            "methode_amortissement": methode_amortissement,
            "base_jours": base_jours,
            "regle_arrondi": regle_arrondi,
            "taux_usure_max_bp": taux_usure_max_bp,
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
    """Lève `is_provisional` — GARDE-FOU STRICT (décidé pour le crédit, plus large que
    l'épargne) :
      - refuse si `compte_credit_membre_id` absent (le minimum indispensable) ;
      - refuse AUSSI si `taux_bp > 0` ET `compte_produits_interets_id` absent — un produit à
        taux non nul sans ce rattachement échouera au premier remboursement portant une part
        d'intérêts (`remboursement.py`), un piège silencieux que le garde-fou épargne (taux
        pouvant légitimement rester nul) n'avait pas à couvrir.
    `compte_credit_client_id` absent ne bloque pas (repli légitime sur le compte membre, comme
    l'épargne PS3) mais produit un AVERTISSEMENT non bloquant. Retourne la liste des
    avertissements (vide si aucun)."""
    if produit.compte_credit_membre_id is None:
        raise ComptesNonRattachesError(
            "Impossible de valider ce produit : aucun compte membre rattaché "
            "(voir l'écran de rattachement comptable)."
        )
    if produit.taux_bp > 0 and produit.compte_produits_interets_id is None:
        raise ComptesNonRattachesError(
            "Impossible de valider ce produit : le taux est non nul mais aucun compte de "
            "produits d'intérêts n'est rattaché — le premier remboursement portant une part "
            "d'intérêts échouerait (voir l'écran de rattachement comptable)."
        )

    avertissements: list[str] = []
    if produit.compte_credit_client_id is None:
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
        action="credit.product.validated",
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
    """(Dés)active le produit — le retire/rétablit dans le catalogue proposé à la demande
    (`consultation.lister_produits` ne renvoie que les actifs), sans jamais le supprimer
    (§15). MOTIF obligatoire dans les deux sens, patron exact de
    `caisse.postes.changer_activation` / `epargne.gestion_produits.changer_activation_produit`."""
    avant = produit.is_active
    produit.is_active = is_active
    produit.updated_by = par
    db.flush()

    ecrire_audit(
        db,
        action="credit.product.activated" if is_active else "credit.product.deactivated",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=produit.id,
        old_values={"is_active": avant},
        new_values={"is_active": is_active, "motif": motif},
    )
    return produit
