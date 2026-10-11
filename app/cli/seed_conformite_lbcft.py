"""Seed des paramètres LBC/FT du module Tiers (lot PM/groupements).

Pose la ligne unique de `tiers.parametres_lbcft` : le seuil de détention à partir duquel une
personne liée est signalée bénéficiaire effectif. VALEUR PROVISOIRE (25 %), à confirmer sur la loi
uniforme LBC/FT de l'UEMOA : elle est marquée `is_provisional` et n'est jamais une constante du
code. Informative seulement : elle ne bloque rien.

NON DESTRUCTIF ET IDEMPOTENT : si la ligne existe, elle n'est ni relue ni réécrite (une valeur
corrigée depuis reste telle quelle). Ne committe pas.
"""

from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

SEUIL_PROVISOIRE = Decimal("25.00")


def executer_seed_lbcft(db: Session) -> bool:
    """Crée la ligne si elle manque. Renvoie True si elle vient d'être créée."""
    resultat = db.execute(
        text(
            "INSERT INTO tiers.parametres_lbcft (id, seuil_beneficiaire_effectif, is_provisional) "
            "VALUES (1, :seuil, TRUE) ON CONFLICT (id) DO NOTHING"
        ),
        {"seuil": SEUIL_PROVISOIRE},
    )
    return resultat.rowcount == 1
