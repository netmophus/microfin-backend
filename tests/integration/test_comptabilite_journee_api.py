"""API — Journée comptable (chantier P1bis, lots 1 et 2).

LOT 1 : modèle, ouverture/fermeture, écran. LOT 2 : branchement caisse — la clôture refuse s'il
reste une session de caisse ouverte quelque part sur le réseau (côté ouverture de caisse, voir
`test_caisse_sessions.py::test_ouverture_refusee_si_aucune_journee_comptable_ouverte`, la moitié
symétrique de la garde vit dans `caisse/service.py`, pas ici).

  - au plus une journée OUVERTE à la fois, garanti par l'index unique partiel en base
    (migration 0055) — le test `test_refuse_une_seconde_ouverture_si_deja_ouverte` couvre le
    contrôle applicatif, pas l'index lui-même (pas testable depuis une connexion unique) ;
  - une date déjà utilisée (ouverte OU clôturée) ne peut pas servir à une nouvelle ouverture ;
  - clôture DÉFINITIVE, aucune réouverture ;
  - clôture refusée (chantier P1bis lot 2) si une session de caisse reste ouverte, quelle que
    soit son agence — la caisse ouverte dans ce test est posée via le VRAI service
    (`caisse.service.ouvrir_session`), jamais une ligne SQL à la main ;
  - permissions (réorganisation RBAC post lot 4b) : consultation (liste, courante) ->
    compta.journee.read (COMPTABLE et ADMIN_FONCTIONNEL) ; ouverture/clôture ->
    compta.journee.manage (ADMIN_FONCTIONNEL SEUL, le COMPTABLE n'ouvre/ne clôture plus),
    403 sinon — permission DISTINCTE de compta.exercice.manage.

Dates de test : lundis fixes (2031-06-02, 2031-06-09), loin de toute donnée réelle — aucun
risque de collision avec l'UNIQUE sur `date_comptable` entre tests (chaque test roule dans sa
propre transaction, annulée à la fin).
"""

import uuid
from collections.abc import Generator
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.caisse.models import Poste, PosteAssignation
from app.modules.caisse.service import ouvrir_session
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.security.jwt import creer_access_token
from app.modules.security.models import Role, User, UserRole
from app.modules.security.password import hasher_mot_de_passe

pytestmark = pytest.mark.integration

LUNDI_1 = date(2031, 6, 2)
LUNDI_2 = date(2031, 6, 9)
SAMEDI = date(2031, 6, 7)
DIMANCHE = date(2031, 6, 8)


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


def _prochaine_date_ouvree_attendue() -> str:
    """Même algorithme que journee.prochaine_date_ouvree, calculé côté test en Python plutôt
    que via CURRENT_DATE : les deux sources doivent converger quel que soit le jour
    d'exécution du test."""
    jour = datetime.now(UTC).date()
    while jour.weekday() >= 5:
        jour += timedelta(days=1)
    return jour.isoformat()


# --- Journée courante --------------------------------------------------------------------------


def test_courante_sans_journee_ouverte_propose_la_prochaine_date_ouvree(
    client: TestClient, db: Session
) -> None:
    comptable = _entete(db, "COMPTABLE")
    reponse = client.get("/comptabilite/journees/courante", headers=comptable)
    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["journee"] is None
    assert corps["prochaine_date_proposee"] == _prochaine_date_ouvree_attendue()


def test_courante_sans_permission_refuse_403(client: TestClient, db: Session) -> None:
    caissier = _entete(db, "CAISSIER")
    reponse = client.get("/comptabilite/journees/courante", headers=caissier)
    assert reponse.status_code == 403


# --- Ouverture -----------------------------------------------------------------------------------


def test_ouverture_reussie(client: TestClient, db: Session) -> None:
    gestionnaire = _entete(db, "ADMIN_FONCTIONNEL")
    reponse = client.post(
        "/comptabilite/journees",
        json={"date_comptable": LUNDI_1.isoformat()},
        headers=gestionnaire,
    )
    assert reponse.status_code == 201
    corps = reponse.json()
    assert corps["date_comptable"] == LUNDI_1.isoformat()
    assert corps["status"] == "ouverte"
    assert corps["opened_par_nom"]
    assert corps["closed_at"] is None

    courante = client.get("/comptabilite/journees/courante", headers=gestionnaire)
    assert courante.json()["journee"]["id"] == corps["id"]


def test_refuse_une_seconde_ouverture_si_deja_ouverte(client: TestClient, db: Session) -> None:
    gestionnaire = _entete(db, "ADMIN_FONCTIONNEL")
    client.post(
        "/comptabilite/journees",
        json={"date_comptable": LUNDI_1.isoformat()},
        headers=gestionnaire,
    )
    reponse = client.post(
        "/comptabilite/journees",
        json={"date_comptable": LUNDI_2.isoformat()},
        headers=gestionnaire,
    )
    assert reponse.status_code == 422
    assert "déjà ouverte" in reponse.json()["detail"]


def test_refuse_une_date_deja_utilisee(client: TestClient, db: Session) -> None:
    gestionnaire = _entete(db, "ADMIN_FONCTIONNEL")
    client.post(
        "/comptabilite/journees",
        json={"date_comptable": LUNDI_1.isoformat()},
        headers=gestionnaire,
    )
    client.post("/comptabilite/journees/cloture", headers=gestionnaire)

    reponse = client.post(
        "/comptabilite/journees",
        json={"date_comptable": LUNDI_1.isoformat()},
        headers=gestionnaire,
    )
    assert reponse.status_code == 422
    assert str(LUNDI_1) in reponse.json()["detail"]


def test_ouverture_champ_inattendu_refuse_422(client: TestClient, db: Session) -> None:
    gestionnaire = _entete(db, "ADMIN_FONCTIONNEL")
    reponse = client.post(
        "/comptabilite/journees",
        json={"date_comptable": LUNDI_1.isoformat(), "status": "cloturee"},
        headers=gestionnaire,
    )
    assert reponse.status_code == 422


def test_ouverture_sans_permission_refuse_403(client: TestClient, db: Session) -> None:
    caissier = _entete(db, "CAISSIER")
    reponse = client.post(
        "/comptabilite/journees", json={"date_comptable": LUNDI_1.isoformat()}, headers=caissier
    )
    assert reponse.status_code == 403


def test_ouverture_refusee_au_comptable_depuis_la_reorganisation_rbac(
    client: TestClient, db: Session
) -> None:
    """Réorganisation RBAC post lot 4b : le COMPTABLE garde journee.read mais perd
    journee.manage — l'ouverture, acte d'exploitation, lui est désormais refusée."""
    comptable = _entete(db, "COMPTABLE")
    reponse = client.post(
        "/comptabilite/journees", json={"date_comptable": LUNDI_1.isoformat()}, headers=comptable
    )
    assert reponse.status_code == 403


# --- Clôture ---------------------------------------------------------------------------------


def test_cloture_reussie(client: TestClient, db: Session) -> None:
    gestionnaire = _entete(db, "ADMIN_FONCTIONNEL")
    client.post(
        "/comptabilite/journees",
        json={"date_comptable": LUNDI_1.isoformat()},
        headers=gestionnaire,
    )
    reponse = client.post("/comptabilite/journees/cloture", headers=gestionnaire)
    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["status"] == "cloturee"
    assert corps["closed_at"] is not None
    assert corps["closed_par_nom"]

    courante = client.get("/comptabilite/journees/courante", headers=gestionnaire)
    assert courante.json()["journee"] is None


def test_refuse_la_cloture_sans_journee_ouverte(client: TestClient, db: Session) -> None:
    gestionnaire = _entete(db, "ADMIN_FONCTIONNEL")
    reponse = client.post("/comptabilite/journees/cloture", headers=gestionnaire)
    assert reponse.status_code == 422
    assert "Aucune journée" in reponse.json()["detail"]


def test_cloture_sans_permission_refuse_403(client: TestClient, db: Session) -> None:
    caissier = _entete(db, "CAISSIER")
    reponse = client.post("/comptabilite/journees/cloture", headers=caissier)
    assert reponse.status_code == 403


def test_cloture_refusee_au_comptable_depuis_la_reorganisation_rbac(
    client: TestClient, db: Session
) -> None:
    """Réorganisation RBAC post lot 4b : le COMPTABLE garde journee.read mais perd
    journee.manage — la clôture, acte d'exploitation, lui est désormais refusée."""
    gestionnaire = _entete(db, "ADMIN_FONCTIONNEL")
    client.post(
        "/comptabilite/journees",
        json={"date_comptable": LUNDI_1.isoformat()},
        headers=gestionnaire,
    )

    comptable = _entete(db, "COMPTABLE")
    reponse = client.post("/comptabilite/journees/cloture", headers=comptable)
    assert reponse.status_code == 403


def _cid(db: Session, numero: str) -> uuid.UUID:
    return db.execute(
        text("SELECT id FROM comptabilite.accounts WHERE account_number = :n"), {"n": numero}
    ).scalar_one()


def _agence_avec_poste_caisse(db: Session, code: str) -> Poste:
    """Agence + poste de caisse principal, même patron que test_caisse_sessions.py::_agence."""
    compte_id = _cid(db, "101111")
    agence = Agency(code=code, name=f"Agence {code}", compte_caisse_id=compte_id)
    db.add(agence)
    db.flush()
    poste = Poste(
        agency_id=agence.id, code="01", libelle="Caisse principale", compte_caisse_id=compte_id
    )
    db.add(poste)
    db.flush()
    return poste


def _caissier_courant(db: Session, poste: Poste, suffixe: str) -> UtilisateurCourant:
    role = db.execute(select(Role).where(Role.code == "CAISSIER")).scalar_one()
    user = User(
        matricule=f"MAT-JRN-{suffixe}", email=f"jrn{suffixe}@ex.com", username=f"jrn{suffixe}",
        password_hash=hasher_mot_de_passe("Motdepasse!123"),
        last_name="Caissier", first_name=suffixe,
        primary_agency_id=poste.agency_id,
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.add(PosteAssignation(poste_id=poste.id, user_id=user.id))
    db.flush()
    return UtilisateurCourant(
        user_id=user.id,
        roles=("CAISSIER",),
        permissions=frozenset({"caisse.session.open"}),
        primary_agency_id=poste.agency_id,
        agency_id=poste.agency_id,
        voit_tout=False,
    )


def test_cloture_refusee_si_une_caisse_reste_ouverte(client: TestClient, db: Session) -> None:
    """Chantier P1bis lot 2 — la clôture refuse tant qu'il reste une session de caisse ouverte,
    quel que soit le réseau : la caisse est ouverte ici via le VRAI service
    (`caisse.service.ouvrir_session`), jamais une ligne SQL à la main."""
    gestionnaire = _entete(db, "ADMIN_FONCTIONNEL")
    client.post(
        "/comptabilite/journees",
        json={"date_comptable": LUNDI_1.isoformat()},
        headers=gestionnaire,
    )

    poste = _agence_avec_poste_caisse(db, "CXJCL")
    caissier = _caissier_courant(db, poste, "CL")
    ouvrir_session(db, caissier, poste_id=poste.id, fonds_initial=10_000)

    reponse = client.post("/comptabilite/journees/cloture", headers=gestionnaire)
    assert reponse.status_code == 422
    assert "1 caisse" in reponse.json()["detail"]

    courante = client.get("/comptabilite/journees/courante", headers=gestionnaire)
    assert courante.json()["journee"]["status"] == "ouverte"


# --- Historique --------------------------------------------------------------------------------


def test_liste_les_journees_plus_recente_dabord_avec_noms_resolus(
    client: TestClient, db: Session
) -> None:
    gestionnaire = _entete(db, "ADMIN_FONCTIONNEL")
    client.post(
        "/comptabilite/journees",
        json={"date_comptable": LUNDI_1.isoformat()},
        headers=gestionnaire,
    )
    client.post("/comptabilite/journees/cloture", headers=gestionnaire)
    client.post(
        "/comptabilite/journees",
        json={"date_comptable": LUNDI_2.isoformat()},
        headers=gestionnaire,
    )

    reponse = client.get("/comptabilite/journees", headers=gestionnaire)
    assert reponse.status_code == 200
    corps = reponse.json()
    assert [ligne["date_comptable"] for ligne in corps] == [
        LUNDI_2.isoformat(),
        LUNDI_1.isoformat(),
    ]
    plus_recente, plus_ancienne = corps
    assert plus_recente["status"] == "ouverte"
    assert plus_recente["opened_par_nom"]
    assert plus_ancienne["status"] == "cloturee"
    assert plus_ancienne["closed_par_nom"]


def test_liste_sans_permission_refuse_403(client: TestClient, db: Session) -> None:
    caissier = _entete(db, "CAISSIER")
    reponse = client.get("/comptabilite/journees", headers=caissier)
    assert reponse.status_code == 403
