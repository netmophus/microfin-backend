"""Seed du cadre comptable : les journaux standard (données PROVISOIRES) et l'ouverture
d'un exercice.

Les journaux sont des DONNÉES (comme le plan de comptes) : livrés provisoires, à valider et
compléter par le comptable, sans redéploiement. Idempotent (upsert par code).

L'ouverture d'exercice est une OPÉRATION (dates propres à l'IMF), séparée du seed des journaux :
`ouvrir_exercice` crée un exercice « ouvert ». La contrainte d'exclusion (0016) empêche tout
chevauchement — donc deux exercices ouverts ne peuvent pas se recouvrir.
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.comptabilite.models import Exercice


@dataclass(frozen=True)
class JournalStandard:
    code: str
    name: str
    type: str


# Journaux minimaux d'une IMF. PROVISOIRES : le comptable les valide/complète.
JOURNAUX: tuple[JournalStandard, ...] = (
    JournalStandard("CA", "Journal de caisse", "tresorerie"),
    JournalStandard("BQ", "Journal de banque", "tresorerie"),
    JournalStandard("OD", "Opérations diverses", "operations_diverses"),
    JournalStandard("AN", "À-nouveaux", "a_nouveaux"),
)


_UPSERT_JOURNAL = text(
    """
    INSERT INTO comptabilite.journals (code, name, type, is_provisional, is_system)
    VALUES (:code, :name, :type, TRUE, FALSE)
    ON CONFLICT (code) DO UPDATE SET
        name       = EXCLUDED.name,
        type       = EXCLUDED.type,
        updated_at = NOW()
    """
)


def executer_seed_journaux(db: Session) -> int:
    """Installe/actualise les journaux standard (provisoires). Ne committe pas."""
    for journal in JOURNAUX:
        db.execute(
            _UPSERT_JOURNAL,
            {"code": journal.code, "name": journal.name, "type": journal.type},
        )
    return len(JOURNAUX)


class ExerciceChevauchantError(Exception):
    """Un exercice existant recouvre déjà la période demandée."""


def ouvrir_exercice(
    db: Session, *, code: str, label: str, date_debut: date, date_fin: date
) -> Exercice:
    """Crée un exercice OUVERT. Ne committe pas. La non-superposition est garantie par la base."""
    exercice = Exercice(
        code=code,
        label=label,
        date_debut=date_debut,
        date_fin=date_fin,
        status="ouvert",
    )
    db.add(exercice)
    db.flush()
    return exercice
