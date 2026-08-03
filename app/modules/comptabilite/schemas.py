"""Schémas Pydantic — Plan de comptes, Bloc 1 (consultation + gestion unitaire).

Montants et libellés en clair, aucun champ technique exposé sans traduction. `parent_number`
est RÉSOLU (le numéro du parent, pas son UUID) : un comptable lit un numéro de compte, jamais
un identifiant opaque.
"""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


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
