"""Contrats d'entrée/sortie de l'API Crédit (CR1 : demande et décision).

La SORTIE est construite champ par champ dans le router (aucun from_attributes). Montants en
ENTIERS de francs CFA. `tier_number`/`product_code` résolus (jamais l'UUID brut à l'écran).
"""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# --- Gestion du référentiel produit (création, modification, validation, activation) ------
# Distinct des rattachements comptables / paramètres d'intérêt (à venir, lot 3b) : ici, le
# CYCLE DE VIE du produit lui-même — credit.product.manage (ADMIN_FONCTIONNEL). Même patron
# que epargne/schemas.py (CreationProduitEpargne, etc.), adapté : pas de `currency` (XOF
# implicite, jamais paramétré côté crédit), `methode_amortissement` remplace
# `methode_calcul_solde`, `taux_usure_max_bp` en plus (plafond paramétrable, migration 0050).


class ProduitCreditDetail(BaseModel):
    """Réponse des endpoints de gestion (création/modification/activation) — le produit
    complet, motif exclu (déjà dans l'audit). Champs non-Literal (contrairement aux schémas
    d'entrée ci-dessous) : c'est de la SORTIE, construite à partir de l'ORM (`Mapped[str]`),
    même choix que `epargne.schemas.ProduitEpargneDetail`."""

    id: uuid.UUID
    code: str
    name: str
    is_active: bool
    is_provisional: bool
    taux_bp: int
    periodicite: str
    methode_amortissement: str
    base_jours: int
    regle_arrondi: str
    taux_usure_max_bp: int | None


class CreationProduitCredit(BaseModel):
    """Valeurs par défaut = celles de la migration 0031 (taux 0, mensuelle, échéance
    constante, arrondi au plus proche) : un produit tout juste créé ne porte aucun intérêt
    tant qu'il n'a pas été réglé explicitement. Ne reçoit AUCUN compte comptable — ça reste le
    rôle de l'écran de rattachement (`compta.plan.manage`), après création.

    `base_jours` N'EST PAS un champ de ce schéma, à dessein — voir `ModificationProduitCredit`.
    La colonne reste à 360 (défaut base, migration 0031) en base, jamais touchée par cet
    endpoint."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=150)
    taux_bp: int = Field(default=0, ge=0, le=10000)
    periodicite: Literal["mensuelle", "trimestrielle", "annuelle"] = "mensuelle"
    methode_amortissement: Literal["capital_constant", "echeance_constante"] = "echeance_constante"
    regle_arrondi: Literal["plus_proche", "plancher"] = "plus_proche"
    taux_usure_max_bp: int | None = Field(default=None, ge=0, le=10000)


class ModificationProduitCredit(BaseModel):
    """État complet soumis à chaque enregistrement — pas un PATCH partiel. Ne touche pas aux
    comptes rattachés ni à `is_active`/`is_provisional`.

    `base_jours` N'EST PAS un champ de ce schéma, à dessein : le calcul de l'échéancier est
    PÉRIODIQUE (taux annuel / nb périodes), base 360 implicite (norme UEMOA) — `base_jours` n'a
    aucun sens dans ce mode et reste GELÉ à sa valeur par défaut en base (voir
    `echeancier.py` et `CreationProduitCredit`). `extra="forbid"` REJETTE (422) toute tentative
    de le faire passer dans le corps, plutôt que de l'ignorer en silence."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=150)
    taux_bp: int = Field(ge=0, le=10000)
    periodicite: Literal["mensuelle", "trimestrielle", "annuelle"]
    methode_amortissement: Literal["capital_constant", "echeance_constante"]
    regle_arrondi: Literal["plus_proche", "plancher"]
    taux_usure_max_bp: int | None = Field(default=None, ge=0, le=10000)
    motif: str = Field(min_length=3, max_length=500)


class ActivationProduit(BaseModel):
    """Motif obligatoire dans les deux sens (activer comme désactiver) — même discipline que
    `caisse.poste`/`epargne.product`."""

    is_active: bool
    motif: str = Field(min_length=3, max_length=500)


class ValidationProduitResultat(ProduitCreditDetail):
    """`avertissements` : messages non bloquants (ex. compte client non rattaché) —
    structurés pour être exploités par le frontend, jamais noyés dans un texte libre."""

    avertissements: list[str] = Field(default_factory=list)


# --- Rattachements comptables (lot 3b, miroir de epargne.schemas) -------------------------
# Distinct de CompteRattachementPalier (paliers de souffrance, même forme mais concern
# différent) : gardé compta.plan.manage, comme le bloc épargne équivalent.


class CompteRattachement(BaseModel):
    """Un compte résolu — numéro + libellé, jamais l'UUID (règle du projet)."""

    account_number: str
    name: str


class RattachementsProduitCredit(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    compte_credit_membre: CompteRattachement | None
    compte_credit_client: CompteRattachement | None
    compte_produits_interets: CompteRattachement | None


class ModificationRattachementsProduitCredit(BaseModel):
    """Les 3 rattachements TOUJOURS fournis ensemble — l'écran soumet l'état complet de ses 3
    sélecteurs à chaque enregistrement, même discipline que l'épargne."""

    compte_credit_membre: str | None
    compte_credit_client: str | None
    compte_produits_interets: str | None
    motif: str = Field(min_length=3, max_length=500)


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


class ApercuSoldeAnticipe(BaseModel):
    """Ce que coûterait un solde anticipé à la date `date_reference_intérêts` — calcul PUR, rien
    n'est encore posé. Mêmes montants que `SoldeAnticipeRecu` SI l'action est déclenchée le même
    jour (voir remboursement.apercevoir_solde_anticipe)."""

    capital_restant: int
    interets_courus: int
    montant_total: int
    date_reference_interets: date
    jours_courus: int


class SoldeAnticipeRecu(BaseModel):
    """La clôture anticipée réellement posée — statut basculé, pièce comptable créée."""

    capital_regle: int
    interets_courus: int
    montant_total: int
    jours_courus: int
    solde_at: datetime
    status: str
    entry_number: str


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


class LigneReclassement(BaseModel):
    """UN dossier réellement reclassé — palier avant/après en clair, pas un UUID nu."""

    application_number: str
    tier_avant_code: str | None
    tier_avant_libelle: str | None
    tier_apres_code: str | None
    tier_apres_libelle: str | None
    jours_retard: int
    encours_actuel: int
    provision_avant: int
    provision_apres: int


class RapportReclassement(BaseModel):
    """Résultat d'une exécution du job de reclassification (CR5c)."""

    dossiers_evalues: int
    reclasses: int
    ignores_rattachement_manquant: list[str]
    lignes: list[LigneReclassement]


class LigneApercuReclassement(BaseModel):
    """UN dossier qui SERAIT reclassé (aperçu, dry-run) — même forme que LigneReclassement,
    plus le motif de refus s'il y en aurait un."""

    application_number: str
    tier_avant_code: str | None
    tier_avant_libelle: str | None
    tier_apres_code: str | None
    tier_apres_libelle: str | None
    jours_retard: int
    encours_actuel: int
    provision_avant: int
    provision_apres: int
    rattachement_manquant: str | None


class ApercuReclassement(BaseModel):
    """Ce que la reclassification FERAIT — aucune écriture (CR5c, dry-run)."""

    dossiers_evalues: int
    a_reclasser: int
    rattachements_manquants: int
    lignes: list[LigneApercuReclassement]
