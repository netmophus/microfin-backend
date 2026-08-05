"""Contrats d'entrée/sortie de l'API Crédit (CR1 : demande et décision).

La SORTIE est construite champ par champ dans le router (aucun from_attributes). Montants en
ENTIERS de francs CFA. `tier_number`/`product_code` résolus (jamais l'UUID brut à l'écran).
"""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


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
    tier_id: uuid.UUID  # pour lister les comptes epargne.accounts éligibles au décaissement
    tier_number: str
    tier_nom: str
    is_member: bool  # membre ou client — pour dire quel compte de crédit recevra la créance
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


class DecaissementCorps(BaseModel):
    """mode='epargne' exige compte_epargne_id (le compte epargne.accounts choisi, n'importe
    quel produit) ; mode='caisse' (défaut) ne doit PAS en porter — explicite, pas deviné."""

    mode: Literal["caisse", "epargne"] = "caisse"
    compte_epargne_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _coherence(self) -> "DecaissementCorps":
        if self.mode == "epargne" and self.compte_epargne_id is None:
            raise ValueError(
                "compte_epargne_id est obligatoire pour un décaissement sur compte."
            )
        if self.mode == "caisse" and self.compte_epargne_id is not None:
            raise ValueError(
                "compte_epargne_id ne doit pas être fourni pour un décaissement en espèces."
            )
        return self


class DemandeDecaissee(DemandeDetail):
    disbursed_at: datetime | None
    compte_credit_number: str | None
    mode_decaissement: str  # 'caisse' | 'epargne'
    # Le compte réellement crédité (C) : la caisse utilisée, ou le compte du tiers choisi.
    compte_destination_number: str | None
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


class EcheanceApercuLigne(BaseModel):
    """Une échéance d'APERÇU (CR6b) — mêmes montants qu'une échéance réelle, sans `status` :
    rien n'est suivi puisque rien n'est écrit en base."""

    numero: int
    due_date: date
    capital: int
    interets: int
    total: int
    capital_restant_du: int


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
