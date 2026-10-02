"""Pont Parts sociales -> comptabilité : traduire une opération de parts en pièce équilibrée.

Résout les rôles du modèle d'écriture (même mécanisme que l'Épargne) :
  - CAISSE            -> compte de caisse de l'AGENCE (agency.compte_caisse_id, 5721) PAR
    DÉFAUT, sauf `compte_caisse_id` fourni par l'appelant (Bloc C3 — souscription au comptant :
    le compte ANCRÉ de la session de caisse ouverte du caissier, jamais recalculé) ;
  - PARTS_LIBEREES    -> compte des parts libérées (config, 1021) ;
  - PARTS_NON_LIBEREES-> compte des parts souscrites non libérées (config, 1022).
Si un rattachement manque (provisoire non renseigné), on REFUSE proprement — rien n'est écrit.

Ne touche NI au solde de parts NI au registre : ce module pose SEULEMENT la pièce. Le service
(souscrire/libérer) l'appelle avec le mouvement et la mise à jour du cache, dans UNE transaction.
"""

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete
from app.modules.comptabilite.journee import date_comptable_obligatoire
from app.modules.comptabilite.models import JournalEntry
from app.modules.comptabilite.schemas_ecriture import ResolveurRole, poser_depuis_schema
from app.modules.parameters.models import Agency

TYPE_SOUSCRIPTION = "parts.souscription"
TYPE_LIBERATION = "parts.liberation"
TYPE_SOUSCRIPTION_COMPTANT = "parts.souscription_comptant"
TYPE_REMBOURSEMENT = "parts.remboursement"
TYPE_ANNULATION = "parts.annulation"


class RattachementPartsManquantError(Exception):
    """Un rôle ne se résout pas : compte de caisse ou de parts non rattaché. Refus propre."""


def _resolveur(
    db: Session,
    *,
    agency_id: uuid.UUID,
    compte_liberees_id: uuid.UUID | None,
    compte_non_liberees_id: uuid.UUID | None,
    compte_caisse_id: uuid.UUID | None = None,
) -> ResolveurRole:
    def resoudre(role: str) -> uuid.UUID:
        if role == "CAISSE":
            # ANCRÉ (Bloc C3, souscription au comptant) prime sur l'agence : l'appelant a déjà
            # résolu la session de caisse ouverte du caissier. Sans override, comportement
            # inchangé pour les autres opérations (libération, remboursement, annulation).
            compte = compte_caisse_id
            if compte is None:
                compte = db.execute(
                    select(Agency.compte_caisse_id).where(Agency.id == agency_id)
                ).scalar_one()
            if compte is None:
                raise RattachementPartsManquantError(
                    "l'agence de cette opération n'a pas de compte de caisse rattaché"
                )
            return compte
        if role == "PARTS_LIBEREES":
            if compte_liberees_id is None:
                raise RattachementPartsManquantError(
                    "le compte des parts libérées (1021) n'est pas rattaché (paramétrage)"
                )
            return compte_liberees_id
        if role == "PARTS_NON_LIBEREES":
            if compte_non_liberees_id is None:
                raise RattachementPartsManquantError(
                    "le compte des parts non libérées (1022) n'est pas rattaché (paramétrage)"
                )
            return compte_non_liberees_id
        raise RattachementPartsManquantError(f"rôle « {role} » inconnu dans le modèle d'écriture")

    return resoudre


def poser_ecriture_parts(
    db: Session,
    code_operation: str,
    montant: int,
    par: uuid.UUID | None,
    *,
    agency_id: uuid.UUID,
    compte_liberees_id: uuid.UUID | None,
    compte_non_liberees_id: uuid.UUID | None,
    libelle: str,
    compte_caisse_id: uuid.UUID | None = None,
    entry_date: date | None = None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> JournalEntry:
    """Pose la pièce équilibrée d'une opération de parts. Ne touche ni cache ni registre :
    l'appelant s'en charge, dans la même transaction.

    `compte_caisse_id` : ANCRÉ, fourni par l'appelant (Bloc C3 — souscription au comptant,
    voir en-tête de module) ; `None` pour toute autre opération, comportement inchangé.

    `entry_date` (chantier P1bis, lot 3) : date de la pièce, par défaut la journée comptable
    ouverte — même patron que `epargne.operations.poser_ecriture_operation`. Paramètre AJOUTÉ
    dans ce lot (absent jusqu'ici) ; son unique appelant (`tiers.parts.*`) ne le fournit pas
    encore, il reçoit donc la date de la journée par défaut, pas de changement de comportement
    en semaine (journée = date système)."""
    jour = entry_date
    if jour is None:
        jour = date_comptable_obligatoire(db)
    return poser_depuis_schema(
        db,
        code=code_operation,
        montant=montant,
        resoudre_role=_resolveur(
            db,
            agency_id=agency_id,
            compte_liberees_id=compte_liberees_id,
            compte_non_liberees_id=compte_non_liberees_id,
            compte_caisse_id=compte_caisse_id,
        ),
        entry_date=jour,
        par=par,
        description=libelle,
        contexte=contexte,
    )
