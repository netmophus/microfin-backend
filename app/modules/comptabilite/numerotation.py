"""Numérotation atomique des pièces comptables — par (journal, exercice), sans trou.

Même mécanisme que la numérotation des tiers (une seule instruction sous verrou de ligne,
ON CONFLICT) : deux comptables qui valident au même instant sur le même journal ne reçoivent
jamais le même numéro, et une validation qui rollback ne consomme pas de numéro (l'incrément
vit dans la transaction de validation). Le zéro-trou par journal et par exercice est une
exigence réglementaire (BCEAO), pas cosmétique.

La séquence repart à 1 à chaque nouvel exercice : la clé est (journal_id, exercice_id).
Format du numéro : « CA-2026-000001 » (code journal - code exercice - rang).
"""

from sqlalchemy import text
from sqlalchemy.orm import Session

_LARGEUR = 6

_UPSERT = text(
    """
    INSERT INTO comptabilite.numbering_sequences (journal_id, exercice_id, last_value)
    VALUES (:journal_id, :exercice_id, 1)
    ON CONFLICT (journal_id, exercice_id)
    DO UPDATE SET last_value = comptabilite.numbering_sequences.last_value + 1,
                  updated_at = NOW()
    RETURNING last_value
    """
)


def prochain_numero(
    db: Session,
    *,
    journal_id: object,
    exercice_id: object,
    journal_code: str,
    exercice_code: str,
) -> str:
    """Alloue atomiquement le prochain rang pour (journal, exercice) et le formate."""
    valeur: int = db.execute(
        _UPSERT, {"journal_id": journal_id, "exercice_id": exercice_id}
    ).scalar_one()
    return f"{journal_code}-{exercice_code}-{valeur:0{_LARGEUR}d}"
