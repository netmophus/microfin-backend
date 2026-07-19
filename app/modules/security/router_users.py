"""Endpoints HTTP de l'annuaire — consultation (4b) et écritures (4c).

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

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant, exige
from app.modules.security.models import Role, User
from app.modules.security.router import _contexte
from app.modules.security.schemas import (
    AgenceBreve,
    CreerUtilisateurRequest,
    ModifierUtilisateurRequest,
    PageUtilisateurs,
    RoleBref,
    UtilisateurCreeResponse,
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
from app.modules.security.utilisateurs_ecriture import (
    ActionSurSoiMemeError,
    AgenceHorsPerimetreError,
    CibleIntrouvableError,
    IdentifiantDejaUtiliseError,
    NouvelUtilisateur,
    PorteeReseauRequiseError,
    activer,
    creer,
    desactiver,
    deverrouiller,
    modifier,
    reinitialiser_mot_de_passe,
    supprimer,
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


# --- écritures (bloc 4c) --------------------------------------------------------------
#
# CORRESPONDANCE DES ERREURS, qui est une décision de sécurité et non un détail technique :
#
#   hors périmètre / inexistant / supprimé -> 404, INDISTINCTEMENT. Un 403 dirait « ce
#       compte existe, mais pas pour toi », et permettrait de cartographier les autres
#       agences en sondant des identifiants.
#   permission absente                     -> 403 (rendu en amont par exige()).
#   action sur soi-même                    -> 403 : l'appelant sait déjà qu'il existe, il
#       n'y a rien à lui cacher — seulement un pouvoir à lui refuser.
#   portée réseau requise                  -> 403, même raison.
#   agence hors périmètre                  -> 422 : la requête est recevable, sa valeur ne
#       l'est pas. Ce n'est pas un refus d'accès mais une donnée invalide.
#   identifiant déjà pris                  -> 409.


def _traduire(erreur: Exception) -> HTTPException:
    """Traduit une erreur du service en réponse HTTP. Un seul endroit pour cette table."""
    if isinstance(erreur, CibleIntrouvableError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_INTROUVABLE)
    if isinstance(erreur, ActionSurSoiMemeError):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cette action ne peut pas être effectuée sur votre propre compte.",
        )
    if isinstance(erreur, PorteeReseauRequiseError):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cette action requiert une portée réseau.",
        )
    if isinstance(erreur, AgenceHorsPerimetreError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="L'agence choisie n'est pas dans votre périmètre.",
        )
    if isinstance(erreur, IdentifiantDejaUtiliseError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Identifiant déjà utilisé : {erreur.champ}.",
        )
    raise erreur


@router.post("", response_model=UtilisateurCreeResponse, status_code=status.HTTP_201_CREATED)
def creer_utilisateur(
    corps: CreerUtilisateurRequest,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("users.create"))],
    db: Annotated[Session, Depends(get_db)],
) -> UtilisateurCreeResponse:
    """Crée un compte et renvoie son mot de passe provisoire — UNE SEULE FOIS.

    Cette réponse est le seul endroit au monde où ce mot de passe existe en clair. Il n'est
    ni stocké, ni journalisé, ni auditable, et l'API ne le redonnera jamais : le perdre
    oblige à une réinitialisation, ce qui est le comportement voulu.
    """
    try:
        resultat = creer(
            db,
            courant,
            NouvelUtilisateur(
                matricule=corps.matricule,
                email=corps.email,
                username=corps.username,
                last_name=corps.last_name,
                first_name=corps.first_name,
                phone=corps.phone,
                primary_agency_id=corps.primary_agency_id,
            ),
            _contexte(request),
        )
    except IntegrityError as erreur:
        # Filet de sécurité : deux créations concurrentes passent le contrôle applicatif
        # puis se heurtent à l'index partiel de la 0006. La base reste l'autorité.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Identifiant déjà utilisé."
        ) from erreur
    except Exception as erreur:
        raise _traduire(erreur) from None
    return UtilisateurCreeResponse(
        utilisateur=_vers_fiche(resultat.utilisateur),
        mot_de_passe_provisoire=resultat.mot_de_passe_provisoire,
    )


@router.patch("/{user_id}", response_model=UtilisateurFiche)
def modifier_utilisateur(
    user_id: uuid.UUID,
    corps: ModifierUtilisateurRequest,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("users.update"))],
    db: Annotated[Session, Depends(get_db)],
) -> UtilisateurFiche:
    """Modification partielle. Seuls les champs présents dans la requête sont touchés."""
    try:
        cible = modifier(db, courant, user_id, corps.modifications(), _contexte(request))
    except IntegrityError as erreur:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Identifiant déjà utilisé."
        ) from erreur
    except Exception as erreur:
        raise _traduire(erreur) from None
    return _vers_fiche(cible)


@router.post("/{user_id}/deactivate", response_model=UtilisateurFiche)
def desactiver_utilisateur(
    user_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("users.update"))],
    db: Annotated[Session, Depends(get_db)],
) -> UtilisateurFiche:
    """Désactive le compte ET révoque ses sessions — sans quoi il garderait 8 h d'accès."""
    try:
        return _vers_fiche(desactiver(db, courant, user_id, _contexte(request)))
    except Exception as erreur:
        raise _traduire(erreur) from None


@router.post("/{user_id}/activate", response_model=UtilisateurFiche)
def activer_utilisateur(
    user_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("users.update"))],
    db: Annotated[Session, Depends(get_db)],
) -> UtilisateurFiche:
    """Réactive le compte. Aucune session n'est restaurée : l'utilisateur se reconnecte."""
    try:
        return _vers_fiche(activer(db, courant, user_id, _contexte(request)))
    except Exception as erreur:
        raise _traduire(erreur) from None


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def supprimer_utilisateur(
    user_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("users.delete"))],
    db: Annotated[Session, Depends(get_db)],
) -> None:
    """Suppression LOGIQUE, réservée à la portée réseau (contrôlée par le service).

    users.delete ne suffit pas : un responsable d'agence désactive, il ne supprime pas.
    """
    try:
        supprimer(db, courant, user_id, _contexte(request))
    except Exception as erreur:
        raise _traduire(erreur) from None


@router.post("/{user_id}/unlock", response_model=UtilisateurFiche)
def deverrouiller_utilisateur(
    user_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("users.unlock"))],
    db: Annotated[Session, Depends(get_db)],
) -> UtilisateurFiche:
    """Lève le verrou C7. lockout_count SURVIT : déverrouiller n'absout pas l'historique."""
    try:
        return _vers_fiche(deverrouiller(db, courant, user_id, _contexte(request)))
    except Exception as erreur:
        raise _traduire(erreur) from None


@router.post("/{user_id}/reset-password", response_model=UtilisateurCreeResponse)
def reinitialiser_mot_de_passe_utilisateur(
    user_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("users.reset_password"))],
    db: Annotated[Session, Depends(get_db)],
) -> UtilisateurCreeResponse:
    """Réinitialise le mot de passe, RÉVOQUE les sessions, exige le renouvellement.

    La révocation est essentielle : on réinitialise souvent parce qu'un compte est suspect,
    et laisser vivre les sessions existantes laisserait l'intrus en place.
    """
    try:
        resultat = reinitialiser_mot_de_passe(db, courant, user_id, _contexte(request))
    except Exception as erreur:
        raise _traduire(erreur) from None
    return UtilisateurCreeResponse(
        utilisateur=_vers_fiche(resultat.utilisateur),
        mot_de_passe_provisoire=resultat.mot_de_passe_provisoire,
    )
