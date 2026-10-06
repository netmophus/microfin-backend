"""Contrats d'entrée/sortie de l'API Conformité — tableau de bord des ratios prudentiels
(lecture, `conformite.ratio.read`) et paramétrage (CRUD, `conformite.ratio.manage`), lot
P2.1.a (couche API au-dessus du moteur `app/modules/conformite/moteur.py`).

La SORTIE est construite champ par champ dans le router (aucun from_attributes) — même
patron que credit/comptabilite. Les références entre agrégats et ratios se font par `code`
(identifiant métier stable), jamais par UUID brut à l'écran, SAUF pour les routes de
paramétrage (POST/PATCH/retirer), qui adressent la ressource par son `id` — même convention
que les paliers de souffrance (`credit.delinquency_tier`).
"""

import uuid
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CategorieSfd = Literal["SANS_DEPOTS", "AFFILIE", "NON_AFFILIE"]
TypeAgregat = Literal["BALANCE", "SPECIAL"]
Operateur = Literal["GE", "LE"]
StatutRatio = Literal["CONFORME", "NON_CONFORME", "NON_CALCULABLE"]

# --- Volet 1 — Lecture (tableau de bord) ---------------------------------------------------


class AvertissementSchema(BaseModel):
    """Message NON BLOQUANT attaché à une évaluation (voir `moteur.calculer_avertissements`) :
    il ne change ni la valeur ni le statut du ratio, il en rend lisible un cas anormal."""

    code: str
    libelle: str


class RatioEvalue(BaseModel):
    """Un ratio évalué à `a_la_date` — ou, si `actif` est faux, un ratio « en attente » dont
    les champs numériques sont volontairement vides (jamais calculés, voir router : un ratio
    inactif n'est jamais soumis au moteur dans la liste, pour qu'un seul paramétrage incomplet
    ne puisse jamais faire échouer tout le tableau de bord — même esprit que l'état « partiel »,
    §6 CLAUDE.md)."""

    code: str
    libelle: str
    reference_reglementaire: str | None
    operateur: Operateur
    seuil_applicable: Decimal | None
    valeur_numerateur: int | None
    valeur_denominateur: int | None
    valeur_ratio_pct: Decimal | None
    conforme: bool | None
    marge: Decimal | None
    statut: StatutRatio
    actif: bool
    # Ajout seul : vide pour un ratio « en attente » (jamais évalué) comme pour un cas sain.
    avertissements: list[AvertissementSchema] = Field(default_factory=list)


class TableauRatios(BaseModel):
    """Enveloppe de `GET /conformite/ratios`. `aucune_ecriture_validee` est levé quand AUCUNE
    écriture validée n'existe jusqu'à la date d'arrêté : une vraie base vide, à distinguer d'une
    base peu active (comptage exact, jamais déduit des montants des agrégats)."""

    aucune_ecriture_validee: bool
    ratios: list[RatioEvalue]


class ComposantAgregatSchema(BaseModel):
    """Une ligne de composition décomposée — `solde` = somme des comptes matchés par
    `prefixe_compte`, `contribution` = `sens * solde` (ce qui entre réellement dans
    l'agrégat). Voir `moteur.ComposantAgregat`."""

    prefixe_compte: str
    sens: Literal[1, -1]
    solde: int
    contribution: int


class DetailAgregatSchema(BaseModel):
    """Décomposition d'un agrégat (numérateur ou dénominateur), pour justifier un chiffre à
    la tutelle. `composants` est vide pour un agrégat SPECIAL."""

    code: str
    libelle: str
    type: TypeAgregat
    valeur: int
    composants: list[ComposantAgregatSchema]
    complement_provisions_tutelle_applique: int | None


class RatioDetail(RatioEvalue):
    """Le détail complet d'UN ratio — même payload que la liste, PLUS la décomposition de ses
    deux agrégats. Contrairement à la liste, cet endpoint évalue TOUJOURS le ratio demandé,
    actif ou non (l'administrateur qui paramètre doit voir le calcul réel) ; si l'agrégat
    référencé est un SPECIAL sans fonction enregistrée, l'endpoint renvoie un 422 clair plutôt
    que de deviner une valeur — voir router."""

    agregat_numerateur: DetailAgregatSchema
    agregat_denominateur: DetailAgregatSchema


# --- Volet 2 — Paramétrage (CRUD) -----------------------------------------------------------


class LigneCompositionAgregat(BaseModel):
    """Une ligne (préfixe de compte, sens) — entrée ET sortie."""

    prefixe_compte: str = Field(min_length=1, max_length=20)
    sens: Literal[1, -1]


class AgregatAdmin(BaseModel):
    """Un agrégat tel que vu par l'écran de paramétrage — composition incluse."""

    id: uuid.UUID
    code: str
    libelle: str
    reference: str | None
    type: TypeAgregat
    calcul_special: str | None
    nets_de_provisions: bool
    applique_complement_provisions_tutelle: bool
    is_system: bool
    composition: list[LigneCompositionAgregat]


class _CorpsAgregat(BaseModel):
    """Champs communs à la création et à la modification (remplacement complet, y compris la
    composition — même discipline que les rattachements produit : l'écran soumet l'état
    entier à chaque enregistrement, pas un PATCH partiel)."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=40)
    libelle: str = Field(min_length=1, max_length=200)
    reference: str | None = Field(default=None, max_length=200)
    type: TypeAgregat
    calcul_special: str | None = Field(default=None, max_length=50)
    nets_de_provisions: bool = False
    applique_complement_provisions_tutelle: bool = False
    composition: list[LigneCompositionAgregat] = Field(default_factory=list)
    motif: str = Field(min_length=3, max_length=500)

    @model_validator(mode="after")
    def _coherence_type_calcul(self) -> "_CorpsAgregat":
        if self.type == "SPECIAL" and not self.calcul_special:
            raise ValueError(
                "Un agrégat de type SPECIAL doit indiquer un calcul_special (nom de la "
                "fonction de calcul dédiée)."
            )
        if self.type == "BALANCE" and self.calcul_special:
            raise ValueError(
                "Un agrégat de type BALANCE ne doit pas indiquer de calcul_special — sa "
                "composition se déclare par les lignes (préfixe, sens)."
            )
        if self.type == "SPECIAL" and self.composition:
            raise ValueError(
                "Un agrégat de type SPECIAL n'a pas de composition par préfixe de compte."
            )
        return self


class CreationAgregat(_CorpsAgregat):
    pass


class ModificationAgregat(_CorpsAgregat):
    pass


class SuppressionAgregat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    motif: str = Field(min_length=3, max_length=500)


class RatioAdmin(BaseModel):
    """Un ratio tel que vu par l'écran de paramétrage — non évalué (voir RatioEvalue pour le
    tableau de bord)."""

    id: uuid.UUID
    code: str
    libelle: str
    reference_reglementaire: str | None
    agregat_numerateur_code: str
    agregat_denominateur_code: str
    operateur: Operateur
    actif: bool
    ordre: int
    is_system: bool


class _CorpsRatio(BaseModel):
    """Remplacement complet — inclut `actif` (activation/désactivation) et `ordre`, pas de
    routes séparées pour ces deux-là."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=40)
    libelle: str = Field(min_length=1, max_length=200)
    reference_reglementaire: str | None = Field(default=None, max_length=200)
    agregat_numerateur_code: str = Field(min_length=1, max_length=40)
    agregat_denominateur_code: str = Field(min_length=1, max_length=40)
    operateur: Operateur
    actif: bool = True
    ordre: int = Field(ge=1)
    motif: str = Field(min_length=3, max_length=500)


class CreationRatio(_CorpsRatio):
    pass


class ModificationRatio(_CorpsRatio):
    pass


class SuppressionRatio(BaseModel):
    model_config = ConfigDict(extra="forbid")

    motif: str = Field(min_length=3, max_length=500)


class SeuilAdmin(BaseModel):
    id: uuid.UUID
    ratio_id: uuid.UUID
    categorie_sfd: CategorieSfd | None
    valeur_seuil: Decimal
    is_system: bool


class _CorpsSeuil(BaseModel):
    model_config = ConfigDict(extra="forbid")

    categorie_sfd: CategorieSfd | None = None
    valeur_seuil: Decimal = Field(ge=0)
    motif: str = Field(min_length=3, max_length=500)


class CreationSeuil(_CorpsSeuil):
    pass


class ModificationSeuil(_CorpsSeuil):
    pass


class SuppressionSeuil(BaseModel):
    model_config = ConfigDict(extra="forbid")

    motif: str = Field(min_length=3, max_length=500)


class ParametreInstitutionSchema(BaseModel):
    id: uuid.UUID
    categorie_sfd: CategorieSfd
    complement_provisions_tutelle: int


class ModificationParametreInstitution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    categorie_sfd: CategorieSfd
    complement_provisions_tutelle: int = Field(ge=0)
    motif: str = Field(min_length=3, max_length=500)
