"""Pont Parts sociales -> comptabilité : traduire une opération de parts en pièce équilibrée.

Résout les rôles du modèle d'écriture (même mécanisme que l'Épargne) :
  - CAISSE            -> `compte_caisse_id` fourni par l'appelant, OBLIGATOIRE pour toute opération
    d'espèces (souscription au comptant, libération, remboursement) : le compte ANCRÉ de la
    session de caisse ouverte de l'acteur, jamais recalculé, JAMAIS celui de l'agence. Sans lui,
    refus explicite (RattachementPartsManquantError) : aucun repli silencieux sur le compte de
    l'agence, qui ferait bouger une caisse que le tiroir de l'acteur ne reflète pas ;
  - PARTS_LIBEREES    -> compte des parts libérées (config, 1021) ;
  - PARTS_NON_LIBEREES-> compte des parts souscrites non libérées (config, 1022).
Si un rattachement manque (provisoire non renseigné), on REFUSE proprement — rien n'est écrit.

Ne touche NI au solde de parts NI au registre : ce module pose SEULEMENT la pièce. Le service
(souscrire/libérer) l'appelle avec le mouvement et la mise à jour du cache, dans UNE transaction.
"""

import uuid
from datetime import date

from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete
from app.modules.comptabilite.journee import date_comptable_obligatoire
from app.modules.comptabilite.models import JournalEntry
from app.modules.comptabilite.schemas_ecriture import ResolveurRole, poser_depuis_schema

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
    compte_liberees_id: uuid.UUID | None,
    compte_non_liberees_id: uuid.UUID | None,
    compte_caisse_id: uuid.UUID | None = None,
) -> ResolveurRole:
    def resoudre(role: str) -> uuid.UUID:
        if role == "CAISSE":
            # Le rôle n'est demandé que par les opérations d'espèces : l'appelant a dû résoudre
            # la session de caisse ouverte de l'acteur. Pas de repli sur l'agence (voir en-tête).
            if compte_caisse_id is None:
                raise RattachementPartsManquantError(
                    "opération d'espèces sans compte de caisse ancré : elle doit passer par la "
                    "session de caisse ouverte de l'acteur"
                )
            return compte_caisse_id
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

    `compte_caisse_id` : ANCRÉ, fourni par l'appelant pour toute opération d'espèces (voir
    en-tête de module) ; `None` pour les opérations sans espèces (souscription différée,
    annulation). Une opération d'espèces sans lui est refusée.

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
            compte_liberees_id=compte_liberees_id,
            compte_non_liberees_id=compte_non_liberees_id,
            compte_caisse_id=compte_caisse_id,
        ),
        entry_date=jour,
        par=par,
        description=libelle,
        contexte=contexte,
    )
