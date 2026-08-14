"""Schémas Pydantic — validation d'entrée par liste blanche (aucun champ non déclaré)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class OuvertureSession(BaseModel):
    """Fonds initial compté PHYSIQUEMENT par le caissier à l'ouverture. `poste_id` TOUJOURS
    obligatoire (Bloc C) — jamais déduit côté serveur, même quand l'acteur n'a qu'un seul poste
    assigné : un contrat à deux formes (obligatoire si plusieurs postes, optionnel sinon) serait
    plus fragile, et un raccourci valable aujourd'hui deviendrait un trou silencieux le jour où
    l'agence gagne un second poste. Le pré-remplissage (un seul poste assigné -> présélectionné)
    est un confort d'ÉCRAN, jamais une valeur implicite acceptée côté serveur sans confirmation."""

    poste_id: uuid.UUID
    fonds_initial: int = Field(ge=0)


class FermetureSession(BaseModel):
    """Montant compté PHYSIQUEMENT par le caissier à la fermeture — comparé au solde théorique
    calculé par le serveur, jamais saisi par le client.

    `motif` : optionnel sous le seuil de tolérance (CA2), OBLIGATOIRE au-delà — contrôle fait en
    service (le seuil est modifiable, une contrainte figée ici mentirait dès le premier
    changement). Ne bloque JAMAIS la fermeture : un motif manquant refuse la requête (422,
    message nommant l'écart et le seuil), le caissier resoumet avec le motif, rien de plus."""

    montant_reel: int = Field(ge=0)
    motif: str | None = Field(None, max_length=500)


class SessionCaisse(BaseModel):
    """Une session — `compte_caisse_number`, `caissier_nom` et `agency_nom` résolus en clair,
    jamais un UUID nu à l'écran (utile aussi pour la lettre de demande d'explication : identité
    du caissier et de son agence dans l'en-tête).

    `solde_theorique_actuel` : calculé EN DIRECT (même fonction que la fermeture, sans figer)
    tant que la session est OUVERTE — None une fois FERMÉE (voir `solde_theorique_cloture`,
    qui EST la valeur figée à ce moment-là ; pas la peine de répéter le même nombre deux fois
    sous deux noms)."""

    id: uuid.UUID
    agency_id: uuid.UUID
    agency_nom: str
    caissier_id: uuid.UUID
    caissier_nom: str
    compte_caisse_number: str
    fonds_initial: int
    opened_at: datetime
    closed_at: datetime | None
    solde_theorique_actuel: int | None
    montant_reel_cloture: int | None
    solde_theorique_cloture: int | None
    ecart: int | None
    status: str
    # CA2 : motif saisi à la fermeture, et trace de validation a posteriori (identité en clair,
    # jamais l'UUID nu — même discipline que caissier_nom/agency_nom). `a_valider` est DÉRIVÉ
    # (fermée + |ecart| > seuil + non validée), jamais stocké — voir service.py.
    motif_ecart: str | None
    valide_le: datetime | None
    valide_par_nom: str | None
    a_valider: bool


class LigneSessionManquante(BaseModel):
    """Une session fermée avec un manquant (écart < 0) — liste de `GET /caisse/sessions`."""

    id: uuid.UUID
    caissier_id: uuid.UUID
    caissier_nom: str
    agency_id: uuid.UUID
    agency_nom: str
    compte_caisse_number: str
    fonds_initial: int
    opened_at: datetime
    closed_at: datetime
    montant_reel_cloture: int
    solde_theorique_cloture: int
    ecart: int


class PageSessionsManquantes(BaseModel):
    lignes: list[LigneSessionManquante]
    total: int
    page: int
    taille: int


# --- Paramètres (CA2) ------------------------------------------------------------------------


class ParametresCaisse(BaseModel):
    seuil_tolerance: int
    is_provisional: bool


class ModificationParametresCaisse(BaseModel):
    seuil_tolerance: int = Field(ge=0)
    motif: str = Field(min_length=3, max_length=500)


class LigneSessionAValider(BaseModel):
    """Une session fermée avec un écart AU-DELÀ DU SEUIL, pas encore validée (CA2) — liste de
    `GET /caisse/sessions?a_valider=true`. Distincte de `LigneSessionManquante` : ici, manquant
    ET excédent comptent (la matérialité comptable ne connaît pas de sens), contrairement à la
    lettre de demande d'explication (manquant seul, sans seuil — les deux mécanismes restent
    volontairement séparés, voir docstring service.py)."""

    id: uuid.UUID
    caissier_id: uuid.UUID
    caissier_nom: str
    agency_id: uuid.UUID
    agency_nom: str
    compte_caisse_number: str
    fonds_initial: int
    opened_at: datetime
    closed_at: datetime
    montant_reel_cloture: int
    solde_theorique_cloture: int
    ecart: int
    motif_ecart: str | None


class PageSessionsAValider(BaseModel):
    lignes: list[LigneSessionAValider]
    total: int
    page: int
    taille: int
    seuil_tolerance: int


# --- Postes de caisse (Bloc B) --------------------------------------------------------------
# Nommage « PosteCaisse » (pas « Poste ») pour ne pas entrer en collision avec le modèle ORM
# `Poste` — même convention que SessionCaisse/CaisseSession.


class PosteCaisse(BaseModel):
    """Un poste — `agency_nom`/`compte_caisse_number`/`compte_caisse_name` résolus en clair,
    jamais un UUID nu à l'écran."""

    id: uuid.UUID
    agency_id: uuid.UUID
    agency_nom: str
    code: str
    libelle: str
    compte_caisse_number: str | None
    compte_caisse_name: str | None
    is_active: bool


class CreationPoste(BaseModel):
    code: str = Field(min_length=1, max_length=20)
    libelle: str = Field(min_length=1, max_length=150)
    motif: str = Field(min_length=3, max_length=500)


class ModificationPoste(BaseModel):
    code: str = Field(min_length=1, max_length=20)
    libelle: str = Field(min_length=1, max_length=150)
    motif: str = Field(min_length=3, max_length=500)


class ActivationPoste(BaseModel):
    is_active: bool
    motif: str = Field(min_length=3, max_length=500)


class RattachementComptePoste(BaseModel):
    """Vider le rattachement (`compte_caisse=None`) est une action légitime, pas une erreur."""

    compte_caisse: str | None
    motif: str = Field(min_length=3, max_length=500)


class UtilisateurAssigne(BaseModel):
    """Un guichetier assigné à un poste — identité en clair, jamais un UUID nu."""

    id: uuid.UUID
    matricule: str
    username: str
    nom_complet: str


class AssignationCreation(BaseModel):
    user_id: uuid.UUID
