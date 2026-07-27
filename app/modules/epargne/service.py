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
from app.modules.epargne.models import SavingsAccount, SavingsMovement
from app.modules.epargne.operations import TYPE_CLOTURE, poser_ecriture_operation


class OuvertureError(Exception):
    """Base des refus d'ouverture de compte."""


class MembreNonActifError(OuvertureError):
    """Le tiers n'est pas un membre actif (prospect, suspendu, désactivé) : pas de compte."""


class ProduitIntrouvableError(OuvertureError):
    """Produit d'épargne inexistant ou inactif."""


class FermetureError(Exception):
    """Base des refus de fermeture de compte."""


class CompteDebiteurError(FermetureError):
    """Le compte est débiteur (solde < 0) : on régularise avant de fermer, pas en fermant."""


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


def _mouvement_restitution(
    db: Session, compte: SavingsAccount, montant: int, piece_id: uuid.UUID, par: uuid.UUID | None
) -> None:
    """Écrit le mouvement de restitution de clôture, relié à sa pièce (helper isolé, testable)."""
    db.add(
        SavingsMovement(
            account_id=compte.id,
            sens="debit",
            amount=montant,
            balance_after=0,
            operation_type="cloture",
            journal_entry_id=piece_id,
            created_by=par,
        )
    )
    db.flush()


def cloturer_compte(
    db: Session,
    compte: SavingsAccount,
    par: uuid.UUID | None,
    *,
    motif: str | None = None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> SavingsAccount:
    """Ferme un compte (cœur, sur un compte déjà chargé). Acte comptable, TOUT dans la transaction
    de l'appelant :
      - solde > 0 : RESTITUTION (retrait final, pièce D 3111 / C 5721) + mouvement 'cloture' ;
      - solde = 0 : fermeture directe, aucune écriture ;
      - solde < 0 : REFUS (débiteur, on ne ferme pas sur une créance de l'IMF).
    Le passage en 'cloture' est DÉFINITIF (trigger 0022 : non réouvrable)."""
    if compte.status == "cloture":
        raise CompteDejaClotureError(f"compte {compte.account_number} déjà clôturé")
    if compte.balance < 0:
        raise CompteDebiteurError(
            f"compte {compte.account_number} débiteur ({compte.balance} F) : régularisez avant "
            "de fermer"
        )

    if compte.balance > 0:
        # Restitution en espèces : pièce de clôture (peut lever si rattachement manquant).
        piece = poser_ecriture_operation(
            db, compte, TYPE_CLOTURE, compte.balance, par, contexte=contexte
        )
        _mouvement_restitution(db, compte, compte.balance, piece.id, par)
        compte.balance = 0

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
