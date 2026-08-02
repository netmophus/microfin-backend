"""Contrats d'entrée/sortie de l'API Épargne (F1 : consultation + ouverture).

La SORTIE est construite champ par champ dans le router (aucun from_attributes) : ce qui n'est
pas écrit explicitement ne sort pas. Les montants sont des ENTIERS de francs CFA (jamais de
flottant). `is_provisional` remonte le caractère provisoire du produit (rattachement/taux non
encore validés) pour l'afficher à l'écran.
"""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field


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


class CompteGuichet(BaseModel):
    """Ce que le caissier voit après recherche par numéro. Le NOM du membre est proéminent :
    c'est la vérification humaine contre une faute de frappe dans le numéro."""

    id: uuid.UUID
    account_number: str
    tier_id: uuid.UUID
    membre_nom: str
    product_name: str
    product_type: str
    currency: str
    balance: int
    status: str
    is_provisional: bool


class OperationGuichet(BaseModel):
    montant: int  # francs CFA entiers ; le service refuse <= 0 (message clair)


class ResultatOperation(BaseModel):
    account_number: str
    nouveau_solde: int
    entry_number: str | None


# --- Intérêts (F4) : prévisualisation obligatoire, puis versement -------------------


class DemandeInterets(BaseModel):
    """Corps d'une prévisualisation ou d'un versement d'intérêts : la période et ses bornes.

    `periode` est le libellé qui verrouille l'anti-double-versement (ex. « 2026-S1 ») : deux
    versements sur la même période, même clé -> refusés en base. `debut`/`fin` bornent le calcul.
    """

    periode: str
    debut: date
    fin: date


class ApercuLigneInterets(BaseModel):
    """Un compte de l'échantillon : de quoi vérifier « ça a l'air juste » avant de lancer."""

    account_number: str
    produit: str
    taux_bp: int  # points de base (350 = 3,5 %) ; l'écran l'affiche en %
    methode: str  # min_periode | moyen_quotidien | fin_periode
    base_solde: int
    montant: int


class ApercuInterets(BaseModel):
    """Prévisualisation d'un versement : le TOTAL et le DÉTAIL, rien n'est encore versé."""

    periode: str
    debut: date
    fin: date
    jours: int
    comptes_actifs: int  # comptes actifs examinés (diagnostic de l'écran : « aucun compte »)
    comptes_taux_zero: int  # parmi eux, produits à taux 0 (barème non fixé)
    comptes_a_crediter: int
    total: int  # total à verser, francs CFA entiers
    deja_traites: int  # comptes déjà versés pour cette période (anti-double)
    deja_verse_le: datetime | None  # quand, si la période a déjà été (au moins en partie) versée
    echantillon: list[ApercuLigneInterets]


class RapportInterets(BaseModel):
    """Résultat d'un versement effectif."""

    traites: int
    credites: int
    ignores: int
    total: int


# --- Rattachements comptables (Bloc 5 du paramétrage comptable) --------------------------


class CompteRattachement(BaseModel):
    """Un compte résolu — numéro + libellé, jamais l'UUID (règle du projet)."""

    account_number: str
    name: str


class RattachementsProduit(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    compte_epargne: CompteRattachement | None
    compte_epargne_client: CompteRattachement | None
    compte_charge_interet: CompteRattachement | None


class ModificationRattachementsProduit(BaseModel):
    """Les 3 rattachements TOUJOURS fournis ensemble — l'écran soumet l'état complet de ses 3
    sélecteurs à chaque enregistrement, pas un PATCH partiel comme la fiche du plan de comptes."""

    compte_epargne: str | None
    compte_epargne_client: str | None
    compte_charge_interet: str | None
    motif: str = Field(min_length=3, max_length=500)


class LigneRapprochement(BaseModel):
    """Une ligne de la vue de contrôle : un compte collectif, ses deux côtés, l'écart."""

    compte_general: str  # numéro du plan (3111, 3121…)
    auxiliaire: int  # Σ des soldes d'épargne rattachés
    general: int  # solde comptable du compte
    concordant: bool
    ecart: int  # auxiliaire moins general (0 si concordant)
