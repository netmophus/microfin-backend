"""Paramètres d'institution du module Caisse (CA2, migration 0043) — le seuil de tolérance sur
l'écart, comparé à `abs(ecart)` dans `service.py::fermer_session`.

Distinct de `service.py` (le cœur TRANSACTIONNEL des sessions) : ici, c'est la LECTURE et
l'ÉCRITURE du PARAMÈTRE lui-même, depuis l'écran du comptable — même séparation que
`tiers/parts_parametres.py` vis-à-vis de `tiers/parts.py`.

La ligne est UNIQUE par construction (migration 0043, colonne `singleton` CHECK+UNIQUE, posée
DÈS LA CRÉATION — pas en retrofit comme `share_parameters`) : ce module ne crée JAMAIS de ligne,
il lit/modifie la seule qui existe (posée par le seed `seed-comptabilite`, idempotent).

CA3 (migration séparée) ajoutera les rattachements comptables de l'écart
(compte_ecart_manquant_id/compte_ecart_excedent_id) à cette même ligne — rien ici ne les
anticipe."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.caisse.models import CaisseParametres

RESSOURCE = "caisse.parametres"


class ParametrageManquantError(Exception):
    """Aucune ligne de configuration — l'IMF doit d'abord lancer `seed-comptabilite`."""


def lire(db: Session) -> CaisseParametres:
    config = db.execute(select(CaisseParametres).limit(1)).scalar_one_or_none()
    if config is None:
        raise ParametrageManquantError(
            "Le seuil de tolérance de caisse n'est pas paramétré : contactez votre "
            "administrateur."
        )
    return config


def modifier(
    db: Session,
    config: CaisseParametres,
    *,
    seuil_tolerance: int,
    motif: str,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> CaisseParametres:
    """Modifie le seuil — MOTIF obligatoire, tracé avant/après. La borne (montant positif) est
    déjà imposée par le schéma Pydantic ; rien d'autre à vérifier ici (pas de compte à ce stade,
    CA2 seul)."""
    avant = {"seuil_tolerance": config.seuil_tolerance}

    config.seuil_tolerance = seuil_tolerance
    config.updated_by = par
    db.flush()

    ecrire_audit(
        db,
        action="caisse.parametres.updated",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=config.id,
        old_values=avant,
        new_values={"seuil_tolerance": seuil_tolerance, "motif": motif},
    )
    return config
