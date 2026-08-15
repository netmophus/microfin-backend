"""Paramètres d'institution du module Caisse — le seuil de tolérance sur l'écart (CA2,
migration 0043), comparé à `abs(ecart)` dans `service.py::fermer_session` ; le rattachement
comptable de l'écart (CA3, migration 0044), lu par `ecart_operations.poser_ecriture_ecart`.

Distinct de `service.py` (le cœur TRANSACTIONNEL des sessions) : ici, c'est la LECTURE et
l'ÉCRITURE du PARAMÈTRE lui-même, depuis l'écran du comptable — même séparation que
`tiers/parts_parametres.py` vis-à-vis de `tiers/parts.py`.

La ligne est UNIQUE par construction (migration 0043, colonne `singleton` CHECK+UNIQUE, posée
DÈS LA CRÉATION — pas en retrofit comme `share_parameters`) : ce module ne crée JAMAIS de ligne,
il lit/modifie la seule qui existe (posée par le seed `seed-comptabilite`, idempotent).

GARDE-FOU sur les comptes (CA3) : chaque numéro soumis passe par `comptes.compte_saisie_actif`
(comptabilite), qui refuse tout compte de regroupement ou désactivé — même discipline que les
rattachements produit/agence/parts déjà en place. DEUX comptes distincts (manquant/excédent),
jamais un signe négatif sur un seul (décision actée)."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.caisse.models import CaisseParametres
from app.modules.comptabilite.comptes import compte_saisie_actif
from app.modules.comptabilite.models import Account

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
    compte_ecart_manquant_number: str | None,
    compte_ecart_excedent_number: str | None,
    motif: str,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> CaisseParametres:
    """Modifie le seuil et/ou les rattachements de l'écart — MOTIF obligatoire, tracé
    avant/après. La borne du seuil (montant positif) est déjà imposée par le schéma Pydantic ;
    le garde-fou des comptes reste à vérifier ici (compte_saisie_actif)."""
    nouveau_manquant = (
        compte_saisie_actif(db, compte_ecart_manquant_number)
        if compte_ecart_manquant_number
        else None
    )
    nouveau_excedent = (
        compte_saisie_actif(db, compte_ecart_excedent_number)
        if compte_ecart_excedent_number
        else None
    )

    def _numero(account_id: uuid.UUID | None) -> str | None:
        if account_id is None:
            return None
        compte = db.get(Account, account_id)
        return compte.account_number if compte else None

    avant = {
        "seuil_tolerance": config.seuil_tolerance,
        "compte_ecart_manquant": _numero(config.compte_ecart_manquant_id),
        "compte_ecart_excedent": _numero(config.compte_ecart_excedent_id),
    }

    config.seuil_tolerance = seuil_tolerance
    config.compte_ecart_manquant_id = nouveau_manquant.id if nouveau_manquant else None
    config.compte_ecart_excedent_id = nouveau_excedent.id if nouveau_excedent else None
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
        new_values={
            "seuil_tolerance": seuil_tolerance,
            "compte_ecart_manquant": compte_ecart_manquant_number,
            "compte_ecart_excedent": compte_ecart_excedent_number,
            "motif": motif,
        },
    )
    return config
