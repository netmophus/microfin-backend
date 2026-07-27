"""Service Épargne — ouverture d'un compte et vérification de cohérence du solde.

OUVERTURE : réservée à un membre ACTIF (gate KYC). Un prospect — y compris un ancien membre
redevenu prospect par réactivation — n'a pas de compte. Double garde-fou : ici (erreur métier
claire) et en base (trigger `trg_compte_membre_actif`, dernier rempart). L'ouverture ne bouge
aucun argent : le compte naît à solde 0.

COHÉRENCE DU SOLDE : le solde stocké (`balance`) est un CACHE. La vérité est la somme des
mouvements. `verifier_coherence_solde` recalcule la somme (credit moins debit) et la compare au
cache -
c'est le filet qui détecte toute divergence (et servira au rapprochement périodique). Même
principe que le score de risque des tiers : le cache est un reflet, l'historique est la vérité.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.epargne import numerotation
from app.modules.epargne.models import SavingsAccount


class OuvertureError(Exception):
    """Base des refus d'ouverture de compte."""


class MembreNonActifError(OuvertureError):
    """Le tiers n'est pas un membre actif (prospect, suspendu, désactivé) : pas de compte."""


class ProduitIntrouvableError(OuvertureError):
    """Produit d'épargne inexistant ou inactif."""


class FermetureError(Exception):
    """Base des refus de fermeture de compte."""


class CompteNonSoldeError(FermetureError):
    """Le compte porte encore un solde : on ne ferme pas un compte non vidé."""


class CompteDejaClotureError(FermetureError):
    """Le compte est déjà clôturé."""


@dataclass(frozen=True)
class CoherenceSolde:
    """Résultat d'un contrôle de cohérence entre le solde-cache et la somme des mouvements."""

    cache: int
    calcule: int  # somme (credit moins debit)
    coherent: bool
    ecart: int  # cache moins calcule (0 si cohérent)


class ActionsAudit:
    """Actions d'audit du module Épargne (format module.action)."""

    COMPTE_OUVERT = "epargne.compte.ouvert"
    COMPTE_CLOTURE = "epargne.compte.cloture"


def _statut_tier(db: Session, tier_id: uuid.UUID) -> str | None:
    return db.execute(
        text("SELECT status FROM tiers.tiers WHERE id = :id AND deleted_at IS NULL"),
        {"id": tier_id},
    ).scalar_one_or_none()


def ouvrir_compte(
    db: Session,
    *,
    tier_id: uuid.UUID,
    product_id: uuid.UUID,
    agency_id: uuid.UUID,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> SavingsAccount:
    """Ouvre un compte d'épargne pour un membre ACTIF. Solde initial 0. Numéro atomique.

    Refuse (gate KYC) si le tiers n'est pas 'actif' : un prospect n'a pas de compte.
    """
    statut = _statut_tier(db, tier_id)
    if statut != "actif":
        raise MembreNonActifError(
            f"le membre (tier={tier_id}) n'est pas actif (statut={statut}) : "
            "seul un membre actif peut ouvrir un compte d'épargne"
        )

    produit = db.execute(
        text("SELECT 1 FROM epargne.products WHERE id = :id AND is_active"),
        {"id": product_id},
    ).scalar_one_or_none()
    if produit is None:
        raise ProduitIntrouvableError(f"produit {product_id} inexistant ou inactif")

    compte = SavingsAccount(
        account_number=numerotation.prochain_numero(db),
        product_id=product_id,
        tier_id=tier_id,
        agency_id=agency_id,
        status="actif",
        balance=0,
        opened_by=par,
        created_by=par,
        updated_by=par,
    )
    db.add(compte)
    db.flush()  # le trigger base confirme le gate KYC ; le numéro est consommé dans la transaction

    ecrire_audit(
        db,
        action=ActionsAudit.COMPTE_OUVERT,
        contexte=contexte,
        acteur_id=par,
        resource_type="epargne.account",
        resource_id=compte.id,
        agency_id=agency_id,
        new_values={"account_number": compte.account_number, "tier_id": str(tier_id)},
    )
    return compte


def verifier_coherence_solde(db: Session, account_id: uuid.UUID) -> CoherenceSolde:
    """Recalcule la somme des mouvements et la compare au solde-cache du compte.

    La vérité est la somme des mouvements ; le cache n'est qu'un reflet. Sert de filet (test)
    et, plus tard, de contrôle de cohérence périodique (rapprochement).
    """
    cache = db.execute(
        select(SavingsAccount.balance).where(SavingsAccount.id == account_id)
    ).scalar_one()
    calcule = db.execute(
        text(
            "SELECT COALESCE(SUM(CASE WHEN sens = 'credit' THEN amount ELSE -amount END), 0) "
            "FROM epargne.movements WHERE account_id = :a"
        ),
        {"a": account_id},
    ).scalar_one()
    return CoherenceSolde(
        cache=cache, calcule=calcule, coherent=(cache == calcule), ecart=cache - calcule
    )


def cloturer_compte(
    db: Session,
    compte: SavingsAccount,
    par: uuid.UUID | None,
    *,
    motif: str | None = None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> SavingsAccount:
    """Ferme un compte SOLDÉ. Refuse s'il reste un solde. La fermeture est distincte du solde 0 :
    un compte vidé mais non fermé reste OUVERT (et bloque la désactivation du membre).

    On vérifie le solde par la VÉRITÉ (somme des mouvements), pas seulement le cache.
    """
    if compte.status == "cloture":
        raise CompteDejaClotureError(f"compte {compte.account_number} déjà clôturé")

    coherence = verifier_coherence_solde(db, compte.id)
    if coherence.calcule != 0:
        raise CompteNonSoldeError(
            f"compte {compte.account_number} : solde de {coherence.calcule} F — "
            "videz le compte avant de le fermer"
        )

    compte.status = "cloture"
    compte.closed_by = par
    compte.closure_reason = motif
    compte.updated_by = par
    compte.closed_at = db.execute(text("SELECT NOW()")).scalar_one()
    db.flush()

    ecrire_audit(
        db,
        action=ActionsAudit.COMPTE_CLOTURE,
        contexte=contexte,
        acteur_id=par,
        resource_type="epargne.account",
        resource_id=compte.id,
        agency_id=compte.agency_id,
        old_values={"status": "actif"},
        new_values={"status": "cloture", "account_number": compte.account_number},
    )
    return compte
