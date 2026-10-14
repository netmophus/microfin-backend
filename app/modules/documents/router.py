"""API des documents archivés : retrouver, télécharger, régénérer.

Tout est gardé par `documents.read` ET cloisonné à l'agence : un document hors du périmètre de
l'acteur n'existe pas pour lui (404, jamais 403). Chaque téléchargement est audité.
"""

import logging
import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.comptabilite.journee import date_comptable_courante
from app.modules.documents import etats, service
from app.modules.documents.models import Archive
from app.modules.documents.schemas import DocumentResume, PageDocuments
from app.modules.security.autorisation import UtilisateurCourant, exige
from app.modules.security.router import _contexte

router = APIRouter(tags=["documents"])
journal = logging.getLogger(__name__)

MESSAGE_INTROUVABLE = "Ce document n'existe pas ou n'est pas accessible."
TAILLE_PAGE_DEFAUT = 25


def _resume(archive: Archive) -> DocumentResume:
    return DocumentResume(
        id=archive.id,
        type=archive.type,
        module=archive.module,
        journee_comptable=archive.journee_comptable,
        nom_fichier=service.nom_de_fichier(archive),
        numero_piece=archive.numero_piece,
        objet_lie_type=archive.objet_lie_type,
        objet_lie_id=archive.objet_lie_id,
        agency_id=archive.agency_id,
        statut=archive.statut,
        erreur=archive.erreur,
        taille=archive.taille,
        sha256=archive.sha256,
        cree_le=archive.cree_le,
    )


def _introuvable() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_INTROUVABLE)


@router.get("/documents", response_model=PageDocuments)
def lister_documents_endpoint(
    courant: Annotated[UtilisateurCourant, Depends(exige("documents.read"))],
    db: Annotated[Session, Depends(get_db)],
    journee: Annotated[date | None, Query(description="Journée comptable (AAAA-MM-JJ).")] = None,
    module: Annotated[str | None, Query(max_length=40)] = None,
    type: Annotated[str | None, Query(max_length=60)] = None,
    agency_id: Annotated[uuid.UUID | None, Query()] = None,
    piece: Annotated[str | None, Query(max_length=60, description="N° de pièce.")] = None,
    statut: Annotated[str | None, Query(pattern="^(ok|echec)$")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    taille: Annotated[int, Query(ge=1, le=service.TAILLE_PAGE_MAX)] = TAILLE_PAGE_DEFAUT,
) -> PageDocuments:
    """Les documents du périmètre de l'acteur, filtrables, paginés côté serveur."""
    filtres = service.FiltresDocuments(
        journee=journee,
        module=module,
        type=type,
        agency_id=agency_id,
        numero_piece=piece,
        statut=statut,
    )
    lignes, total = service.lister(db, courant, filtres, page=page, taille=taille)
    return PageDocuments(
        lignes=[_resume(archive) for archive in lignes], total=total, page=page, taille=taille
    )


ETATS_PAR_SEGMENT = {
    "journal-du-jour": etats.TYPE_JOURNAL,
    "balance-du-jour": etats.TYPE_BALANCE,
}
MESSAGE_JOURNEE_NON_OUVERTE = (
    "Seule la journée ouverte peut être demandée en brouillon : les journées clôturées ont leurs "
    "états figés dans les documents."
)
MESSAGE_AUCUNE_JOURNEE_OUVERTE = (
    "Aucune journée comptable n'est ouverte : il n'y a pas de brouillon."
)
MESSAGE_ETAT_INDISPONIBLE = "Le brouillon n'a pas pu être généré. Réessayez."


@router.get("/documents/etats/{segment}/brouillon")
def brouillon_etat_endpoint(
    segment: str,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("compta.rapport.read"))],
    db: Annotated[Session, Depends(get_db)],
    journee: Annotated[
        date | None, Query(description="Doit être la journée ouverte (défaut : elle).")
    ] = None,
) -> Response:
    """Journal ou balance de la journée OUVERTE, en BROUILLON : rendu à la volée, jamais archivé
    (un brouillon n'est pas une pièce). Les états des journées clôturées sont archivés et figés
    à la clôture : on les retrouve par GET /documents."""
    type_etat = ETATS_PAR_SEGMENT.get(segment)
    if type_etat is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MESSAGE_INTROUVABLE)
    ouverte = date_comptable_courante(db)
    if ouverte is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=MESSAGE_AUCUNE_JOURNEE_OUVERTE,
        )
    if journee is not None and journee != ouverte:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=MESSAGE_JOURNEE_NON_OUVERTE
        )
    try:
        pdf, nom = etats.rendre_brouillon(
            db, courant, type_etat, ouverte, contexte_requete=_contexte(request)
        )
    except Exception:
        journal.exception("Brouillon %s impossible (%s)", type_etat, ouverte)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=MESSAGE_ETAT_INDISPONIBLE
        ) from None
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{nom}"',
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/documents/{document_id}/fichier")
def telecharger_document_endpoint(
    document_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("documents.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Le PDF archivé, tel qu'écrit (jamais régénéré). Contrôle d'accès à l'objet, contrôle de
    l'empreinte, une ligne d'audit par téléchargement."""
    try:
        archive, contenu = service.lire_fichier(
            db, courant, document_id, contexte_requete=_contexte(request)
        )
    except service.DocumentIntrouvableError:
        raise _introuvable() from None
    except service.FichierIndisponibleError as erreur:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(erreur)) from None
    except service.ArchiveAltereeError as erreur:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(erreur)) from None
    return Response(
        content=contenu,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{service.nom_de_fichier(archive)}"',
            "Cache-Control": "private, no-store",
        },
    )


@router.post("/documents/{document_id}/regenerer", response_model=DocumentResume)
def regenerer_document_endpoint(
    document_id: uuid.UUID,
    request: Request,
    courant: Annotated[UtilisateurCourant, Depends(exige("documents.read"))],
    db: Annotated[Session, Depends(get_db)],
) -> DocumentResume:
    """Rattrape un document dont la génération a échoué (idempotent). 422 si le document n'est
    pas en échec ; 404 hors périmètre."""
    try:
        archive = service.regenerer(db, courant, document_id, contexte_requete=_contexte(request))
    except service.DocumentIntrouvableError:
        raise _introuvable() from None
    except service.RegenerationImpossibleError as erreur:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(erreur)
        ) from None
    return _resume(archive)
