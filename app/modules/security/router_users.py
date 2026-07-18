"""Endpoints HTTP de consultation de l'annuaire (bloc 4b). Lecture seule.

Premier branchement RÉEL de la brique d'autorisation (4a) sur de vraies routes.

Les deux routes exigent users.read — donc 401 sans jeton, 403 avec un jeton dépourvu de la
permission. Le cloisonnement fin, lui, n'est pas un code d'erreur : c'est le WHERE que pose
le service. Une fiche hors périmètre n'est pas trouvée, donc 404. Jamais 403 : un 403 dirait
« ce compte existe, mais pas pour toi », et permettrait de cartographier les autres agences
en sondant des identifiants.

CONVERSION EXPLICITE. _vers_item et _vers_fiche construisent les schémas de sortie champ par
champ. Aucun model_validate(objet_orm), aucun from_attributes : ce qui n'est pas écrit ici
ne sort pas. C'est la seule protection qui survit à l'ajout d'une colonne sensible dans la
table users — un dump automatique, lui, l'exposerait le jour même.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant, exige
from app.modules.security.models import Role, User
from app.modules.security.schemas import (
    AgenceBreve,
    PageUtilisateurs,
    RoleBref,
    UtilisateurFiche,
    UtilisateurListeItem,
)
from app.modules.security.utilisateurs import (
    TAILLE_PAGE_DEFAUT,
    TAILLE_PAGE_MAX,
    FiltresUtilisateurs,
    LigneAnnuaire,
    lire,
    lister,
)

router = APIRouter(prefix="/users", tags=["users"])

MESSAGE_INTROUVABLE = "Utilisateur introuvable."


def _agence(agence: Agency | None) -> AgenceBreve | None:
    if agence is None:
        return None
    return AgenceBreve(id=agence.id, code=agence.code, name=agence.name)


def _role(role: Role) -> RoleBref:
    return RoleBref(code=role.code, name=role.name)


def _vers_item(ligne: LigneAnnuaire) -> UtilisateurListeItem:
    user = ligne.utilisateur
    return UtilisateurListeItem(
        id=user.id,
        matricule=user.matricule,
        username=user.username,
        email=user.email,
        last_name=user.last_name,
        first_name=user.first_name,
        agence=_agence(ligne.agence),
        is_active=user.is_active,
        is_locked=user.is_locked,
    )


def _vers_fiche(user: User) -> UtilisateurFiche:
    return UtilisateurFiche(
        id=user.id,
        matricule=user.matricule,
        username=user.username,
        email=user.email,
        phone=user.phone,
        last_name=user.last_name,
        first_name=user.first_name,
        agence_principale=_agence(user.primary_agency),
        agences_habilitees=[
            agence for agence in (_agence(a) for a in user.agencies) if agence is not None
        ],
        roles=[_role(role) for role in user.roles],
        is_active=user.is_active,
        is_locked=user.is_locked,
        locked_until=user.locked_until,
        must_change_password=user.must_change_password,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


@router.get("", response_model=PageUtilisateurs)
def lister_utilisateurs(
    courant: Annotated[UtilisateurCourant, Depends(exige("users.read"))],
    db: Annotated[Session, Depends(get_db)],
    q: Annotated[
        str | None,
        Query(description="Recherche libre — matricule, identifiant, email, nom, prénom."),
    ] = None,
    is_active: Annotated[bool | None, Query(description="Filtre sur l'activation.")] = None,
    agence: Annotated[uuid.UUID | None, Query(description="Rattachés OU habilités.")] = None,
    role: Annotated[str | None, Query(description="Code de rôle (ex. CAISSIER).")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    taille: Annotated[int, Query(ge=1, le=TAILLE_PAGE_MAX)] = TAILLE_PAGE_DEFAUT,
) -> PageUtilisateurs:
    """Annuaire paginé, restreint au périmètre de l'appelant.

    Un appelant sans perimetre.reseau ne voit que son agence — et le total suit le même
    filtre, sans quoi le compteur trahirait l'effectif du réseau.
    """
    resultat = lister(
        db,
        courant,
        FiltresUtilisateurs(q=q, is_active=is_active, agency_id=agence, role_code=role),
        page=page,
        taille=taille,
    )
    return PageUtilisateurs(
        lignes=[_vers_item(ligne) for ligne in resultat.lignes],
        total=resultat.total,
        page=resultat.page,
        taille=resultat.taille,
    )


@router.get("/{user_id}", response_model=UtilisateurFiche)
def lire_utilisateur(
    user_id: uuid.UUID,
    courant: Annotated[UtilisateurCourant, Depends(exige("users.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> UtilisateurFiche:
    """Fiche détaillée, ou 404.

    404 couvre INDISTINCTEMENT « n'existe pas », « supprimé » et « hors de ton périmètre ».
    C'est délibéré : distinguer ces cas revient à répondre à la question « ce compte
    existe-t-il ? » posée par quelqu'un qui n'a pas le droit de la poser.
    """
    user = lire(db, courant, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_INTROUVABLE)
    return _vers_fiche(user)
