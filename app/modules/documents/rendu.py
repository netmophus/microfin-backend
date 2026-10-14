"""Rendu HTML + CSS -> PDF/A avec WeasyPrint, gabarits Jinja.

Polices LIBRES embarquées (DejaVu, y compris la chasse fixe), aucune police générique : le CSS
explicite tout. Aucune horloge dans le contenu : la date affichée est la journée comptable, et les
métadonnées du PDF portent cette même date.

WeasyPrint dépend de bibliothèques natives (Pango). Linux/Docker : `apt install libpango-1.0-0
libpangoft2-1.0-0` (voir Dockerfile). Windows de dev : MSYS2 + WEASYPRINT_DLL_DIRECTORIES (voir
docs/runbook-documents.md). L'import est PARESSEUX : l'application démarre même sans Pango, et
l'échec ne se produit qu'à la génération, où il est tracé (statut « echec ») au lieu de planter.
"""

import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from app.modules.documents.libelles import EXEMPLAIRES, LIBELLES
from app.modules.documents.montant_en_lettres import montant_en_lettres

DOSSIER_GABARITS = Path(__file__).resolve().parent / "gabarits"
VARIANTE_PDFA = "pdf/a-2b"
DLL_MSYS2_DEV = r"C:\msys64\mingw64\bin"


def formater_jour(jour: date) -> str:
    """JJ/MM/AAAA, sans passer par l'horloge ni les paramètres régionaux."""
    return f"{jour.day:02d}/{jour.month:02d}/{jour.year:04d}"


def _environnement() -> Environment:
    environnement = Environment(
        loader=FileSystemLoader(DOSSIER_GABARITS),
        autoescape=select_autoescape(["html", "j2"]),
        undefined=StrictUndefined,  # une variable oubliée est une erreur, pas un trou muet
    )
    environnement.filters["montant_en_lettres"] = montant_en_lettres
    return environnement


def rendre_html(gabarit: str, contexte: dict[str, Any], journee: date) -> str:
    """HTML d'un gabarit : le contexte du document + le socle commun (libellés, deux exemplaires,
    CSS, journée comptable)."""
    complet = {
        "css": (DOSSIER_GABARITS / "style.css").read_text(encoding="utf-8"),
        "libelles": LIBELLES,
        "exemplaires": EXEMPLAIRES,
        "journee_iso": journee.isoformat(),
        "journee_affichee": formater_jour(journee),
        "duplicata": False,
        "brouillon": False,
        **contexte,
    }
    return _environnement().get_template(gabarit).render(**complet)


def _preparer_bibliotheques_natives() -> None:
    """Windows de dev uniquement : indique où MSYS2 a installé Pango, sauf réglage explicite."""
    if sys.platform == "win32" and Path(DLL_MSYS2_DEV).is_dir():
        os.environ.setdefault("WEASYPRINT_DLL_DIRECTORIES", DLL_MSYS2_DEV)


def html_vers_pdfa(html: str) -> bytes:
    """PDF/A-2b du HTML. Lève si WeasyPrint ou Pango sont indisponibles (l'appelant trace
    l'échec)."""
    _preparer_bibliotheques_natives()
    from weasyprint import HTML  # import paresseux : voir l'en-tête du module

    pdf = HTML(string=html, base_url=DOSSIER_GABARITS.as_uri() + "/").write_pdf(
        pdf_variant=VARIANTE_PDFA
    )
    if pdf is None:
        raise RuntimeError("WeasyPrint n'a produit aucun contenu")
    return bytes(pdf)


def rendre_pdfa(gabarit: str, contexte: dict[str, Any], journee: date) -> bytes:
    return html_vers_pdfa(rendre_html(gabarit, contexte, journee))
