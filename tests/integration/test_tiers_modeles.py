"""Modèles Tiers (T1b) — l'héritage Class Table Inheritance résout le bon sous-type, et la
contrainte UNIQUE(tier_number) est le dernier rempart contre un numéro en double.

L'aller-retour est fait à travers un EXPUNGE : on vide l'identity map avant de relire, sinon
le test verrait l'objet Python qu'il vient de créer et ne prouverait rien sur le chargement
polymorphe depuis la base. Ici on recharge vraiment, et on vérifie que la ligne revient sous
sa classe fille exacte.
"""

import uuid
from collections.abc import Generator
from datetime import date

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import engine
from app.modules.tiers.models import (
    GroupProfile,
    IndividualProfile,
    LegalEntityProfile,
    Tier,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def db() -> Generator[Session, None, None]:
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(
        bind=connection,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
    )
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def _agence(db: Session) -> uuid.UUID:
    """Crée une agence via SQL brut et rend son id (évite d'importer le modèle Agency)."""
    suffixe = uuid.uuid4().hex[:8]
    return db.execute(
        text("INSERT INTO parameters.agencies (code, name) VALUES (:c, :n) RETURNING id"),
        {"c": f"AG-{suffixe}", "n": "Agence de test"},
    ).scalar_one()


def _pays(db: Session, code: str) -> uuid.UUID:
    return db.execute(
        text("SELECT id FROM parameters.countries WHERE code = :c"), {"c": code}
    ).scalar_one()


def _num() -> str:
    """Numéro de test unique (année 2999, hors des vraies séquences)."""
    return f"M-2999-{uuid.uuid4().int % 10_000_000:07d}"


def test_personne_physique_revient_sous_sa_classe(db: Session) -> None:
    agence = _agence(db)
    profil = IndividualProfile(
        tier_number=_num(),
        primary_agency_id=agence,
        last_name="Diallo",
        first_name="Amadou",
        birth_date=date(1990, 5, 12),
        gender="M",
        nationality_id=_pays(db, "SN"),
    )
    db.add(profil)
    db.flush()
    identifiant = profil.id
    db.expunge_all()  # vide l'identity map : la relecture vient vraiment de la base

    charge = db.execute(select(Tier).where(Tier.id == identifiant)).scalar_one()

    assert isinstance(charge, IndividualProfile)
    assert charge.tier_type == "individual"
    assert charge.last_name == "Diallo"
    assert charge.status == "prospect"  # défaut serveur


def test_personne_morale_revient_sous_sa_classe(db: Session) -> None:
    agence = _agence(db)
    profil = LegalEntityProfile(
        tier_number=f"P-2999-{uuid.uuid4().int % 10_000_000:07d}",
        primary_agency_id=agence,
        legal_name="ACME SARL",
        legal_form="SARL",
        constitution_date=date(2020, 1, 1),
        headquarters_country_id=_pays(db, "SN"),
    )
    db.add(profil)
    db.flush()
    identifiant = profil.id
    db.expunge_all()

    charge = db.execute(select(Tier).where(Tier.id == identifiant)).scalar_one()

    assert isinstance(charge, LegalEntityProfile)
    assert charge.tier_type == "legal_entity"
    assert charge.legal_name == "ACME SARL"


def test_groupement_revient_sous_sa_classe(db: Session) -> None:
    agence = _agence(db)
    profil = GroupProfile(
        tier_number=f"G-2999-{uuid.uuid4().int % 10_000_000:07d}",
        primary_agency_id=agence,
        group_name="Femmes de Ouallam",
        group_type="caution_solidaire",
        constitution_date=date(2021, 3, 1),
    )
    db.add(profil)
    db.flush()
    identifiant = profil.id
    db.expunge_all()

    charge = db.execute(select(Tier).where(Tier.id == identifiant)).scalar_one()

    assert isinstance(charge, GroupProfile)
    assert charge.tier_type == "group"
    assert charge.group_name == "Femmes de Ouallam"


def test_tier_number_en_double_est_rejete(db: Session) -> None:
    """Le backstop UNIQUE(tier_number) MORD : deux fiches ne partagent jamais un numéro."""
    agence = _agence(db)
    numero = _num()
    db.add(
        IndividualProfile(
            tier_number=numero,
            primary_agency_id=agence,
            last_name="Ba",
            first_name="Fatou",
            birth_date=date(1985, 2, 2),
            gender="F",
            nationality_id=_pays(db, "SN"),
        )
    )
    db.flush()
    db.add(
        IndividualProfile(
            tier_number=numero,  # le MÊME numéro
            primary_agency_id=agence,
            last_name="Sow",
            first_name="Moussa",
            birth_date=date(1992, 7, 7),
            gender="M",
            nationality_id=_pays(db, "SN"),
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()
