"""Schémas de l'API des documents archivés. Jamais le chemin du serveur : seulement le nom du
fichier (un chemin d'archive est une information interne)."""

import uuid
from datetime import date, datetime

from pydantic import BaseModel


class DocumentRef(BaseModel):
    """Référence d'un document, jointe au résultat d'une opération : de quoi le télécharger
    (`GET /documents/{id}/fichier`) ou, en cas d'échec de génération, le régénérer
    (`POST /documents/{id}/regenerer`). L'opération elle-même est déjà validée."""

    id: uuid.UUID
    nom: str
    statut: str  # 'ok' | 'echec'


class DocumentResume(BaseModel):
    id: uuid.UUID
    type: str
    module: str
    journee_comptable: date  # la journée comptable : celle qui date le document
    nom_fichier: str
    numero_piece: str | None
    objet_lie_type: str | None
    objet_lie_id: uuid.UUID | None
    agency_id: uuid.UUID
    statut: str  # 'ok' | 'echec'
    erreur: str | None  # message technique d'une génération échouée
    taille: int | None
    sha256: str | None
    cree_le: datetime  # horodatage réel (audit), distinct de la journée comptable


class PageDocuments(BaseModel):
    lignes: list[DocumentResume]
    total: int
    page: int
    taille: int
