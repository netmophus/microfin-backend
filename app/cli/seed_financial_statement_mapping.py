"""Seed du mapping comptes -> postes d'états financiers (chantier P1, lot c).

Lit `docs/reference/plan_comptable_enrichi.csv` (une ligne par compte : account_number, name,
class, normal_side, is_posting, etat, masse, poste_libelle — colonnes de référence name/class/
normal_side/is_posting NON utilisées ici, elles ne servent qu'à l'expert pour relire le fichier
en contexte) et peuple `comptabilite.financial_statement_mapping`.

NON DESTRUCTIF : une ligne déjà ajustée à la main (`gere_manuellement = TRUE`, posée par l'écran
d'admin du mapping) n'est plus jamais réécrite par ce seed — même discipline que
`security.roles.gere_manuellement` (voir `seed_security.py`, `_UPSERT_ROLE`).

`poste_ordre` N'EST PAS dans le CSV : dérivé ici, par rang de PREMIÈRE apparition du
`poste_libelle` dans le fichier (déjà trié par `account_number` croissant, donc dans un ordre de
présentation déjà sensé), SÉPARÉMENT par (`etat`, `masse`) — l'ordre d'affichage d'un bilan n'a de
sens que dans sa propre colonne (actif, ou passif), jamais mélangé entre les deux. Les rangs sont
multipliés par 10 (10, 20, 30…) pour laisser de la place à une insertion manuelle future entre
deux postes sans tout renuméroter.
"""

import csv
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.modules.comptabilite.models import Account

CHEMIN_CSV_DEFAUT = (
    Path(__file__).resolve().parents[2] / "docs" / "reference" / "plan_comptable_enrichi.csv"
)

ETATS_VALIDES = {"BILAN", "RESULTAT"}
MASSES_VALIDES = {"ACTIF", "PASSIF", "CONTRA_ACTIF", "CHARGE", "PRODUIT", "MIXTE"}

COLONNES_ATTENDUES = {"account_number", "etat", "masse", "poste_libelle"}


class FichierMappingInvalideError(Exception):
    """Colonne manquante, etat/masse hors liste, ou compte absent du plan de comptes en base."""


@dataclass(frozen=True)
class LigneMapping:
    account_number: str
    etat: str
    masse: str
    poste_libelle: str


def lire_mapping_csv(chemin: Path = CHEMIN_CSV_DEFAUT) -> list[LigneMapping]:
    """Lit et valide le CSV — lève FichierMappingInvalideError à la moindre anomalie de forme
    (colonne manquante, etat/masse hors énumération). Ne touche PAS à la base."""
    with open(chemin, encoding="utf-8-sig", newline="") as f:
        lecteur = csv.DictReader(f, delimiter=";")
        entete = set(lecteur.fieldnames or [])
        manquantes = COLONNES_ATTENDUES - entete
        if manquantes:
            raise FichierMappingInvalideError(
                f"colonnes manquantes : {', '.join(sorted(manquantes))}"
            )

        lignes: list[LigneMapping] = []
        for index, brut in enumerate(lecteur, start=2):  # ligne 1 = en-tête
            numero = (brut.get("account_number") or "").strip()
            if not numero:
                continue
            etat = (brut.get("etat") or "").strip().upper()
            masse = (brut.get("masse") or "").strip().upper()
            poste = (brut.get("poste_libelle") or "").strip()
            if etat not in ETATS_VALIDES:
                raise FichierMappingInvalideError(
                    f"ligne {index} (compte {numero}) : etat « {etat} » invalide "
                    f"(attendu : {', '.join(sorted(ETATS_VALIDES))})"
                )
            if masse not in MASSES_VALIDES:
                raise FichierMappingInvalideError(
                    f"ligne {index} (compte {numero}) : masse « {masse} » invalide "
                    f"(attendu : {', '.join(sorted(MASSES_VALIDES))})"
                )
            if not poste:
                raise FichierMappingInvalideError(
                    f"ligne {index} (compte {numero}) : poste_libelle vide"
                )
            lignes.append(
                LigneMapping(account_number=numero, etat=etat, masse=masse, poste_libelle=poste)
            )
    return lignes


def _ordres_affichage(lignes: list[LigneMapping]) -> dict[tuple[str, str, str], int]:
    """Rang (x10) de première apparition de chaque poste, par (etat, masse)."""
    ordres: dict[tuple[str, str, str], int] = {}
    rang_suivant: dict[tuple[str, str], int] = {}
    for ligne in lignes:
        cle_poste = (ligne.etat, ligne.masse, ligne.poste_libelle)
        if cle_poste in ordres:
            continue
        cle_masse = (ligne.etat, ligne.masse)
        rang_suivant[cle_masse] = rang_suivant.get(cle_masse, 0) + 1
        ordres[cle_poste] = rang_suivant[cle_masse] * 10
    return ordres


_UPSERT = text(
    """
    INSERT INTO comptabilite.financial_statement_mapping
        (account_id, etat, masse, poste_libelle, poste_ordre)
    VALUES
        (:account_id, :etat, :masse, :poste_libelle, :poste_ordre)
    ON CONFLICT (account_id) DO UPDATE SET
        etat          = EXCLUDED.etat,
        masse         = EXCLUDED.masse,
        poste_libelle = EXCLUDED.poste_libelle,
        poste_ordre   = EXCLUDED.poste_ordre,
        updated_at    = NOW()
    WHERE NOT comptabilite.financial_statement_mapping.gere_manuellement
    """
)

_COMPTE_GERES_MANUELLEMENT = text(
    "SELECT count(*) FROM comptabilite.financial_statement_mapping WHERE gere_manuellement"
)


@dataclass(frozen=True)
class RapportSeedMapping:
    nb_lignes_csv: int
    nb_geres_manuellement_ignores: int


def executer_seed_mapping_etats(
    db: Session, chemin: Path = CHEMIN_CSV_DEFAUT
) -> RapportSeedMapping:
    """Convergence NON DESTRUCTIVE : upsert toutes les lignes du CSV, sauf celles déjà ajustées
    à la main (`gere_manuellement = TRUE`), jamais réécrites. Ne committe pas."""
    lignes = lire_mapping_csv(chemin)

    numeros = [ligne.account_number for ligne in lignes]
    comptes = {
        compte.account_number: compte.id
        for compte in db.execute(
            select(Account).where(Account.account_number.in_(numeros))
        ).scalars()
    }
    manquants = sorted(set(numeros) - set(comptes))
    if manquants:
        raise FichierMappingInvalideError(
            f"{len(manquants)} compte(s) du CSV absent(s) du plan de comptes en base : "
            f"{', '.join(manquants)}"
        )

    ordres = _ordres_affichage(lignes)
    for ligne in lignes:
        db.execute(
            _UPSERT,
            {
                "account_id": comptes[ligne.account_number],
                "etat": ligne.etat,
                "masse": ligne.masse,
                "poste_libelle": ligne.poste_libelle,
                "poste_ordre": ordres[(ligne.etat, ligne.masse, ligne.poste_libelle)],
            },
        )
    db.flush()

    nb_geres_manuellement = db.execute(_COMPTE_GERES_MANUELLEMENT).scalar_one()
    return RapportSeedMapping(
        nb_lignes_csv=len(lignes), nb_geres_manuellement_ignores=nb_geres_manuellement
    )
