"""Contrats d'entrée/sortie de l'API Épargne (F1 : consultation + ouverture).

La SORTIE est construite champ par champ dans le router (aucun from_attributes) : ce qui n'est
pas écrit explicitement ne sort pas. Les montants sont des ENTIERS de francs CFA (jamais de
flottant). `is_provisional` remonte le caractère provisoire du produit (rattachement/taux non
encore validés) pour l'afficher à l'écran.
"""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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


class ProduitEpargneDetail(BaseModel):
    """Réponse des endpoints de gestion du référentiel (création/modification/activation) —
    le produit complet, motif exclu (déjà dans l'audit)."""

    id: uuid.UUID
    code: str
    name: str
    type: str
    currency: str
    is_active: bool
    is_provisional: bool
    taux_bp: int
    periodicite: str
    methode_calcul_solde: str
    base_jours: int
    regle_arrondi: str
    solde_minimum_remunere: int


class CreationProduitEpargne(BaseModel):
    """Valeurs par défaut = celles de la migration 0018/0023 (taux 0, calcul en fin de période,
    base 360, arrondi au plus proche) : un produit tout juste créé ne verse aucun intérêt tant
    qu'il n'a pas été réglé explicitement. Ne reçoit AUCUN compte comptable — ça reste le rôle
    de l'écran de rattachement (`compta.plan.manage`), après création.

    `decouvert_autorise` N'EST PAS un champ de ce schéma, à dessein : un SFD ne tient pas de
    comptes courants, l'épargne n'autorise jamais de découvert. `extra="forbid"` REJETTE (422)
    toute tentative de le faire passer dans le corps, plutôt que de l'ignorer en silence — la
    colonne reste à sa valeur par défaut (0) en base, jamais touchée par cet endpoint."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=150)
    type: Literal["a_vue", "terme", "programmee"] = "a_vue"
    currency: str = Field(default="XOF", min_length=3, max_length=3)
    taux_bp: int = Field(default=0, ge=0, le=10000)
    periodicite: Literal["mensuelle", "trimestrielle", "annuelle"] = "annuelle"
    methode_calcul_solde: Literal["min_periode", "moyen_quotidien", "fin_periode"] = "fin_periode"
    base_jours: Literal[360, 365] = 360
    regle_arrondi: Literal["plus_proche", "plancher"] = "plus_proche"
    solde_minimum_remunere: int = Field(default=0, ge=0)


class ModificationProduitEpargne(BaseModel):
    """État complet soumis à chaque enregistrement — pas un PATCH partiel. SEUL chemin
    d'écriture du taux et des paramètres de calcul (l'ancien endpoint comptable
    `parametres-interet` a été retiré : deux chemins sur les mêmes champs créaient une
    collision silencieuse). Ne touche pas aux comptes rattachés ni à `is_active`/
    `is_provisional`. `decouvert_autorise` absent à dessein — voir `CreationProduitEpargne`,
    même garde-fou `extra="forbid"`."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=150)
    type: Literal["a_vue", "terme", "programmee"]
    taux_bp: int = Field(ge=0, le=10000)
    periodicite: Literal["mensuelle", "trimestrielle", "annuelle"]
    methode_calcul_solde: Literal["min_periode", "moyen_quotidien", "fin_periode"]
    base_jours: Literal[360, 365]
    regle_arrondi: Literal["plus_proche", "plancher"]
    solde_minimum_remunere: int = Field(ge=0)
    motif: str = Field(min_length=3, max_length=500)


class ActivationProduit(BaseModel):
    """Motif obligatoire dans les deux sens (activer comme désactiver) — même discipline que
    `caisse.poste`."""

    is_active: bool
    motif: str = Field(min_length=3, max_length=500)


class ValidationProduitResultat(ProduitEpargneDetail):
    """`avertissements` : messages non bloquants (ex. compte client non rattaché) — structurés
    pour être exploités par le frontend, jamais noyés dans un texte libre."""

    avertissements: list[str] = Field(default_factory=list)


class LigneRapprochement(BaseModel):
    """Une ligne de la vue de contrôle : un compte collectif, ses deux côtés, l'écart."""

    compte_general: str  # numéro du plan (3111, 3121…)
    auxiliaire: int  # Σ des soldes d'épargne rattachés
    general: int  # solde comptable du compte
    concordant: bool
    ecart: int  # auxiliaire moins general (0 si concordant)
