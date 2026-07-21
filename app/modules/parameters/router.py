"""Module Paramétrage — premier morceau : lecture des agences.

C'est la plus petite pièce utile du futur module Paramétrage. Elle existe parce que le
formulaire de création d'utilisateurs a besoin d'un sélecteur d'agences, et qu'un sélecteur
a besoin d'une source. Le reste (CRUD des agences, produits, seuils comptables) viendra avec
le module complet ; on ne le devine pas d'avance.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant, exige_authentification

router = APIRouter(prefix="/agencies", tags=["agences"])


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
