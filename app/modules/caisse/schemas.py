"""Schémas Pydantic — validation d'entrée par liste blanche (aucun champ non déclaré)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class OuvertureSession(BaseModel):
    """Fonds initial compté PHYSIQUEMENT par le caissier à l'ouverture."""

    fonds_initial: int = Field(ge=0)


class FermetureSession(BaseModel):
    """Montant compté PHYSIQUEMENT par le caissier à la fermeture — comparé au solde théorique
    calculé par le serveur, jamais saisi par le client."""

    montant_reel: int = Field(ge=0)


class SessionCaisse(BaseModel):
    """Une session — `compte_caisse_number` résolu en clair, jamais un UUID nu à l'écran.

    `solde_theorique_actuel` : calculé EN DIRECT (même fonction que la fermeture, sans figer)
    tant que la session est OUVERTE — None une fois FERMÉE (voir `solde_theorique_cloture`,
    qui EST la valeur figée à ce moment-là ; pas la peine de répéter le même nombre deux fois
    sous deux noms)."""

    id: uuid.UUID
    agency_id: uuid.UUID
    caissier_id: uuid.UUID
    compte_caisse_number: str
    fonds_initial: int
    opened_at: datetime
    closed_at: datetime | None
    solde_theorique_actuel: int | None
    montant_reel_cloture: int | None
    solde_theorique_cloture: int | None
    ecart: int | None
    status: str
