"""Aide de test partagée : alimenter un compte de caisse au grand livre.

Depuis le contrôle de solde (transferts, puis décaissement en espèces), une caisse doit contenir
au grand livre ce qu'on en sort. Les tests qui sortent de l'argent d'une caisse la garnissent
d'abord ici : pièce VALIDÉE D compte de caisse / C 1141 (contrepartie sans importance, seule la
caisse est lue)."""

import uuid
from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.comptabilite import ecritures, journee
from app.modules.comptabilite.ecritures import LigneSaisie

FONDS_SOURCE = 10_000_000


def alimenter_compte(
    db: Session, compte_id: uuid.UUID, montant: int = FONDS_SOURCE, jour: date | None = None
) -> None:
    journal_id = db.execute(
        text("SELECT journal_id FROM comptabilite.entry_schemas WHERE code = :c"),
        {"c": "caisse.transfert.envoi"},
    ).scalar_one()
    contrepartie = db.execute(
        text("SELECT id FROM comptabilite.accounts WHERE account_number = '1141'")
    ).scalar_one()
    entree = ecritures.creer_brouillon(
        db,
        journal_id=journal_id,
        entry_date=jour or journee.date_comptable_courante(db),
        description="Alimentation de test",
        lignes=[
            LigneSaisie(account_id=compte_id, side="D", amount=montant),
            LigneSaisie(account_id=contrepartie, side="C", amount=montant),
        ],
        par=None,
    )
    ecritures.valider(db, entree, None)
