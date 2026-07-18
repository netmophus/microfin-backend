"""Consultation de l'annuaire des utilisateurs (bloc 4b) — lecture seule.

Premier usage RÉEL de la brique d'autorisation (4a) : c'est ici que le cloisonnement par
agence cesse d'être une intention et devient une clause WHERE.

DEUX RÈGLES QUI TIENNENT TOUT LE FICHIER

1. UNE SEULE fonction de visibilité, _condition_visibilite, partagée par la liste, la fiche
   ET le compteur de total. Si ces trois-là appliquaient des filtres écrits séparément, ils
   divergeraient un jour — et la divergence serait silencieuse : la fiche répondrait 200 sur
   une ligne que la liste cache, ou le total annoncerait des résultats invisibles. Un seul
   endroit à lire pour savoir ce qu'un utilisateur a le droit de voir.

2. Le filtre est DANS la requête, jamais après. On ne charge pas une ligne pour comparer son
   agence ensuite : on ne la trouve pas du tout. C'est ce qui donne le 404 naturel sur une
   fiche hors périmètre, là où un contrôle après lecture obligerait à répondre 403 — donc à
   confirmer que le compte existe. Un responsable d'agence ne doit pas pouvoir dénombrer les
   employés d'une autre agence en sondant des identifiants.

PÉRIMÈTRE = RATTACHEMENT **OU** HABILITATION. Un utilisateur relève de l'agence X s'il y est
rattaché (primary_agency_id) ou s'il y est habilité (ligne dans user_agencies). C'est
exactement la définition qu'applique déjà _resoudre_agence dans auth.py pour accepter une
connexion (C6) ; en retenir une autre ici créerait deux définitions concurrentes de « relève
de l'agence X », qui divergeraient. Concrètement : un agent rattaché à A mais habilité à B,
qui travaille à B aujourd'hui, DOIT être visible du responsable de B — sinon celui-ci ne
pourra pas déverrouiller son compte (4d) alors qu'il l'a devant lui, au guichet.

RECHERCHE INSENSIBLE À LA CASSE ET AUX ACCENTS. « KANE » trouve « Kané », « traore » trouve
« Traoré ». Voir migration 0005 pour le pourquoi (saisies avec ou sans accents selon
l'opérateur) et pour la dette d'index assumée.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy import func, or_, select
from sqlalchemy.orm import InstrumentedAttribute, Session, selectinload
from sqlalchemy.sql.elements import ColumnElement

from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.security.models import Role, User, UserAgency, UserRole

# Bornes de pagination. Le plafond n'est pas décoratif : sans lui, ?taille=100000 exfiltre
# l'annuaire entier en une requête et fait du compteur un inventaire.
TAILLE_PAGE_DEFAUT = 25
TAILLE_PAGE_MAX = 100

# Champs balayés par la recherche libre (q).
CHAMPS_RECHERCHE = (User.matricule, User.username, User.email, User.last_name, User.first_name)


@dataclass(frozen=True)
class FiltresUtilisateurs:
    """Filtres demandés par l'appelant. Distincts du PÉRIMÈTRE, qui, lui, s'impose à lui."""

    q: str | None = None
    is_active: bool | None = None
    agency_id: uuid.UUID | None = None
    role_code: str | None = None


@dataclass(frozen=True)
class LigneAnnuaire:
    """Une ligne de liste : l'utilisateur et son agence de rattachement, déjà jointe.

    L'agence voyage avec la ligne parce qu'elle est ramenée par la MÊME requête (LEFT JOIN).
    Rendre l'utilisateur seul obligerait la couche de présentation à relire l'agence ligne
    par ligne — le N+1 classique sur une liste paginée.
    """

    utilisateur: User
    agence: Agency | None


@dataclass(frozen=True)
class PageAnnuaire:
    lignes: Sequence[LigneAnnuaire]
    total: int
    page: int
    taille: int


def _echapper_like(valeur: str) -> str:
    """Neutralise les jokers SQL saisis par l'utilisateur.

    Sans ça, chercher « _ » ou « % » ramène tout l'annuaire : ce n'est pas une injection
    (les valeurs restent des paramètres liés), mais une recherche qui ment sur son résultat.
    Le backslash est échappé EN PREMIER, sinon on ré-échapperait les échappements posés
    ensuite.
    """
    for caractere in ("\\", "%", "_"):
        valeur = valeur.replace(caractere, f"\\{caractere}")
    return valeur


def _normaliser(
    expression: ColumnElement[str] | InstrumentedAttribute[str] | str,
) -> ColumnElement[str]:
    """minuscules + sans accents, côté PostgreSQL.

    Le CAST en text n'est pas superflu sur email et username, qui sont en CITEXT :
    unaccent() rend du text, donc l'insensibilité à la casse offerte par CITEXT est PERDUE
    dès qu'on l'enveloppe. D'où lower() appliqué explicitement à TOUS les champs, y compris
    ceux qu'on croirait déjà couverts — sinon « KANE » trouverait sur trois champs sur cinq.

    La normalisation est faite par la base, pas en Python : le motif et la colonne doivent
    subir exactement la même transformation, et les règles de unaccent sont celles de
    PostgreSQL.
    """
    return func.unaccent(func.lower(sa.cast(expression, sa.Text)))


def _condition_recherche(q: str) -> ColumnElement[bool]:
    motif = f"%{_echapper_like(q.strip())}%"
    return or_(
        *(_normaliser(champ).like(_normaliser(motif), escape="\\") for champ in CHAMPS_RECHERCHE)
    )


def _releve_de_agence(agence_id: uuid.UUID) -> ColumnElement[bool]:
    """« Cet utilisateur relève de l'agence X » : rattaché OU habilité.

    Même définition que _resoudre_agence (auth.py, C6). Une seule notion, un seul endroit
    où la lire.
    """
    return or_(
        User.primary_agency_id == agence_id,
        select(1).where(UserAgency.user_id == User.id, UserAgency.agency_id == agence_id).exists(),
    )


def _condition_visibilite(courant: UtilisateurCourant) -> ColumnElement[bool]:
    """CE QUE `courant` A LE DROIT DE VOIR. La seule porte d'entrée de tout ce module.

    Deux clauses, toutes deux non négociables :
      - soft-delete : un utilisateur supprimé n'existe plus pour l'API, nulle part ;
      - périmètre   : réseau -> tout ; agence -> son agence ; ni l'un ni l'autre -> RIEN.

    Le fail-secure du troisième cas vit dans condition_perimetre_sur (4a), pas ici : le
    recopier dans chaque service est précisément ce qui a produit la faille corrigée en 4a.
    """
    return sa.and_(
        User.deleted_at.is_(None),
        courant.condition_perimetre_sur(_releve_de_agence),
    )


def _conditions_filtres(filtres: FiltresUtilisateurs) -> list[ColumnElement[bool]]:
    """Filtres DEMANDÉS. Ils restreignent toujours, ils n'élargissent jamais le périmètre."""
    conditions: list[ColumnElement[bool]] = []
    if filtres.q:
        conditions.append(_condition_recherche(filtres.q))
    if filtres.is_active is not None:
        conditions.append(User.is_active.is_(filtres.is_active))
    if filtres.agency_id is not None:
        # Même définition que le périmètre : « qui travaille dans cette agence », et non
        # « qui y est administrativement rattaché ». Deux réponses différentes à la même
        # question dans un même écran seraient incompréhensibles.
        conditions.append(_releve_de_agence(filtres.agency_id))
    if filtres.role_code:
        conditions.append(
            select(1)
            .where(
                UserRole.user_id == User.id,
                UserRole.role_id == Role.id,
                Role.code == filtres.role_code,
            )
            .exists()
        )
    return conditions


def lister(
    db: Session,
    courant: UtilisateurCourant,
    filtres: FiltresUtilisateurs | None = None,
    page: int = 1,
    taille: int = TAILLE_PAGE_DEFAUT,
) -> PageAnnuaire:
    """Page de l'annuaire visible par `courant`, plus le total sous les MÊMES conditions.

    Le total porte exactement les mêmes clauses que la page. Un compteur calculé sans le
    filtre d'agence afficherait « 247 résultats » à un responsable qui n'en voit que douze
    — et lui révélerait l'effectif du réseau. Le total est une fuite potentielle, pas un
    simple confort d'affichage.
    """
    filtres = filtres or FiltresUtilisateurs()
    taille = max(1, min(taille, TAILLE_PAGE_MAX))
    page = max(1, page)
    conditions = [_condition_visibilite(courant), *_conditions_filtres(filtres)]

    total = db.execute(select(func.count()).select_from(User).where(*conditions)).scalar_one()

    lignes = db.execute(
        select(User, Agency)
        .outerjoin(Agency, User.primary_agency_id == Agency.id)
        .where(*conditions)
        # Tri déterministe, id en dernier ressort : deux homonymes sans départage feraient
        # osciller l'ordre entre deux pages, donc apparaître ou disparaître des lignes.
        .order_by(User.last_name, User.first_name, User.id)
        .offset((page - 1) * taille)
        .limit(taille)
    ).all()

    return PageAnnuaire(
        lignes=[LigneAnnuaire(utilisateur=u, agence=a) for u, a in lignes],
        total=total,
        page=page,
        taille=taille,
    )


def lire(db: Session, courant: UtilisateurCourant, user_id: uuid.UUID) -> User | None:
    """La fiche, ou None si elle n'existe pas OU est hors du périmètre de `courant`.

    Les deux cas sont volontairement INDISTINGUABLES, et c'est tout l'objet du bloc : le
    routeur répond 404 dans les deux cas. Répondre 403 sur « existe mais pas pour toi »
    confirmerait l'existence du compte, et permettrait de cartographier les autres agences
    par sondage d'identifiants.

    Le périmètre est dans le WHERE — pas un contrôle après chargement.
    """
    return db.execute(
        select(User)
        .options(
            selectinload(User.roles),
            selectinload(User.agencies),
            selectinload(User.primary_agency),
        )
        .where(User.id == user_id, _condition_visibilite(courant))
    ).scalar_one_or_none()
