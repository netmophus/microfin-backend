"""Schéma identité & contacts (T2a) — les contraintes qui MORDENT.

Trois garanties structurelles, prouvées en les voyant refuser (comme la FK composite de T0) :
  - une adresse sans rue ni repère est refusée ;
  - une adresse avec un point de repère seul est acceptée (zone rurale) ;
  - un tiers ne peut pas avoir deux pièces principales.
"""

import uuid
from collections.abc import Generator
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import engine
from app.modules.tiers.models import Contact, IdentityDocument, IndividualProfile

pytestmark = pytest.mark.integration


@pytest.fixture
def db() -> Generator[Session, None, None]:
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def _agence(db: Session) -> uuid.UUID:
    suffixe = uuid.uuid4().hex[:8]
    return db.execute(
        text("INSERT INTO parameters.agencies (code, name) VALUES (:c, :n) RETURNING id"),
        {"c": f"AG-{suffixe}", "n": "Agence de test"},
    ).scalar_one()


def _pays(db: Session, code: str) -> uuid.UUID:
    return db.execute(
        text("SELECT id FROM parameters.countries WHERE code = :c"), {"c": code}
    ).scalar_one()


def _type_piece(db: Session, code: str) -> uuid.UUID:
    return db.execute(
        text("SELECT id FROM parameters.identity_document_types WHERE code = :c"), {"c": code}
    ).scalar_one()


def _tier(db: Session) -> IndividualProfile:
    tier = IndividualProfile(
        tier_number=f"M-2999-{uuid.uuid4().int % 10_000_000:07d}",
        primary_agency_id=_agence(db),
        last_name="Diallo",
        first_name="Amadou",
        birth_date=date(1990, 5, 12),
        gender="M",
        nationality_id=_pays(db, "SN"),
    )
    db.add(tier)
    db.flush()
    return tier


# --- adresse en zone rurale ------------------------------------------------------------


def test_une_adresse_sans_rue_ni_repere_est_refusee(db: Session) -> None:
    tier = _tier(db)
    # Type 'address', mais ni address_line1 ni landmark : ck_contacts_address refuse.
    db.add(Contact(tier_id=tier.id, contact_type="address", quarter="Plateau"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_une_adresse_avec_repere_seul_est_acceptee(db: Session) -> None:
    tier = _tier(db)
    db.add(
        Contact(
            tier_id=tier.id,
            contact_type="address",
            landmark="Derrière la mosquée, à côté de la boutique de Salif",
        )
    )
    db.flush()  # aucune erreur : le repère suffit

    trouve = db.execute(
        text("SELECT landmark FROM tiers.contacts WHERE tier_id = :t"), {"t": tier.id}
    ).scalar_one()
    assert trouve.startswith("Derrière la mosquée")


# --- pièce principale unique -----------------------------------------------------------


def test_un_tiers_ne_peut_avoir_deux_pieces_principales(db: Session) -> None:
    tier = _tier(db)
    type_cni = _type_piece(db, "CNI")

    db.add(
        IdentityDocument(
            tier_id=tier.id,
            document_type_id=type_cni,
            document_number="A-1",
            document_number_normalized="A-1",
            is_primary=True,
        )
    )
    db.flush()

    db.add(
        IdentityDocument(
            tier_id=tier.id,
            document_type_id=type_cni,
            document_number="A-2",
            document_number_normalized="A-2",
            is_primary=True,
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()  # l'index unique partiel refuse la seconde principale
