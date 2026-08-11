"""Module Paramétrage — lecture des référentiels : agences, pays, devises.

C'est la plus petite pièce utile du futur module Paramétrage. Elle existe parce que les
formulaires (utilisateurs, tiers) ont besoin de sélecteurs, et qu'un sélecteur a besoin d'une
source. Le reste (CRUD, produits, seuils comptables) viendra avec le module complet ; on ne le
devine pas d'avance.

Tous ces référentiels sont en lecture, AUTHENTIFIÉ suffit : leur structure n'est pas
confidentielle (tout employé sait dans quels pays opère son IMF, quelles devises elle tient).
La vraie protection reste sur les écritures (POST /tiers revalide le périmètre du créateur).

EXCEPTION scopée (Bloc 5 du paramétrage comptable) : le rattachement comptable de la caisse
d'une agence (`compte_caisse_id`) se lit/s'écrit désormais ici, réservé à compta.plan.read/
manage — écriture NARROW (un seul champ), pas le CRUD complet des agences.

COEXISTENCE AVEC LE MODULE CAISSE (Bloc A/B) : `Agency.compte_caisse_id` et les postes de
caisse (`caisse.postes`) sont deux colonnes INDÉPENDANTES depuis la migration 0041 — rien ne
les synchronise. Modifier ce rattachement ici n'affecte QUE les guichets pas encore migrés
(épargne, décaissement/remboursement crédit, souscription parts comptant) ; le module Caisse
(sessions, `ouvrir_session()`) lit désormais le compte du POSTE, pas celui-ci. Une divergence
serait silencieuse sans le signal ci-dessous (`postes_divergents`) : `_postes_divergents`
compare, pour chaque agence, les postes ACTIFS dont le compte diffère de celui affiché ici —
informatif, jamais bloquant (ce rattachement reste légitimement modifiable tant que les
guichets cités en dépendent).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.caisse.models import Poste
from app.modules.comptabilite.comptes import CompteInvalideRattachementError
from app.modules.comptabilite.models import Account
from app.modules.parameters import rattachements
from app.modules.parameters.models import (
    Agency,
    Country,
    Currency,
    IdentityDocumentType,
    SecteurActivite,
)
from app.modules.security.autorisation import UtilisateurCourant, exige, exige_authentification
from app.modules.security.router import _contexte

router = APIRouter(prefix="/agencies", tags=["agences"])
router_countries = APIRouter(prefix="/countries", tags=["pays"])
router_currencies = APIRouter(prefix="/currencies", tags=["devises"])
router_doctypes = APIRouter(prefix="/identity-document-types", tags=["types de pièces"])
router_secteurs = APIRouter(prefix="/secteurs-activite", tags=["secteurs d'activité"])


class AgenceItem(BaseModel):
    """Agence réduite à ce qu'un sélecteur affiche. Construit champ par champ (règle projet)."""

    id: uuid.UUID
    code: str
    name: str


@router.get("", response_model=list[AgenceItem])
def lister_agences(
    _: Annotated[UtilisateurCourant, Depends(exige_authentification())],
    db: Annotated[Session, Depends(get_db)],
) -> list[AgenceItem]:
    """Liste les agences ACTIVES. Authentifié suffit, aucune permission particulière.

    La structure d'agences n'est pas confidentielle : tout employé sait où sont les guichets
    de son institution. Et la vraie protection reste sur POST /users, qui revalide le
    périmètre du créateur — un sélecteur non filtré est au pire un défaut d'ergonomie, jamais
    une faille.

    RAFFINEMENT À VENIR : le jour où un responsable d'agence créera vraiment des comptes, il
    faudra filtrer cette liste sur SON périmètre (condition_perimetre), pour ne pas lui
    proposer des agences où il ne peut de toute façon pas rattacher. Aujourd'hui seul
    l'administrateur (portée réseau) crée des comptes, donc la question ne se pose pas encore.
    """
    lignes = db.execute(
        select(Agency.id, Agency.code, Agency.name)
        .where(Agency.is_active.is_(True))
        .order_by(Agency.name)
    )
    return [AgenceItem(id=ligne.id, code=ligne.code, name=ligne.name) for ligne in lignes]


# --- Rattachement comptable de la caisse (Bloc 5 du paramétrage comptable) -----------------

MESSAGE_AGENCE_INTROUVABLE = "Agence introuvable."


class CompteRattachementAgence(BaseModel):
    """Un compte résolu — numéro + libellé, jamais l'UUID (règle du projet)."""

    account_number: str
    name: str


class PosteDivergent(BaseModel):
    """Un poste ACTIF de cette agence dont le compte diffère de `compte_caisse` ci-dessus —
    signal de dérive entre l'ancien rattachement (agence) et le nouveau (poste), voir docstring
    du module."""

    code: str
    libelle: str
    compte_caisse: CompteRattachementAgence | None


class AgenceRattachement(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    compte_caisse: CompteRattachementAgence | None
    postes_divergents: list[PosteDivergent]


class ModificationRattachementCaisse(BaseModel):
    compte_caisse: str | None
    motif: str = Field(min_length=3, max_length=500)


def _compte_rattachement_agence(
    db: Session, account_id: uuid.UUID | None
) -> CompteRattachementAgence | None:
    if account_id is None:
        return None
    compte = db.get(Account, account_id)
    if compte is None:
        return None
    return CompteRattachementAgence(account_number=compte.account_number, name=compte.name)


def _postes_divergents(db: Session, agence: Agency) -> list[PosteDivergent]:
    """Postes ACTIFS de l'agence dont le compte diffère de `Agency.compte_caisse_id` — voir
    COEXISTENCE dans la docstring du module. None vs un compte réel compte comme divergent."""
    postes = db.execute(
        select(Poste).where(Poste.agency_id == agence.id, Poste.is_active.is_(True))
    ).scalars()
    return [
        PosteDivergent(
            code=poste.code,
            libelle=poste.libelle,
            compte_caisse=_compte_rattachement_agence(db, poste.compte_caisse_id),
        )
        for poste in postes
        if poste.compte_caisse_id != agence.compte_caisse_id
    ]


def _vers_rattachement_agence(db: Session, agence: Agency) -> AgenceRattachement:
    return AgenceRattachement(
        id=agence.id,
        code=agence.code,
        name=agence.name,
        compte_caisse=_compte_rattachement_agence(db, agence.compte_caisse_id),
        postes_divergents=_postes_divergents(db, agence),
    )


@router.get("/rattachements", response_model=list[AgenceRattachement])
def lister_rattachements_agences(
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.plan.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> list[AgenceRattachement]:
    """Rattachement caisse des agences ACTIVES (Bloc 5) — écran du comptable."""
    agences = list(
        db.execute(select(Agency).where(Agency.is_active).order_by(Agency.name)).scalars()
    )
    return [_vers_rattachement_agence(db, a) for a in agences]


@router.patch("/{agence_id}/compte-caisse", response_model=AgenceRattachement)
def modifier_compte_caisse_endpoint(
    agence_id: uuid.UUID,
    corps: ModificationRattachementCaisse,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.plan.manage"))],
    db: Annotated[Session, Depends(get_db)],
) -> AgenceRattachement:
    """Ce changement s'applique aux PROCHAINES opérations seulement — les écritures déjà
    posées référencent directement un compte, jamais ce paramètre."""
    agence = db.get(Agency, agence_id)
    if agence is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_AGENCE_INTROUVABLE
        )
    try:
        rattachements.modifier_compte_caisse(
            db,
            agence,
            compte_caisse_number=corps.compte_caisse,
            motif=corps.motif,
            par=courant.user_id,
            contexte=_contexte(request),
        )
        db.commit()
    except CompteInvalideRattachementError as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    return _vers_rattachement_agence(db, agence)


class CountryItem(BaseModel):
    """Pays réduit à ce qu'un sélecteur de nationalité affiche."""

    id: uuid.UUID
    code: str
    name: str


@router_countries.get("", response_model=list[CountryItem])
def lister_pays(
    _: Annotated[UtilisateurCourant, Depends(exige_authentification())],
    db: Annotated[Session, Depends(get_db)],
) -> list[CountryItem]:
    """Liste les pays ACTIFS, UEMOA en tête (display_order), puis alphabétique.

    Source du sélecteur de nationalité / pays de naissance / siège des formulaires tiers.
    """
    lignes = db.execute(
        select(Country.id, Country.code, Country.name)
        .where(Country.is_active.is_(True))
        .order_by(Country.display_order, Country.name)
    )
    return [CountryItem(id=ligne.id, code=ligne.code, name=ligne.name) for ligne in lignes]


class CurrencyItem(BaseModel):
    """Devise réduite à ce qu'un sélecteur affiche. decimal_places sert au formatage (XOF = 0)."""

    id: uuid.UUID
    code: str
    name: str
    decimal_places: int


@router_currencies.get("", response_model=list[CurrencyItem])
def lister_devises(
    _: Annotated[UtilisateurCourant, Depends(exige_authentification())],
    db: Annotated[Session, Depends(get_db)],
) -> list[CurrencyItem]:
    """Liste les devises ACTIVES. Source du sélecteur de capital des personnes morales."""
    lignes = db.execute(
        select(Currency.id, Currency.code, Currency.name, Currency.decimal_places)
        .where(Currency.is_active.is_(True))
        .order_by(Currency.display_order, Currency.code)
    )
    return [
        CurrencyItem(
            id=ligne.id, code=ligne.code, name=ligne.name, decimal_places=ligne.decimal_places
        )
        for ligne in lignes
    ]


class TypePieceItem(BaseModel):
    """Type de pièce pour le sélecteur de saisie (T2c). `requires_expiry_date` dit au formulaire
    s'il doit réclamer une échéance ; `enforce_unique` n'est PAS exposé (le contrôle d'unicité
    reste au service — le front n'a pas à connaître la règle pour l'appliquer)."""

    id: uuid.UUID
    code: str
    name: str
    requires_expiry_date: bool


@router_doctypes.get("", response_model=list[TypePieceItem])
def lister_types_pieces(
    _: Annotated[UtilisateurCourant, Depends(exige_authentification())],
    db: Annotated[Session, Depends(get_db)],
) -> list[TypePieceItem]:
    """Liste les types de pièces ACTIFS (display_order), pour le sélecteur de saisie des pièces."""
    lignes = db.execute(
        select(
            IdentityDocumentType.id,
            IdentityDocumentType.code,
            IdentityDocumentType.name,
            IdentityDocumentType.requires_expiry_date,
        )
        .where(IdentityDocumentType.is_active.is_(True))
        .order_by(IdentityDocumentType.display_order, IdentityDocumentType.name)
    )
    return [
        TypePieceItem(
            id=ligne.id,
            code=ligne.code,
            name=ligne.name,
            requires_expiry_date=ligne.requires_expiry_date,
        )
        for ligne in lignes
    ]


class SecteurItem(BaseModel):
    """Secteur d'activité pour le sélecteur KYC (T3c). `is_a_risque` n'est PAS exposé : la
    conséquence sur le risque relève du moteur, pas de l'écran de saisie."""

    id: uuid.UUID
    code: str
    libelle: str


@router_secteurs.get("", response_model=list[SecteurItem])
def lister_secteurs(
    _: Annotated[UtilisateurCourant, Depends(exige_authentification())],
    db: Annotated[Session, Depends(get_db)],
) -> list[SecteurItem]:
    """Liste les secteurs d'activité ACTIFS (display_order), pour le sélecteur KYC."""
    lignes = db.execute(
        select(SecteurActivite.id, SecteurActivite.code, SecteurActivite.libelle)
        .where(SecteurActivite.is_active.is_(True))
        .order_by(SecteurActivite.display_order, SecteurActivite.libelle)
    )
    return [SecteurItem(id=ligne.id, code=ligne.code, libelle=ligne.libelle) for ligne in lignes]
