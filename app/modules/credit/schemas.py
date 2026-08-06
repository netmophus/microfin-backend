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
    """CR5b : `montant_paye`/`solde_du` reflètent un versement partiel éventuel — `status` seul
    ('partiellement_paye') ne suffit pas à afficher ce qui reste réellement dû."""

    numero: int
    due_date: date
    capital: int
    interets: int
    total: int
    capital_restant_du: int
    status: str
    montant_paye: int
    solde_du: int


class EcheanceApercuLigne(BaseModel):
    """Une échéance d'APERÇU (CR6b) — mêmes montants qu'une échéance réelle, sans `status` :
    rien n'est suivi puisque rien n'est écrit en base."""

    numero: int
    due_date: date
    capital: int
    interets: int
    total: int
    capital_restant_du: int


class EcheanceDue(BaseModel):
    """CR5b : `solde_du` (pas `total`) est le montant à présenter/encaisser au guichet — une
    échéance déjà partiellement payée (`montant_paye` > 0) ne doit jamais faire réapparaître
    son montant d'origine comme s'il restait intégralement dû."""

    numero: int
    due_date: date
    capital: int
    interets: int
    total: int
    montant_paye: int
    solde_du: int


class DossierRemboursable(BaseModel):
    """Un résultat de recherche du guichet (CR6d). `prochaine_echeance` absente (None) = ce
    crédit est déjà entièrement soldé — affiché tel quel, jamais un résultat qui échouerait
    au clic."""

    id: uuid.UUID
    application_number: str
    tier_number: str
    tier_nom: str
    product_name: str
    prochaine_echeance: EcheanceDue | None


class Remboursement(BaseModel):
    montant: int = Field(gt=0)


class RemboursementRecu(BaseModel):
    """CE versement (CR5b) — `capital`/`interets`/`montant_total` décrivent ce que CE paiement
    a couvert, pas nécessairement l'échéance entière si elle n'est que partiellement soldée
    (`echeance_soldee=False`, `solde_du` > 0 : il reste un reliquat sur CETTE échéance)."""

    numero: int
    due_date: date
    capital: int
    interets: int
    montant_total: int
    paid_at: datetime
    solde_du: int
    echeance_soldee: bool
    echeances_restantes: int


class CompteRattachementPalier(BaseModel):
    """Un compte résolu — numéro + libellé, jamais l'UUID (règle du projet)."""

    account_number: str
    name: str


class PalierSouffrance(BaseModel):
    """Un palier de souffrance (CR5a ; `compte_provision`/`compte_reprise` ajoutés en CR5c).
    Comptes absents (None) = non rattaché — provisoire, à compléter via l'écran."""

    id: uuid.UUID
    code: str
    libelle: str
    seuil_jours: int
    taux_provision_bp: int
    compte_encours: CompteRattachementPalier | None
    compte_dotation: CompteRattachementPalier | None
    compte_provision: CompteRattachementPalier | None
    compte_reprise: CompteRattachementPalier | None
    is_terminal: bool
    is_provisional: bool


class CreationPalier(BaseModel):
    code: str = Field(min_length=1, max_length=20)
    libelle: str = Field(min_length=1, max_length=150)
    seuil_jours: int = Field(ge=0)
    taux_provision_bp: int = Field(ge=0, le=10000)
    compte_encours: str | None = None
    compte_dotation: str | None = None
    compte_provision: str | None = None
    compte_reprise: str | None = None
    is_terminal: bool = False
    motif: str = Field(min_length=3, max_length=500)


class ModificationPalier(BaseModel):
    """L'écran soumet l'état COMPLET du palier à chaque enregistrement — pas un PATCH partiel,
    même discipline que les rattachements produit d'épargne."""

    code: str = Field(min_length=1, max_length=20)
    libelle: str = Field(min_length=1, max_length=150)
    seuil_jours: int = Field(ge=0)
    taux_provision_bp: int = Field(ge=0, le=10000)
    compte_encours: str | None = None
    compte_dotation: str | None = None
    compte_provision: str | None = None
    compte_reprise: str | None = None
    is_terminal: bool = False
    motif: str = Field(min_length=3, max_length=500)


class SuppressionPalier(BaseModel):
    motif: str = Field(min_length=3, max_length=500)


class RapportReclassement(BaseModel):
    """Résultat d'une exécution du job de reclassification (CR5c)."""

    dossiers_evalues: int
    reclasses: int
    ignores_rattachement_manquant: list[str]
