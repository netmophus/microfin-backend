"""Rattachement comptable de la caisse par agence (Bloc 5 du paramétrage comptable).

Le compte de caisse (5721 provisoire) est la face COMPTABLE de l'agence — pas le vrai module
Caisse (guichets, arrêtés, dénombrement, écarts), qui reste à venir.

GARDE-FOU : le compte soumis passe par `comptes.compte_saisie_actif`, qui refuse tout compte de
regroupement ou désactivé — même soumis directement à l'API, en contournant le sélecteur.

Aucun impact sur le passé : les écritures déjà posées référencent directement un compte
concret (`journal_lines.account_id`, figé à la pose), jamais ce paramètre. Changer le
rattachement ne peut affecter que les PROCHAINES opérations, par construction.
"""

import uuid

from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.comptabilite.comptes import compte_saisie_actif
from app.modules.comptabilite.models import Account
from app.modules.parameters.models import Agency

RESSOURCE = "parameters.agency"


def modifier_compte_caisse(
    db: Session,
    agence: Agency,
    *,
    compte_caisse_number: str | None,
    motif: str,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> Agency:
    """Re-pointe le compte de caisse — MOTIF obligatoire, tracé avant/après. Vider le
    rattachement (None) est une action LÉGITIME, pas une erreur."""
    nouveau = compte_saisie_actif(db, compte_caisse_number) if compte_caisse_number else None

    avant_numero = None
    if agence.compte_caisse_id is not None:
        avant = db.get(Account, agence.compte_caisse_id)
        avant_numero = avant.account_number if avant else None

    agence.compte_caisse_id = nouveau.id if nouveau else None
    agence.updated_by = par
    db.flush()

    ecrire_audit(
        db,
        action="parameters.agency.compte_caisse_updated",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=agence.id,
        old_values={"compte_caisse": avant_numero},
        new_values={"compte_caisse": compte_caisse_number, "motif": motif},
    )
    return agence
