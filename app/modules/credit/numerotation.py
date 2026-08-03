"""Numérotation atomique des dossiers de crédit — sans trou (patron épargne/tiers).

Une seule instruction sous verrou de ligne (ON CONFLICT) : deux créations simultanées ne
reçoivent jamais le même numéro, une création qui rollback ne consomme pas de numéro. Format :
« CR-2026-0000001 ». La séquence repart à 1 chaque année (clé (prefix, year)).
"""

from sqlalchemy import text
from sqlalchemy.orm import Session

PREFIX = "CR"
_LARGEUR = 7

_UPSERT = text(
    """
    INSERT INTO credit.numbering_sequences (prefix, year, last_value)
    VALUES (:prefix, :annee, 1)
    ON CONFLICT (prefix, year)
    DO UPDATE SET last_value = credit.numbering_sequences.last_value + 1,
                  updated_at = NOW()
    RETURNING last_value
    """
)

_ANNEE_COURANTE = text("SELECT EXTRACT(YEAR FROM NOW() AT TIME ZONE 'UTC')::int")


def prochain_numero(db: Session) -> str:
    """Alloue atomiquement le prochain numéro de dossier pour l'année courante (côté base)."""
    annee: int = db.execute(_ANNEE_COURANTE).scalar_one()
    valeur: int = db.execute(_UPSERT, {"prefix": PREFIX, "annee": annee}).scalar_one()
    return f"{PREFIX}-{annee}-{valeur:0{_LARGEUR}d}"
