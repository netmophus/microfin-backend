"""Contrats d'entrée/sortie de l'API Crédit (CR1 : demande et décision).

La SORTIE est construite champ par champ dans le router (aucun from_attributes). Montants en
ENTIERS de francs CFA. `tier_number`/`product_code` résolus (jamais l'UUID brut à l'écran).
"""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class CreationDemande(BaseModel):
    product_id: uuid.UUID
    montant_demande: int = Field(gt=0)
    duree_echeances: int = Field(gt=0)
    objet: str | None = Field(default=None, max_length=1000)


class Decision(BaseModel):
    decision: Literal["approuve", "refuse"]
    montant_decide: int | None = Field(default=None, gt=0)
    motif: str = Field(min_length=3, max_length=500)


class DemandeResume(BaseModel):
    id: uuid.UUID
    application_number: str
    tier_number: str
    tier_nom: str
    product_code: str
    product_name: str
    montant_demande: int
    duree_echeances: int
    status: str  # 'en_instruction' | 'approuve' | 'refuse'
    created_at: datetime


class DemandeDetail(DemandeResume):
    objet: str | None
    montant_decide: int | None
    decided_at: datetime | None
    motif_decision: str | None


class DemandeDecaissee(DemandeDetail):
    disbursed_at: datetime | None
    compte_credit_number: str | None
    nb_echeances: int
    premiere_echeance_le: date | None
    derniere_echeance_le: date | None


class EcheanceLigne(BaseModel):
    numero: int
    due_date: date
    capital: int
    interets: int
    total: int
    capital_restant_du: int
    status: str


class Remboursement(BaseModel):
    montant: int = Field(gt=0)


class RemboursementRecu(BaseModel):
    numero: int
    due_date: date
    capital: int
    interets: int
    montant_total: int
    paid_at: datetime
    echeances_restantes: int
