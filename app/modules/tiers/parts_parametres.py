"""Paramètres d'institution des parts sociales (Bloc 5 du paramétrage comptable).

Distinct de `parts.py` (le cœur TRANSACTIONNEL) : ici, c'est la LECTURE et l'ÉCRITURE du
PARAMÈTRE lui-même, depuis l'écran du comptable — valeur d'une part, minimum pour adhérer,
remboursable, moment de l'adhésion, rattachements 1021/1022.

GARDE-FOU sur les comptes : chaque numéro soumis passe par `comptes.compte_saisie_actif`
(comptabilite), qui refuse tout compte de regroupement ou désactivé — même soumis directement
à l'API, en contournant le sélecteur.

La ligne est UNIQUE par construction (migration 0029, colonne `singleton` CHECK+UNIQUE) : ce
module ne crée JAMAIS de ligne, il lit/modifie la seule qui existe (posée par le seed
`seed-comptabilite`, idempotent).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.comptabilite.comptes import compte_saisie_actif
from app.modules.comptabilite.models import Account
from app.modules.tiers.models import ShareParameters

RESSOURCE = "tiers.share_parameters"


class ParametrageManquantError(Exception):
    """Aucune ligne de configuration — l'IMF doit d'abord lancer `seed-comptabilite`."""


def lire(db: Session) -> ShareParameters:
    config = db.execute(select(ShareParameters).limit(1)).scalar_one_or_none()
    if config is None:
        raise ParametrageManquantError(
            "Les parts sociales ne sont pas paramétrées : contactez votre administrateur."
        )
    return config


def modifier(
    db: Session,
    config: ShareParameters,
    *,
    unit_value: int,
    minimum_shares: int,
    is_refundable: bool,
    membership_on: str,
    compte_parts_liberees_number: str | None,
    compte_parts_non_liberees_number: str | None,
    motif: str,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> ShareParameters:
    """Modifie la config — MOTIF obligatoire, tracé avant/après. Les bornes de valeur
    (montants positifs, moment d'adhésion valide) sont déjà imposées par le schéma Pydantic ;
    seul le garde-fou des comptes reste à vérifier ici."""
    nouveau_liberees = (
        compte_saisie_actif(db, compte_parts_liberees_number)
        if compte_parts_liberees_number
        else None
    )
    nouveau_non_liberees = (
        compte_saisie_actif(db, compte_parts_non_liberees_number)
        if compte_parts_non_liberees_number
        else None
    )

    def _numero(account_id: uuid.UUID | None) -> str | None:
        if account_id is None:
            return None
        compte = db.get(Account, account_id)
        return compte.account_number if compte else None

    avant = {
        "unit_value": config.unit_value,
        "minimum_shares": config.minimum_shares,
        "is_refundable": config.is_refundable,
        "membership_on": config.membership_on,
        "compte_parts_liberees": _numero(config.compte_parts_liberees_id),
        "compte_parts_non_liberees": _numero(config.compte_parts_non_liberees_id),
    }

    config.unit_value = unit_value
    config.minimum_shares = minimum_shares
    config.is_refundable = is_refundable
    config.membership_on = membership_on
    config.compte_parts_liberees_id = nouveau_liberees.id if nouveau_liberees else None
    config.compte_parts_non_liberees_id = (
        nouveau_non_liberees.id if nouveau_non_liberees else None
    )
    config.updated_by = par
    db.flush()

    ecrire_audit(
        db,
        action="tiers.share_parameters.updated",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=config.id,
        old_values=avant,
        new_values={
            "unit_value": unit_value,
            "minimum_shares": minimum_shares,
            "is_refundable": is_refundable,
            "membership_on": membership_on,
            "compte_parts_liberees": compte_parts_liberees_number,
            "compte_parts_non_liberees": compte_parts_non_liberees_number,
            "motif": motif,
        },
    )
    return config
