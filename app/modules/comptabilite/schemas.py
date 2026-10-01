"""Schémas Pydantic — Plan de comptes, Bloc 1 (consultation + gestion unitaire).

Montants et libellés en clair, aucun champ technique exposé sans traduction. `parent_number`
est RÉSOLU (le numéro du parent, pas son UUID) : un comptable lit un numéro de compte, jamais
un identifiant opaque.
"""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CompteResume(BaseModel):
    id: uuid.UUID
    account_number: str
    name: str
    short_name: str | None
    account_class: int
    parent_number: str | None
    normal_side: str
    is_posting: bool
    is_system: bool
    is_provisional: bool
    is_active: bool


class CompteDetail(CompteResume):
    notes: str | None
    created_at: datetime
    updated_at: datetime


class PageComptes(BaseModel):
    lignes: list[CompteResume]
    total: int
    page: int
    taille: int


class CreationCompte(BaseModel):
    """Création MANUELLE, à l'unité — distincte de l'import CSV (un modèle générique à
    valider). is_system n'est jamais proposé ici : un compte système ne vient QUE du plan de
    référence importé."""

    account_number: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=200)
    short_name: str | None = Field(default=None, max_length=50)
    account_class: int = Field(ge=1, le=9)
    parent_number: str | None = Field(default=None, max_length=20)
    normal_side: Literal["D", "C"]
    is_posting: bool
    notes: str | None = None


class ModificationCompte(BaseModel):
    """PATCH partiel : seuls les champs FOURNIS sont modifiés (même patron que la fiche
    utilisateur — model_fields_set distingue « absent » de « explicitement vidé »)."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    short_name: str | None = Field(default=None, max_length=50)
    notes: str | None = None

    def modifications(self) -> dict[str, object]:
        """Ne rend que les champs RÉELLEMENT fournis par le client."""
        return {champ: getattr(self, champ) for champ in self.model_fields_set}


class ChangementSens(BaseModel):
    """Motif OBLIGATOIRE : acte sensible sur le plan comptable, tracé (trace-only pour
    l'instant — le double contrôle maker-checker est un chantier séparé, à venir)."""

    normal_side: Literal["D", "C"]
    motif: str = Field(min_length=3, max_length=500)


class DesactivationCompte(BaseModel):
    motif: str = Field(min_length=3, max_length=500)


class VerrouillageSaisie(BaseModel):
    """Motif OBLIGATOIRE : ferme la saisie d'un compte (is_posting -> FALSE), jamais l'inverse."""

    motif: str = Field(min_length=3, max_length=500)


class DiffChampSchema(BaseModel):
    champ: str
    avant: str
    apres: str


class CompteApercuSchema(BaseModel):
    account_number: str
    name: str
    diffs: list[DiffChampSchema] = []


class ApercuImportComptes(BaseModel):
    """Résultat de l'aperçu (Bloc 2) : soit des anomalies (rien d'autre n'est fourni, l'import
    est bloqué), soit le diff — ce qui serait créé/modifié — accompagné d'une empreinte à
    reprendre telle quelle à la confirmation."""

    anomalies: list[str] = []
    empreinte: str | None = None
    a_creer: list[CompteApercuSchema] = []
    a_modifier: list[CompteApercuSchema] = []
    inchanges: int = 0


class ConfirmationImportComptes(BaseModel):
    crees: int
    mis_a_jour: int
    provisoire_leve: bool


class CompteSelecteur(BaseModel):
    """Un compte réduit à ce qu'un sélecteur de rattachement affiche — TOUJOURS de saisie et
    actif (voir comptes.lister_pour_selecteur)."""

    id: uuid.UUID
    account_number: str
    name: str


# --- Rapports (R1 grand livre, R2 balance) — lecture pure, aucune écriture -------------------


class CompteSelecteurRapport(CompteSelecteur):
    """Comme CompteSelecteur, + is_active : ce sélecteur propose AUSSI les comptes désactivés
    (l'historique doit rester consultable), il faut donc pouvoir les distinguer à l'écran."""

    is_active: bool


class CompteRapport(BaseModel):
    """Le compte concerné par un rapport — numéro + libellé, jamais l'UUID à l'écran.
    is_active : un grand livre peut porter sur un compte désactivé (historique consultable) —
    l'écran doit pouvoir le signaler même une fois le sélecteur refermé."""

    account_number: str
    name: str
    is_active: bool


class LigneGrandLivre(BaseModel):
    entry_date: date
    entry_number: str | None
    journal_code: str
    label: str
    side: Literal["D", "C"]
    amount: int
    solde_cumule: int


class PageGrandLivre(BaseModel):
    compte: CompteRapport
    solde_ouverture: int
    lignes: list[LigneGrandLivre]
    total: int
    page: int
    taille: int


class LigneBalance(BaseModel):
    account_number: str
    name: str
    solde_ouverture: int
    total_debit: int
    total_credit: int
    solde_cloture: int


class Balance(BaseModel):
    date_debut: date | None
    date_fin: date | None
    lignes: list[LigneBalance]
    total_debit: int
    total_credit: int
    equilibree: bool


# --- Saisie manuelle d'écriture (OD), chantier P1 lot 1 ------------------------------------
# Journal OD (Opérations diverses) UNIQUEMENT — jamais un champ de ces schémas : la restriction
# est posée côté service (ecritures_od.py), pas ici. extra="forbid" : un champ inattendu (ex.
# "journal_id" envoyé par erreur) est un 422, pas une valeur silencieusement ignorée.


class LigneSaisieOD(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_number: str = Field(min_length=1, max_length=20)
    side: Literal["D", "C"]
    amount: int = Field(gt=0)
    label: str | None = Field(default=None, max_length=300)


class CreationEcritureOD(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_date: date
    description: str = Field(min_length=1, max_length=300)
    lignes: list[LigneSaisieOD] = Field(min_length=1)


class LigneEcritureODDetail(BaseModel):
    account_number: str
    name: str
    side: Literal["D", "C"]
    amount: int
    label: str | None


class EcritureODResume(BaseModel):
    """Une ligne de la liste — sans le détail des lignes (voir EcritureODDetail). `equilibree`
    et `deja_contre_passee` évitent à l'écran de recalculer ce que le moteur sait déjà."""

    id: uuid.UUID
    entry_number: str | None
    entry_date: date
    description: str
    status: Literal["brouillon", "validee"]
    nb_lignes: int
    total_debit: int
    total_credit: int
    equilibree: bool
    est_contre_passation: bool
    deja_contre_passee: bool


class EcritureODDetail(EcritureODResume):
    lignes: list[LigneEcritureODDetail]


class PageEcrituresOD(BaseModel):
    lignes: list[EcritureODResume]
    total: int
    page: int
    taille: int


# --- Clôture d'exercice (chantier P1, lot b1) ------------------------------------------------
# Clôture TECHNIQUE uniquement : solde les comptes de charges/produits (classe 6/7) vers 591
# (« Excédent ou déficit en instance d'approbation »). L'affectation du résultat (591 -> réserves
# et/ou 58, après approbation de l'assemblée générale) est le lot b2a, ci-dessous. 592 n'est
# jamais utilisé (décision actée, voir affectation_resultat.py).


class ExerciceResume(BaseModel):
    id: uuid.UUID
    code: str
    label: str
    date_debut: date
    date_fin: date
    status: Literal["ouvert", "clos"]
    resultat_affecte: bool
    a_nouveaux_generes: bool


class LigneResultatCloture(BaseModel):
    account_number: str
    name: str
    account_class: int
    total_debit: int
    total_credit: int
    side: Literal["D", "C"]
    amount: int


class BrouillonBloquantSchema(BaseModel):
    entry_id: uuid.UUID
    journal_code: str
    entry_date: date
    description: str


class ApercuCloture(BaseModel):
    """Dry-run obligatoire avant la confirmation à l'écran — rien n'est posé ici."""

    exercice: ExerciceResume
    resultat: int
    compte_resultat: str
    lignes: list[LigneResultatCloture]
    brouillons_bloquants: list[BrouillonBloquantSchema]
    cloturable: bool


class ClotureExerciceResultat(BaseModel):
    """Résultat de l'exécution — l'exercice (désormais clos) et l'écriture de clôture posée."""

    exercice: ExerciceResume
    entry_number: str
    resultat: int


# --- Affectation du résultat (chantier P1, lot b2a) ------------------------------------------
# Ventilation À LA MAIN (pas de taux automatique) : réserve générale (5521), réserves
# facultatives (5522), autres réserves (5523), report à nouveau (58). Sur un déficit, seul
# report_a_nouveau est autorisé (refusé côté service si les réserves sont non nulles).


class VentilationAffectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reserve_generale: int = Field(default=0, ge=0)
    reserves_facultatives: int = Field(default=0, ge=0)
    autres_reserves: int = Field(default=0, ge=0)
    report_a_nouveau: int = Field(default=0, ge=0)


class ApercuAffectation(BaseModel):
    """Dry-run : montant à affecter (signé, None si rien), et si c'est déjà fait."""

    exercice: ExerciceResume
    montant: int | None
    deja_affecte: bool
    affectable: bool


class AffectationResultatResultat(BaseModel):
    """Résultat de l'exécution — l'écriture posée et la ventilation retenue."""

    exercice: ExerciceResume
    entry_number: str
    montant: int
    ventilation: VentilationAffectation


# --- À-nouveaux (chantier P1, lot b2b) --------------------------------------------------------
# Report des soldes de clôture des comptes de BILAN (classes 1-5) de l'exercice source vers
# l'exercice suivant, journal AN. Indépendant de l'affectation du résultat (b2a, décision actée).


class LigneANouveauxSchema(BaseModel):
    account_number: str
    name: str
    account_class: int
    side: Literal["D", "C"]
    amount: int


class ApercuANouveaux(BaseModel):
    """Dry-run : comptes à reporter, totaux, exercice suivant (et son état) s'il existe."""

    exercice_source: ExerciceResume
    exercice_suivant: ExerciceResume | None
    lignes: list[LigneANouveauxSchema]
    total_debit: int
    total_credit: int
    equilibre: bool
    deja_genere: bool
    generable: bool


class ANouveauxResultat(BaseModel):
    """Résultat de l'exécution — l'exercice suivant (désormais pourvu de ses à-nouveaux)."""

    exercice_suivant: ExerciceResume
    entry_number: str
    total: int
