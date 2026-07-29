"""Pont Parts sociales -> comptabilité : traduire une opération de parts en pièce équilibrée.

Résout les rôles du modèle d'écriture (même mécanisme que l'Épargne) :
  - CAISSE            -> compte de caisse de l'AGENCE (agency.compte_caisse_id, 5721) ;
  - PARTS_LIBEREES    -> compte des parts libérées (config, 1021) ;
  - PARTS_NON_LIBEREES-> compte des parts souscrites non libérées (config, 1022).
Si un rattachement manque (provisoire non renseigné), on REFUSE proprement — rien n'est écrit.

Ne touche NI au solde de parts NI au registre : ce module pose SEULEMENT la pièce. Le service
(souscrire/libérer) l'appelle avec le mouvement et la mise à jour du cache, dans UNE transaction.
"""

import uuid

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete
from app.modules.comptabilite.models import JournalEntry
from app.modules.comptabilite.schemas_ecriture import ResolveurRole, poser_depuis_schema
from app.modules.parameters.models import Agency

TYPE_SOUSCRIPTION = "parts.souscription"
TYPE_LIBERATION = "parts.liberation"
TYPE_SOUSCRIPTION_COMPTANT = "parts.souscription_comptant"
TYPE_REMBOURSEMENT = "parts.remboursement"


class RattachementPartsManquantError(Exception):
    """Un rôle ne se résout pas : compte de caisse ou de parts non rattaché. Refus propre."""


def _resolveur(
    db: Session,
    *,
    agency_id: uuid.UUID,
    compte_liberees_id: uuid.UUID | None,
    compte_non_liberees_id: uuid.UUID | None,
) -> ResolveurRole:
    compte_caisse = db.execute(
        select(Agency.compte_caisse_id).where(Agency.id == agency_id)
    ).scalar_one()

    def resoudre(role: str) -> uuid.UUID:
        if role == "CAISSE":
            if compte_caisse is None:
                raise RattachementPartsManquantError(
                    "l'agence de cette opération n'a pas de compte de caisse rattaché"
                )
            return compte_caisse
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
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> JournalEntry:
    """Pose la pièce équilibrée d'une opération de parts (date du jour). Ne touche ni cache ni
    registre : l'appelant s'en charge, dans la même transaction."""
    jour = db.execute(text("SELECT CURRENT_DATE")).scalar_one()
    return poser_depuis_schema(
        db,
        code=code_operation,
        montant=montant,
        resoudre_role=_resolveur(
            db,
            agency_id=agency_id,
            compte_liberees_id=compte_liberees_id,
            compte_non_liberees_id=compte_non_liberees_id,
        ),
        entry_date=jour,
        par=par,
        description=libelle,
        contexte=contexte,
    )
