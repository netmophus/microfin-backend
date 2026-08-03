"""Crédit CR1 — demande et décision (dossier, sans écriture comptable).

Pas de pièce comptable, pas de mouvement ici : décaissement et échéancier sont les blocs
suivants (CR2+). Gate KYC en DEUX temps (voir migration 0032, triggers) :
  - à la CRÉATION : le tiers doit être 'actif' ;
  - à L'APPROBATION (jamais au refus) : re-vérifié, une demande pouvant rester en_instruction
    plusieurs jours sans garantie que le tiers soit resté actif entre-temps.
Le service double CHAQUE fois le trigger par une erreur métier claire, AVANT d'écrire ; le
trigger reste le dernier rempart contre une écriture qui contournerait le service.

DÉCISION UNIQUE ET DÉFINITIVE : comme une pièce comptable validée, une demande décidée ne se
redécide pas — corriger, c'est créer une nouvelle demande, pas réécrire l'ancienne.
"""

import uuid
from typing import Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.credit import numerotation
from app.modules.credit.models import Application, Product

RESSOURCE = "credit.application"


class CreditError(Exception):
    """Base des refus propres au module Crédit."""


class TierNonActifError(CreditError):
    """Le tiers n'est pas actif (gate KYC) — à la création ou à l'approbation."""


class ProduitIntrouvableError(CreditError):
    """Produit de crédit inexistant ou inactif."""


class DemandeDejaDecideeError(CreditError):
    """Cette demande a déjà été décidée : pas de redécision."""


class MontantDecideInvalideError(CreditError):
    """Montant décidé absent (si approuvé), non positif, ou supérieur au montant demandé."""


def _statut_tier(db: Session, tier_id: uuid.UUID) -> str | None:
    return db.execute(
        text("SELECT status FROM tiers.tiers WHERE id = :id AND deleted_at IS NULL"),
        {"id": tier_id},
    ).scalar_one_or_none()


def creer_demande(
    db: Session,
    *,
    tier_id: uuid.UUID,
    agency_id: uuid.UUID,
    product_id: uuid.UUID,
    montant_demande: int,
    duree_echeances: int,
    objet: str | None,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> Application:
    """Crée une demande pour un tiers ACTIF (gate KYC). Refuse si le produit n'existe pas ou
    est inactif. Numéro alloué atomiquement."""
    statut = _statut_tier(db, tier_id)
    if statut != "actif":
        raise TierNonActifError(
            "Ce tiers n'est pas actif : seul un tiers dont le KYC est validé peut demander "
            "un crédit."
        )

    produit = db.get(Product, product_id)
    if produit is None or not produit.is_active:
        raise ProduitIntrouvableError("Produit de crédit inexistant ou indisponible.")

    demande = Application(
        application_number=numerotation.prochain_numero(db),
        tier_id=tier_id,
        agency_id=agency_id,
        product_id=product_id,
        montant_demande=montant_demande,
        duree_echeances=duree_echeances,
        objet=objet,
        status="en_instruction",
        created_by=par,
        updated_by=par,
    )
    db.add(demande)
    db.flush()

    ecrire_audit(
        db,
        action="credit.demande.creee",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=demande.id,
        agency_id=agency_id,
        new_values={
            "application_number": demande.application_number,
            "tier_id": str(tier_id),
            "product_id": str(product_id),
            "montant_demande": montant_demande,
            "duree_echeances": duree_echeances,
        },
    )
    return demande


def decider(
    db: Session,
    demande: Application,
    *,
    decision: Literal["approuve", "refuse"],
    montant_decide: int | None,
    motif: str,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> Application:
    """Décide une demande — MOTIF obligatoire dans LES DEUX sens, tracé. Refuse toute
    redécision (demande déjà tranchée). Refuser n'a AUCUN garde-fou KYC (toujours permis, même
    pour un tiers devenu non-actif — clôturer un dossier périmé ne crée aucun risque).
    Approuver revérifie le tiers AVANT d'écrire (le trigger 0032 double ce contrôle)."""
    if demande.status != "en_instruction":
        raise DemandeDejaDecideeError(
            f"Cette demande ({demande.application_number}) a déjà été décidée "
            f"({demande.status}) : créez une nouvelle demande pour corriger."
        )

    if decision == "approuve":
        statut = _statut_tier(db, demande.tier_id)
        if statut != "actif":
            raise TierNonActifError(
                "Ce tiers n'est plus actif : cette demande ne peut pas être approuvée en l'état."
            )
        if montant_decide is None or montant_decide <= 0 or montant_decide > demande.montant_demande:
            raise MontantDecideInvalideError(
                "Le montant décidé est obligatoire pour une approbation, positif, et ne peut "
                "pas dépasser le montant demandé."
            )

    avant = {"status": demande.status}
    demande.status = decision
    demande.montant_decide = montant_decide if decision == "approuve" else None
    demande.decided_at = db.execute(text("SELECT NOW()")).scalar_one()
    demande.decided_by = par
    demande.motif_decision = motif
    demande.updated_by = par
    db.flush()

    ecrire_audit(
        db,
        action="credit.demande.decidee",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=demande.id,
        agency_id=demande.agency_id,
        old_values=avant,
        new_values={
            "status": decision,
            "montant_decide": demande.montant_decide,
            "motif": motif,
        },
    )
    return demande
