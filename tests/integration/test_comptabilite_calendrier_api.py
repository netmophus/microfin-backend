"""Calendrier des jours fériés — chantier P1bis, lot 4a.

ADDITIF STRICT : ce lot ne crée que la table, le service (`est_jour_ouvre`,
`prochain_jour_ouvre`, CRUD) et l'écran. La SEULE modification d'un comportement existant
autorisée est `journee.prochaine_date_ouvree`, qui délègue désormais à `calendrier` pour
tenir compte des fériés, pas seulement du week-end — couvert ici par
`test_prochaine_date_ouvree_sans_ferie_comportement_inchange` (non-régression) et
`test_prochaine_date_ouvree_saute_un_ferie_sur_aujourdhui` (la bascule elle-même).

SÉMANTIQUE DE `prochain_jour_ouvre` : le PREMIER jour ouvré À PARTIR DE `d`, `d` INCLUS par
défaut (`strict=False`) — même convention que `journee.prochaine_date_ouvree` (propose
aujourd'hui s'il est déjà ouvré). `strict=True` cherche STRICTEMENT après `d` (réservé au
lot 4b, le report d'échéance).

Dates de test : octobre 2032 (lundi 2032-10-04 à vendredi 2032-10-08), loin de toute donnée
réelle, choisies pour composer un enchaînement week-end + férié contigu.
"""

import uuid
from collections.abc import Generator
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.comptabilite import calendrier, journee
from app.modules.comptabilite.calendrier import (
    JourFerieExistantError,
    JourFerieIntrouvableError,
)
from app.modules.security.jwt import creer_access_token
from app.modules.security.models import Role, User, UserRole
from app.modules.security.password import hasher_mot_de_passe

pytestmark = pytest.mark.integration

# Octobre 2032 : lundi -> vendredi, aucun week-end entre les deux.
LUNDI = date(2032, 10, 4)
MARDI = date(2032, 10, 5)
JEUDI = date(2032, 10, 7)
VENDREDI = date(2032, 10, 8)
SAMEDI = date(2032, 10, 9)
DIMANCHE = date(2032, 10, 10)
LUNDI_SUIVANT = date(2032, 10, 11)


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


@pytest.fixture
def client(db: Session) -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _agence_id(db: Session) -> uuid.UUID:
    return db.execute(text("SELECT id FROM parameters.agencies LIMIT 1")).scalar_one()


def _entete(db: Session, role_code: str) -> dict[str, str]:
    role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
    agence_id = _agence_id(db)
    s = uuid.uuid4().hex[:8]
    user = User(
        matricule=f"MAT-{s}", email=f"{s}@ex.com", username=f"u{s}",
        password_hash=hasher_mot_de_passe("Motdepasse!123"), last_name="T", first_name="A",
        primary_agency_id=agence_id,
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.flush()
    jeton = creer_access_token(
        user_id=user.id, roles=[role_code], primary_agency_id=agence_id, agency_id=agence_id
    )
    return {"Authorization": f"Bearer {jeton}"}


# --- est_jour_ouvre --------------------------------------------------------------------------


def test_est_jour_ouvre_jour_normal_accepte(db: Session) -> None:
    assert calendrier.est_jour_ouvre(db, MARDI) is True


def test_est_jour_ouvre_samedi_refuse(db: Session) -> None:
    assert calendrier.est_jour_ouvre(db, SAMEDI) is False


def test_est_jour_ouvre_dimanche_refuse(db: Session) -> None:
    assert calendrier.est_jour_ouvre(db, DIMANCHE) is False


def test_est_jour_ouvre_jour_ferie_refuse(db: Session) -> None:
    calendrier.ajouter_jour_ferie(db, JEUDI, "Férié de test", None)
    assert calendrier.est_jour_ouvre(db, JEUDI) is False


# --- prochain_jour_ouvre ------------------------------------------------------------------------


def test_prochain_jour_ouvre_jour_deja_ouvre_renvoie_le_meme_jour(db: Session) -> None:
    assert calendrier.prochain_jour_ouvre(db, MARDI) == MARDI


def test_prochain_jour_ouvre_strict_cherche_apres_le_jour_donne(db: Session) -> None:
    assert calendrier.prochain_jour_ouvre(db, MARDI, strict=True) != MARDI
    assert calendrier.prochain_jour_ouvre(db, MARDI, strict=True) == date(2032, 10, 6)


def test_prochain_jour_ouvre_saute_un_weekend(db: Session) -> None:
    assert calendrier.prochain_jour_ouvre(db, SAMEDI) == LUNDI_SUIVANT
    assert calendrier.prochain_jour_ouvre(db, DIMANCHE) == LUNDI_SUIVANT


def test_prochain_jour_ouvre_saute_un_ferie(db: Session) -> None:
    calendrier.ajouter_jour_ferie(db, JEUDI, "Férié de test", None)
    assert calendrier.prochain_jour_ouvre(db, JEUDI) == VENDREDI


def test_prochain_jour_ouvre_enchaine_ferie_puis_weekend(db: Session) -> None:
    """Le vendredi est férié : le week-end qui suit immédiatement est AUSSI non ouvré —
    `prochain_jour_ouvre` doit enchaîner les deux sans s'arrêter au samedi."""
    calendrier.ajouter_jour_ferie(db, VENDREDI, "Férié de test", None)
    assert calendrier.prochain_jour_ouvre(db, VENDREDI) == LUNDI_SUIVANT


# --- CRUD + doublon -------------------------------------------------------------------------


def test_ajouter_puis_lister_par_annee(db: Session) -> None:
    calendrier.ajouter_jour_ferie(db, JEUDI, "Fête du test", None)
    calendrier.ajouter_jour_ferie(db, VENDREDI, "Autre fête", None)
    # Une année DIFFÉRENTE ne doit rien remonter.
    assert calendrier.lister_jours_feries(db, 2031) == []
    resultat = calendrier.lister_jours_feries(db, 2032)
    assert [j.date_feriee for j in resultat] == [JEUDI, VENDREDI]  # triés par date


def test_ajouter_un_doublon_de_date_refuse(db: Session) -> None:
    calendrier.ajouter_jour_ferie(db, JEUDI, "Première saisie", None)
    with pytest.raises(JourFerieExistantError):
        calendrier.ajouter_jour_ferie(db, JEUDI, "Deuxième saisie", None)


def test_supprimer_un_jour_ferie(db: Session) -> None:
    jour_ferie = calendrier.ajouter_jour_ferie(db, JEUDI, "À supprimer", None)
    calendrier.supprimer_jour_ferie(db, jour_ferie.id, None)
    assert calendrier.lister_jours_feries(db, 2032) == []


def test_supprimer_un_jour_ferie_introuvable_refuse_proprement(db: Session) -> None:
    with pytest.raises(JourFerieIntrouvableError):
        calendrier.supprimer_jour_ferie(db, uuid.uuid4(), None)


# --- API : CRUD + permissions -----------------------------------------------------------------


def test_api_ajoute_puis_liste_puis_supprime(client: TestClient, db: Session) -> None:
    comptable = _entete(db, "COMPTABLE")

    reponse = client.post(
        "/comptabilite/jours-feries",
        json={"date_feriee": JEUDI.isoformat(), "libelle": "Tabaski"},
        headers=comptable,
    )
    assert reponse.status_code == 201
    corps = reponse.json()
    assert corps["date_feriee"] == JEUDI.isoformat()
    assert corps["libelle"] == "Tabaski"
    jour_ferie_id = corps["id"]

    liste = client.get("/comptabilite/jours-feries", params={"annee": 2032}, headers=comptable)
    assert liste.status_code == 200
    assert [j["date_feriee"] for j in liste.json()] == [JEUDI.isoformat()]

    suppression = client.delete(
        f"/comptabilite/jours-feries/{jour_ferie_id}", headers=comptable
    )
    assert suppression.status_code == 204

    liste_apres = client.get(
        "/comptabilite/jours-feries", params={"annee": 2032}, headers=comptable
    )
    assert liste_apres.json() == []


def test_api_ajouter_un_doublon_refuse_422(client: TestClient, db: Session) -> None:
    comptable = _entete(db, "COMPTABLE")
    client.post(
        "/comptabilite/jours-feries",
        json={"date_feriee": JEUDI.isoformat(), "libelle": "Première saisie"},
        headers=comptable,
    )
    reponse = client.post(
        "/comptabilite/jours-feries",
        json={"date_feriee": JEUDI.isoformat(), "libelle": "Deuxième saisie"},
        headers=comptable,
    )
    assert reponse.status_code == 422


def test_api_supprimer_un_jour_ferie_introuvable_404(client: TestClient, db: Session) -> None:
    comptable = _entete(db, "COMPTABLE")
    reponse = client.delete(
        f"/comptabilite/jours-feries/{uuid.uuid4()}", headers=comptable
    )
    assert reponse.status_code == 404


def test_api_champ_inattendu_refuse_422(client: TestClient, db: Session) -> None:
    comptable = _entete(db, "COMPTABLE")
    reponse = client.post(
        "/comptabilite/jours-feries",
        json={"date_feriee": JEUDI.isoformat(), "libelle": "Test", "status": "ouverte"},
        headers=comptable,
    )
    assert reponse.status_code == 422


def test_api_sans_permission_refuse_403(client: TestClient, db: Session) -> None:
    caissier = _entete(db, "CAISSIER")
    reponse = client.get(
        "/comptabilite/jours-feries", params={"annee": 2032}, headers=caissier
    )
    assert reponse.status_code == 403


# --- Non-régression de journee.prochaine_date_ouvree -------------------------------------------


def _prochaine_date_ouvree_sans_ferie_attendue() -> date:
    jour = datetime.now(UTC).date()
    while jour.weekday() >= 5:
        jour = date.fromordinal(jour.toordinal() + 1)
    return jour


def test_prochaine_date_ouvree_sans_ferie_comportement_inchange(db: Session) -> None:
    """Aucun férié dans la base de test : le comportement de `prochaine_date_ouvree` doit être
    EXACTEMENT celui d'avant ce lot (saute seulement samedi/dimanche)."""
    assert journee.prochaine_date_ouvree(db) == _prochaine_date_ouvree_sans_ferie_attendue()


def test_prochaine_date_ouvree_saute_un_ferie_sur_aujourdhui(db: Session) -> None:
    """La bascule elle-même : un férié posé sur AUJOURD'HUI doit repousser la date proposée,
    preuve que `prochaine_date_ouvree` consomme bien `calendrier.est_jour_ouvre`."""
    aujourdhui = datetime.now(UTC).date()
    if aujourdhui.weekday() >= 5:
        pytest.skip("aujourd'hui est un week-end dans cet environnement : rien à démontrer ici")
    calendrier.ajouter_jour_ferie(db, aujourdhui, "Férié de test (aujourd'hui)", None)
    resultat = journee.prochaine_date_ouvree(db)
    assert resultat > aujourdhui
    assert calendrier.est_jour_ouvre(db, resultat) is True
