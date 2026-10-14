"""Archivage des documents : génération (PDF/A), écriture unique sur disque, index immuable.

RÈGLES (voir docs/runbook-documents.md) :
  - Le PDF se génère APRÈS le commit métier, jamais dedans : un échec de rendu ou d'écriture ne
    défait jamais une opération déjà comptabilisée. `generer_apres_commit` ne lève pas : elle pose
    une ligne d'index `echec` (avec le message technique) que `regenerer` rattrape.
  - Écriture UNIQUE : le fichier est lié sans écraser ; nommage déterministe ; une seconde demande
    pour la même clé naturelle retrouve le document existant (aucune réécriture).
  - Datation : la JOURNÉE COMPTABLE de l'opération, jamais l'horloge, pour le dossier comme pour
    le contenu.
  - Index immuable : une régénération pose une NOUVELLE ligne `ok`, elle ne modifie jamais une
    ligne `echec`.
"""

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast

from sqlalchemy import ColumnElement, and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.documents import stockage
from app.modules.documents.models import STATUT_ECHEC, STATUT_OK, Archive
from app.modules.documents.rendu import rendre_pdfa
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant

journal = logging.getLogger(__name__)

RESSOURCE = "documents.archive"
ACTION_CREE = "documents.archive.cree"
ACTION_TELECHARGE = "documents.telecharge"
TAILLE_PAGE_MAX = 100

# Type de document -> gabarit Jinja. Un seul gabarit de reçu sert tous les reçus (les lignes et
# les règles de signature viennent du contexte, voir recus.py). Les types suivants (parts, lettre,
# états) s'y ajoutent aux lots suivants, sans toucher au socle.
GABARIT_DU_TYPE: dict[str, str] = {
    "essai": "essai.html.j2",
    "recu_versement": "recu.html.j2",
    "recu_retrait": "recu.html.j2",
    "recu_placement_dat": "recu.html.j2",
    "recu_denouement_dat": "recu.html.j2",
    "recu_retrait_anticipe_dat": "recu.html.j2",
    "bon_dat": "bon_dat.html.j2",
}


class DocumentIntrouvableError(Exception):
    """Document inexistant OU hors du périmètre de l'acteur (jamais distingués : pas d'IDOR)."""


class RegenerationImpossibleError(Exception):
    """Seul un document en échec se régénère."""


class FichierIndisponibleError(Exception):
    """Document sans fichier (génération échouée) ou fichier absent du disque."""


class ArchiveAltereeError(Exception):
    """L'empreinte du fichier sur disque ne correspond plus à celle de l'index."""


@dataclass(frozen=True)
class DemandeDocument:
    """Tout ce qu'il faut pour générer, archiver et indexer UN document."""

    type: str
    module: str
    cle_naturelle: str  # identité dans son type : rend la génération idempotente
    journee: date  # journée comptable : date le dossier ET le contenu
    agency_id: uuid.UUID
    nom: str  # nom de fichier sans extension (assaini à l'écriture)
    contexte: dict[str, Any]  # JSON-sérialisable : variables du gabarit, conservées à l'index
    numero_piece: str | None = None
    objet_lie_type: str | None = None
    objet_lie_id: uuid.UUID | None = None
    acteur_id: uuid.UUID | None = None


def _sha256(contenu: bytes) -> str:
    return hashlib.sha256(contenu).hexdigest()


def trouver_ok(db: Session, type_document: str, cle_naturelle: str) -> Archive | None:
    return db.execute(
        select(Archive).where(
            Archive.type == type_document,
            Archive.cle_naturelle == cle_naturelle,
            Archive.statut == STATUT_OK,
        )
    ).scalar_one_or_none()


def _agence(db: Session, agency_id: uuid.UUID) -> Agency:
    agence = db.get(Agency, agency_id)
    if agence is None:
        raise ValueError(f"Agence introuvable : {agency_id}")
    return agence


def _institution() -> dict[str, Any]:
    """L'en-tête de l'institution, lu dans la config du déploiement (IMF_*). Sans nom configuré :
    bandeau provisoire sur le document, jamais un nom inventé."""
    return {
        "nom": settings.IMF_NOM,
        "adresse": settings.IMF_ADRESSE,
        "telephone": settings.IMF_TELEPHONE,
        "numero_agrement": settings.IMF_NUMERO_AGREMENT,
        "provisoire": not settings.IMF_NOM,
    }


def _rendre(db: Session, demande: DemandeDocument) -> bytes:
    gabarit = GABARIT_DU_TYPE.get(demande.type)
    if gabarit is None:
        raise ValueError(f"Type de document inconnu : {demande.type}")
    agence = _agence(db, demande.agency_id)
    contexte = {
        **demande.contexte,
        "institution": _institution(),
        "agence": {"code": agence.code, "nom": agence.name},
    }
    return rendre_pdfa(gabarit, contexte, demande.journee)


def _chemin(db: Session, demande: DemandeDocument) -> str:
    agence = _agence(db, demande.agency_id)
    return stockage.chemin_relatif(agence.code, demande.journee, demande.module, demande.nom)


def _ligne(
    demande: DemandeDocument, relatif: str, statut: str, **champs: Any
) -> Archive:
    return Archive(
        type=demande.type,
        module=demande.module,
        cle_naturelle=demande.cle_naturelle,
        journee_comptable=demande.journee,
        chemin_relatif=relatif,
        numero_piece=demande.numero_piece,
        objet_lie_type=demande.objet_lie_type,
        objet_lie_id=demande.objet_lie_id,
        agency_id=demande.agency_id,
        acteur_id=demande.acteur_id,
        contexte=demande.contexte,
        statut=statut,
        **champs,
    )


def archiver(
    db: Session, demande: DemandeDocument, *, contexte_requete: ContexteRequete = CONTEXTE_VIDE
) -> Archive:
    """Génère, écrit et indexe le document ; retourne le document EXISTANT si la clé naturelle a
    déjà un document `ok` (aucune réécriture). LÈVE en cas d'échec : l'appelant décide —
    `generer_apres_commit` trace l'échec sans jamais remonter l'erreur. Flush seulement."""
    existant = trouver_ok(db, demande.type, demande.cle_naturelle)
    if existant is not None:
        return existant

    pdf = _rendre(db, demande)
    relatif = _chemin(db, demande)
    base = settings.repertoire_documents
    try:
        stockage.ecrire_sans_ecraser(base, relatif, pdf)
        contenu = pdf
    except stockage.FichierExistantError:
        # Fichier déjà présent sans ligne `ok` (arrêt entre l'écriture et l'index, ou course) :
        # on l'ADOPTE tel quel — jamais écrasé, l'index en reflète l'empreinte réelle.
        contenu = stockage.chemin_absolu(base, relatif).read_bytes()

    archive = _ligne(
        demande, relatif, STATUT_OK, sha256=_sha256(contenu), taille=len(contenu)
    )
    db.add(archive)
    db.flush()
    ecrire_audit(
        db,
        action=ACTION_CREE,
        contexte=contexte_requete,
        acteur_id=demande.acteur_id,
        resource_type=RESSOURCE,
        resource_id=archive.id,
        agency_id=demande.agency_id,
        new_values={
            "type": demande.type,
            "numero_piece": demande.numero_piece,
            "journee_comptable": demande.journee.isoformat(),
            "sha256": archive.sha256,
        },
    )
    return archive


def generer_apres_commit(
    db: Session, demande: DemandeDocument, *, contexte_requete: ContexteRequete = CONTEXTE_VIDE
) -> Archive:
    """À appeler APRÈS le commit de l'opération métier. Ne lève jamais : un échec (rendu,
    bibliothèques natives, disque) pose une ligne d'index `echec` et la requête métier, déjà
    validée, reste intacte. Commit lui-même (index + audit)."""
    try:
        archive = archiver(db, demande, contexte_requete=contexte_requete)
        db.commit()
        return archive
    except IntegrityError:
        # Course : un autre processus a archivé la même clé entre-temps. Le document existe.
        db.rollback()
        existant = trouver_ok(db, demande.type, demande.cle_naturelle)
        if existant is not None:
            return existant
        return _tracer_echec(db, demande, "Conflit d'unicité inattendu à l'archivage")
    except Exception as erreur:
        db.rollback()
        journal.exception(
            "Échec de génération du document %s %s", demande.type, demande.cle_naturelle
        )
        return _tracer_echec(db, demande, f"{type(erreur).__name__}: {erreur}")


def _tracer_echec(db: Session, demande: DemandeDocument, message: str) -> Archive:
    try:
        relatif = _chemin(db, demande)
    except Exception:
        relatif = f"echec/{uuid.uuid4().hex}.pdf"
    ligne = _ligne(demande, relatif, STATUT_ECHEC, erreur=message[:2000])
    db.add(ligne)
    db.commit()
    return ligne


PERMISSION_AGENCE = "documents.read.agence"


def _perimetre(courant: UtilisateurCourant) -> ColumnElement[bool]:
    """Ce que l'acteur peut voir. `documents.read.agence` : tout le périmètre (agence, ou réseau
    pour les rôles à portée réseau). `documents.read` SEUL (le caissier) : ses PROPRES documents
    seulement (`acteur_id`), toujours dans son agence — un caissier ne voit jamais ceux d'un
    collègue."""
    # `condition_perimetre` attend une colonne NULLABLE ; `agency_id` ne l'est pas (invariance de
    # ColumnElement) : le cast est sûr, la condition SQL produite est la même.
    colonne = cast("ColumnElement[uuid.UUID | None]", Archive.agency_id)
    dans_le_perimetre = courant.condition_perimetre(colonne)
    if PERMISSION_AGENCE in courant.permissions:
        return dans_le_perimetre
    return and_(dans_le_perimetre, Archive.acteur_id == courant.user_id)


def _charger(db: Session, courant: UtilisateurCourant, archive_id: uuid.UUID) -> Archive:
    """Le document dans le périmètre de l'acteur, sinon 404 (jamais 403 : un document hors
    périmètre n'existe pas de son point de vue)."""
    archive = db.execute(
        select(Archive).where(Archive.id == archive_id, _perimetre(courant))
    ).scalar_one_or_none()
    if archive is None:
        raise DocumentIntrouvableError()
    return archive


@dataclass(frozen=True)
class FiltresDocuments:
    journee: date | None = None
    module: str | None = None
    type: str | None = None
    agency_id: uuid.UUID | None = None
    numero_piece: str | None = None
    statut: str | None = None


def lister(
    db: Session,
    courant: UtilisateurCourant,
    filtres: FiltresDocuments,
    *,
    page: int,
    taille: int,
) -> tuple[list[Archive], int]:
    """Page de documents du périmètre, du plus récent au plus ancien. Pagination SERVEUR."""
    taille = max(1, min(taille, TAILLE_PAGE_MAX))
    conditions = [_perimetre(courant)]
    if filtres.journee is not None:
        conditions.append(Archive.journee_comptable == filtres.journee)
    if filtres.module is not None:
        conditions.append(Archive.module == filtres.module)
    if filtres.type is not None:
        conditions.append(Archive.type == filtres.type)
    if filtres.agency_id is not None:
        conditions.append(Archive.agency_id == filtres.agency_id)
    if filtres.numero_piece is not None:
        conditions.append(Archive.numero_piece == filtres.numero_piece)
    if filtres.statut is not None:
        conditions.append(Archive.statut == filtres.statut)
    total = db.execute(select(func.count()).select_from(Archive).where(*conditions)).scalar_one()
    lignes = (
        db.execute(
            select(Archive)
            .where(*conditions)
            .order_by(Archive.journee_comptable.desc(), Archive.cree_le.desc(), Archive.id)
            .offset((max(page, 1) - 1) * taille)
            .limit(taille)
        )
        .scalars()
        .all()
    )
    return list(lignes), total


def lire_fichier(
    db: Session,
    courant: UtilisateurCourant,
    archive_id: uuid.UUID,
    *,
    contexte_requete: ContexteRequete = CONTEXTE_VIDE,
) -> tuple[Archive, bytes]:
    """Contenu du PDF archivé, après contrôle d'accès à l'objet ET de l'empreinte. Une ligne
    d'AUDIT par téléchargement (consulter une pièce légale est un acte à tracer)."""
    archive = _charger(db, courant, archive_id)
    if archive.statut != STATUT_OK or archive.sha256 is None:
        raise FichierIndisponibleError("Ce document n'a pas pu être généré : régénérez-le.")
    chemin = stockage.chemin_absolu(settings.repertoire_documents, archive.chemin_relatif)
    if not chemin.is_file():
        raise FichierIndisponibleError("Le fichier de ce document est introuvable sur le serveur.")
    contenu = chemin.read_bytes()
    if _sha256(contenu) != archive.sha256:
        ecrire_audit(
            db,
            action="documents.archive.alteree",
            contexte=contexte_requete,
            acteur_id=courant.user_id,
            resource_type=RESSOURCE,
            resource_id=archive.id,
            agency_id=archive.agency_id,
            new_values={"sha256_index": archive.sha256},
        )
        db.commit()
        raise ArchiveAltereeError("L'empreinte du fichier ne correspond plus à l'index.")
    ecrire_audit(
        db,
        action=ACTION_TELECHARGE,
        contexte=contexte_requete,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=archive.id,
        agency_id=archive.agency_id,
        new_values={
            "type": archive.type,
            "numero_piece": archive.numero_piece,
            "sha256": archive.sha256,
        },
    )
    db.commit()
    return archive, contenu


def nom_de_fichier(archive: Archive) -> str:
    return Path(archive.chemin_relatif).name


def regenerer(
    db: Session,
    courant: UtilisateurCourant,
    archive_id: uuid.UUID,
    *,
    contexte_requete: ContexteRequete = CONTEXTE_VIDE,
) -> Archive:
    """Rattrape un document en échec : génère à partir des données conservées à l'index et pose
    une NOUVELLE ligne `ok`. Idempotent : si la clé a déjà un document `ok`, il est rendu tel quel.
    Refuse un document qui n'est pas en échec."""
    archive = _charger(db, courant, archive_id)
    if archive.statut != STATUT_ECHEC:
        raise RegenerationImpossibleError(
            "Seul un document dont la génération a échoué peut être régénéré."
        )
    existant = trouver_ok(db, archive.type, archive.cle_naturelle)
    if existant is not None:
        return existant
    demande = DemandeDocument(
        type=archive.type,
        module=archive.module,
        cle_naturelle=archive.cle_naturelle,
        journee=archive.journee_comptable,
        agency_id=archive.agency_id,
        nom=Path(archive.chemin_relatif).stem,
        contexte=archive.contexte,
        numero_piece=archive.numero_piece,
        objet_lie_type=archive.objet_lie_type,
        objet_lie_id=archive.objet_lie_id,
        # Le PROPRIÉTAIRE d'origine : régénérer le document d'un caissier ne le lui retire pas.
        acteur_id=archive.acteur_id,
    )
    return generer_apres_commit(db, demande, contexte_requete=contexte_requete)
