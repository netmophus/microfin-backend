"""Module Paramétrage — lecture des référentiels : agences, pays, devises.

C'est la plus petite pièce utile du futur module Paramétrage. Elle existe parce que les
formulaires (utilisateurs, tiers) ont besoin de sélecteurs, et qu'un sélecteur a besoin d'une
source. Le reste (CRUD, produits, seuils comptables) viendra avec le module complet ; on ne le
devine pas d'avance.

Tous ces référentiels sont en lecture, AUTHENTIFIÉ suffit : leur structure n'est pas
confidentielle (tout employé sait dans quels pays opère son IMF, quelles devises elle tient).
La vraie protection reste sur les écritures (POST /tiers revalide le périmètre du créateur).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.parameters.models import Agency, Country, Currency
from app.modules.security.autorisation import UtilisateurCourant, exige_authentification

router = APIRouter(prefix="/agencies", tags=["agences"])
router_countries = APIRouter(prefix="/countries", tags=["pays"])
router_currencies = APIRouter(prefix="/currencies", tags=["devises"])


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
