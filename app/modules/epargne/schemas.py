"""Contrats d'entrée/sortie de l'API Épargne (F1 : consultation + ouverture).

La SORTIE est construite champ par champ dans le router (aucun from_attributes) : ce qui n'est
pas écrit explicitement ne sort pas. Les montants sont des ENTIERS de francs CFA (jamais de
flottant). `is_provisional` remonte le caractère provisoire du produit (rattachement/taux non
encore validés) pour l'afficher à l'écran.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel


class ProduitEpargne(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    type: str
    is_provisional: bool


class CompteEpargneResume(BaseModel):
    id: uuid.UUID
    account_number: str
    product_code: str
    product_name: str
    product_type: str
    currency: str
    balance: int  # francs CFA entiers
    status: str  # 'actif' | 'cloture'
    is_provisional: bool  # du produit


class MouvementResume(BaseModel):
    id: uuid.UUID
    sens: str  # 'credit' | 'debit'
    amount: int
    balance_after: int
    operation_type: str  # depot, retrait, interet, cloture
    label: str | None
    created_at: datetime
    entry_number: str | None  # n° de la pièce comptable liée


class CompteEpargneDetail(CompteEpargneResume):
    opened_at: datetime
    closed_at: datetime | None
    mouvements: list[MouvementResume]


class OuvertureCompte(BaseModel):
    product_id: uuid.UUID
