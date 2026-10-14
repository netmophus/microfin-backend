"""Stockage des documents sur disque : chemins datés par la JOURNÉE COMPTABLE, noms assainis,
écriture UNIQUE (jamais d'écrasement).

Arborescence : <base>/<code_agence>/AAAA/MM/JJ/<module>/<nom>.pdf — la date est celle de la
journée comptable de l'opération, jamais celle de l'horloge. L'archive est conservée sans limite
de durée : ce module n'offre AUCUNE suppression.
"""

import logging
import os
import re
import uuid
from datetime import date
from pathlib import Path

journal = logging.getLogger(__name__)

CARACTERES_INTERDITS = re.compile(r"[^A-Za-z0-9._-]")
POINTS_CONSECUTIFS = re.compile(r"\.{2,}")
LONGUEUR_MAX_COMPOSANT = 120


class FichierExistantError(Exception):
    """Le fichier de destination existe déjà : on n'écrase jamais un document archivé."""


class ComposantInvalideError(ValueError):
    """Un composant de chemin ou de nom devient vide une fois assaini."""


def assainir(composant: str) -> str:
    """Liste blanche de caractères (lettres, chiffres, point, tiret, souligné) : tout le reste
    devient « _ ». Jamais de « .. » ni de point initial : aucune évasion du dossier d'archive."""
    propre = CARACTERES_INTERDITS.sub("_", composant.strip())
    propre = POINTS_CONSECUTIFS.sub("_", propre).lstrip(".")[:LONGUEUR_MAX_COMPOSANT]
    if not propre or propre.strip("_") == "":
        raise ComposantInvalideError(f"Composant de chemin invalide : {composant!r}")
    return propre


def chemin_relatif(code_agence: str, journee: date, module: str, nom: str) -> str:
    """`<agence>/AAAA/MM/JJ/<module>/<nom>.pdf`, composants assainis, séparateur « / »."""
    return "/".join(
        [
            assainir(code_agence),
            f"{journee.year:04d}",
            f"{journee.month:02d}",
            f"{journee.day:02d}",
            assainir(module),
            f"{assainir(nom)}.pdf",
        ]
    )


def chemin_absolu(base: Path, relatif: str) -> Path:
    """Résout un chemin relatif SOUS la base ; refuse toute sortie du dossier d'archive."""
    destination = (base / relatif).resolve()
    if not destination.is_relative_to(base.resolve()):
        raise ComposantInvalideError(f"Chemin hors de l'archive : {relatif!r}")
    return destination


def ecrire_sans_ecraser(base: Path, relatif: str, contenu: bytes) -> Path:
    """Écrit `contenu` sous un nom temporaire, puis le LIE à sa destination : la liaison échoue si
    le nom existe (`FichierExistantError`) — le fichier existant n'est jamais touché. Le fichier
    temporaire est retiré dans tous les cas."""
    destination = chemin_absolu(base, relatif)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporaire = destination.parent / f".tmp-{uuid.uuid4().hex}"
    try:
        temporaire.write_bytes(contenu)
        try:
            os.link(temporaire, destination)
        except FileExistsError:
            raise FichierExistantError(relatif) from None
    finally:
        temporaire.unlink(missing_ok=True)
    return destination


def verifier_repertoire_pret(base: Path) -> str | None:
    """None si le dossier d'archive existe et est inscriptible, sinon la raison — SANS le chemin :
    elle part sur une route publique (/health/ready), le chemin est journalisé côté serveur."""
    if not base.is_dir():
        journal.error("Dossier d'archivage des documents absent : %s", base)
        return "Le dossier d'archivage des documents n'existe pas."
    if not os.access(base, os.W_OK | os.X_OK):
        journal.error("Dossier d'archivage des documents non inscriptible : %s", base)
        return "Le dossier d'archivage des documents n'est pas inscriptible."
    return None
