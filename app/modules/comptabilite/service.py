"""Gestion courante du plan de comptes — les garde-fous du plan.

Aucune SUPPRESSION : un compte ne se supprime pas, il se DÉSACTIVE (is_active = False). Les
garde-fous, chacun destiné à être vu MORDRE par un test :

  - compte SYSTÈME : son sens (D/C) et son caractère saisie/regroupement (is_posting) sont
    VERROUILLÉS — c'est le plan de référence, l'IMF ne le déforme pas ;
  - compte MOUVEMENTÉ (portant des écritures) : ni changement de sens, ni désactivation — sinon
    on romprait la cohérence d'écritures déjà passées ;
  - compte à ENFANTS ACTIFS : pas de désactivation — on ne coupe pas une branche de l'arbre
    au-dessus de comptes encore vivants.

« Mouvementé » se lit dans journal_lines (table du bloc C2). Pour que le garde-fou soit
TESTABLE et qu'il MORDE avant même l'existence des écritures, la vérification d'usage est
INJECTÉE (paramètre `est_mouvemente`). La version de production (`compte_a_des_ecritures`)
interroge journal_lines si la table existe, et renvoie False sinon : le garde-fou est câblé
dès maintenant, il devient effectif dès que C2 crée la table — sans changer une ligne ici.
"""

import uuid
from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.comptabilite.models import Account

# Une fonction qui dit si un compte porte des écritures. Injectée pour la testabilité.
EstMouvemente = Callable[[uuid.UUID], bool]


class ModificationInterditeError(Exception):
    """Base des refus de garde-fou sur le plan de comptes."""


class CompteSystemeError(ModificationInterditeError):
    """Tentative de déformer un compte du plan de référence (sens / is_posting)."""


class CompteMouvementeError(ModificationInterditeError):
    """Tentative de toucher (sens/désactivation) un compte portant des écritures."""


class CompteAvecEnfantsActifsError(ModificationInterditeError):
    """Tentative de désactiver un compte qui a encore des enfants actifs."""


def compte_a_des_ecritures(db: Session, account_id: uuid.UUID) -> bool:
    """Vrai si le compte porte au moins une ligne d'écriture.

    Tant que journal_lines n'existe pas (avant C2), renvoie False : honnête sur le fait que le
    garde-fou est INERTE aujourd'hui, mais déjà branché. Dès que C2 crée la table, il MORD.
    """
    existe = db.execute(
        text("SELECT to_regclass('comptabilite.journal_lines') IS NOT NULL")
    ).scalar_one()
    if not existe:
        return False
    return db.execute(
        text("SELECT EXISTS(SELECT 1 FROM comptabilite.journal_lines WHERE account_id = :a)"),
        {"a": account_id},
    ).scalar_one()


def _a_des_enfants_actifs(db: Session, account_id: uuid.UUID) -> bool:
    return db.execute(
        text(
            "SELECT EXISTS(SELECT 1 FROM comptabilite.accounts "
            "WHERE parent_id = :p AND is_active)"
        ),
        {"p": account_id},
    ).scalar_one()


def modifier_sens(
    db: Session,
    compte: Account,
    nouveau_sens: str,
    par: uuid.UUID | None,
    *,
    est_mouvemente: EstMouvemente,
) -> None:
    """Change le sens normal d'un compte. Refusé si système, ou si le compte est mouvementé."""
    if compte.is_system:
        raise CompteSystemeError(
            f"compte système {compte.account_number} : sens verrouillé"
        )
    if est_mouvemente(compte.id):
        raise CompteMouvementeError(
            f"compte {compte.account_number} mouvementé : changement de sens refusé"
        )
    compte.normal_side = nouveau_sens
    compte.updated_by = par
    db.flush()


def desactiver(
    db: Session,
    compte: Account,
    par: uuid.UUID | None,
    *,
    est_mouvemente: EstMouvemente,
) -> None:
    """Désactive un compte (soft delete). Refusé s'il est mouvementé ou a des enfants actifs."""
    if est_mouvemente(compte.id):
        raise CompteMouvementeError(
            f"compte {compte.account_number} mouvementé : désactivation refusée"
        )
    if _a_des_enfants_actifs(db, compte.id):
        raise CompteAvecEnfantsActifsError(
            f"compte {compte.account_number} : des enfants actifs, désactivation refusée"
        )
    compte.is_active = False
    compte.updated_by = par
    db.flush()
