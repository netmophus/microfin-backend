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


# --- Modèles d'écriture (pont comptable E1), PROVISOIRES ---------------------------------------
@dataclass(frozen=True)
class ModeleEcriture:
    code: str
    label: str
    journal: str  # code du journal où poser la pièce
    lignes: tuple[tuple[str, str], ...]  # (rôle, sens D/C), ordonnées


# Dépôt : D CAISSE / C EPARGNE (la caisse entre, la dette envers le membre monte).
# Retrait : l'inverse. Comptes exacts résolus au moment de l'opération (rôle -> compte).
MODELES: tuple[ModeleEcriture, ...] = (
    ModeleEcriture("epargne.depot", "Dépôt d'épargne", "CA", (("CAISSE", "D"), ("EPARGNE", "C"))),
    ModeleEcriture(
        "epargne.retrait", "Retrait d'épargne", "CA", (("EPARGNE", "D"), ("CAISSE", "C"))
    ),
    # Clôture : mêmes D/C qu'un retrait, mais ÉTIQUETÉE clôture (restitution de fin de vie ≠
    # retrait courant) — traçabilité fine en audit.
    ModeleEcriture(
        "epargne.cloture", "Clôture d'épargne (restitution)", "CA",
        (("EPARGNE", "D"), ("CAISSE", "C")),
    ),
    # Intérêts : la charge de l'IMF monte (D 603), la dette envers le membre monte (C 3111).
    # Journal des opérations diverses (pas de mouvement de caisse).
    ModeleEcriture(
        "epargne.interet", "Intérêts d'épargne (versement)", "OD",
        (("INTERETS", "D"), ("EPARGNE", "C")),
    ),
)

_UPSERT_SCHEMA = text(
    """
    INSERT INTO comptabilite.entry_schemas (code, label, journal_id, is_provisional)
    VALUES (:code, :label, (SELECT id FROM comptabilite.journals WHERE code = :journal), TRUE)
    ON CONFLICT (code) DO UPDATE SET
        label      = EXCLUDED.label,
        journal_id = EXCLUDED.journal_id,
        updated_at = NOW()
    RETURNING id
    """
)


def executer_seed_schemas(db: Session) -> int:
    """Installe/actualise les modèles d'écriture provisoires (dépôt, retrait). Ne committe pas."""
    for modele in MODELES:
        schema_id = db.execute(
            _UPSERT_SCHEMA,
            {"code": modele.code, "label": modele.label, "journal": modele.journal},
        ).scalar_one()
        # Réécrit les lignes du modèle (idempotent) : on repart d'un jeu propre.
        db.execute(
            text("DELETE FROM comptabilite.entry_schema_lines WHERE schema_id = :s"),
            {"s": schema_id},
        )
        for ordre, (role, side) in enumerate(modele.lignes, start=1):
            db.execute(
                text(
                    "INSERT INTO comptabilite.entry_schema_lines "
                    "(schema_id, line_order, role, side) VALUES (:s, :o, :r, :side)"
                ),
                {"s": schema_id, "o": ordre, "r": role, "side": side},
            )
    return len(MODELES)


def rattacher_caisse_agences(db: Session, numero: str = "5721") -> int:
    """Rattache PROVISOIREMENT le compte de caisse `numero` aux agences qui n'en ont pas.

    Ne remplace jamais un rattachement déjà posé (la vraie config caisse sera propre à l'IMF).
    Renvoie le nombre d'agences nouvellement rattachées (0 si le compte n'existe pas).
    """
    return len(
        db.execute(
            text(
                "UPDATE parameters.agencies SET compte_caisse_id = "
                "(SELECT id FROM comptabilite.accounts WHERE account_number = :n), "
                "updated_at = NOW() "
                "WHERE compte_caisse_id IS NULL "
                "AND EXISTS (SELECT 1 FROM comptabilite.accounts WHERE account_number = :n) "
                "RETURNING id"
            ),
            {"n": numero},
        ).fetchall()
    )


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
