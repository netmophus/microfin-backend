"""Le moteur du pont comptable : poser une pièce à partir d'un MODÈLE D'ÉCRITURE (donnée).

Générique, réutilisable (Épargne aujourd'hui, Crédit/Caisse demain). Le modèle (entry_schemas +
entry_schema_lines) dit, pour une opération, quelles lignes (rôle, sens) et dans quel journal.
Le RÔLE est abstrait ; l'appelant fournit un résolveur `role -> account_id` propre à son contexte
(l'Épargne résout EPARGNE via le produit, CAISSE via l'agence). Le montant est unique et porté par
toutes les lignes (opérations à deux lignes : dépôt, retrait).

La pièce est posée via le service C2 (creer_brouillon + valider) : TOUS les garde-fous comptables
(équilibre au niveau pièce, écriture réservée aux comptes de saisie, immuabilité) mordent LÀ, sur
la vraie écriture. Ce module ne les réimplémente pas.
"""

import uuid
from collections.abc import Callable
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete
from app.modules.comptabilite import ecritures
from app.modules.comptabilite.ecritures import LigneSaisie
from app.modules.comptabilite.models import EntrySchema, EntrySchemaLine, JournalEntry

# Fonction fournie par l'appelant : traduit un rôle abstrait en compte concret (ou lève).
ResolveurRole = Callable[[str], uuid.UUID]


class SchemaError(Exception):
    """Problème sur le modèle d'écriture."""


class SchemaIntrouvableError(SchemaError):
    """Aucun modèle actif pour ce code d'opération."""


class SchemaSansJournalError(SchemaError):
    """Le modèle n'a pas de journal rattaché (provisoire non renseigné) : on ne poste pas."""


def poser_depuis_schema(
    db: Session,
    *,
    code: str,
    montant: int,
    resoudre_role: ResolveurRole,
    entry_date: date,
    par: uuid.UUID | None,
    description: str,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> JournalEntry:
    """Pose et VALIDE une pièce à partir du modèle `code`. Le résolveur peut lever (rattachement
    manquant) : l'erreur remonte telle quelle, refus propre, rien n'est écrit."""
    schema = db.execute(
        select(EntrySchema).where(EntrySchema.code == code, EntrySchema.is_active)
    ).scalar_one_or_none()
    if schema is None:
        raise SchemaIntrouvableError(f"aucun modèle d'écriture actif pour « {code} »")
    if schema.journal_id is None:
        raise SchemaSansJournalError(f"le modèle « {code} » n'a pas de journal rattaché")

    lignes_modele = (
        db.execute(
            select(EntrySchemaLine)
            .where(EntrySchemaLine.schema_id == schema.id)
            .order_by(EntrySchemaLine.line_order)
        )
        .scalars()
        .all()
    )

    lignes = [
        LigneSaisie(
            account_id=resoudre_role(ligne.role),
            side=ligne.side,
            amount=montant,
            label=f"{code} ({ligne.role})",
        )
        for ligne in lignes_modele
    ]

    entry = ecritures.creer_brouillon(
        db,
        journal_id=schema.journal_id,
        entry_date=entry_date,
        description=description,
        lignes=lignes,
        par=par,
    )
    ecritures.valider(db, entry, par, contexte=contexte)
    return entry
