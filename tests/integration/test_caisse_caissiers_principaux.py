"""Chantier coffre-fort/caisses, sous-chantier 3, Lot B — désignation du caissier principal
(`caisse/caissiers_principaux.py`, endpoints GET/PUT/DELETE `/caisse/agences/{id}/caissier-
principal`).

Ce que ces tests protègent :
  - Désigner, remplacer (upsert — l'ancien saute, un seul caissier principal à la fois),
    retirer (idempotent), lire (désigné / aucun, état lisible).
  - Garde-fou à DEUX conditions cumulées : l'utilisateur doit être habilité à l'agence ET
    détenir le rôle Caissier — vérifié séparément (contre-épreuve : habilité mais mauvais rôle
    refusé ; bon rôle mais hors agence refusé).
  - Permission `caisse.principale.manage` exigée (RESPONSABLE_AGENCE seul).
  - Cloisonnement AGENCE STRICT (pas condition_perimetre, même discipline que le coffre dans
    transferts.py) : hors périmètre -> 404, jamais 403 (IDOR).
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.caisse.caissiers_principaux import (
    RoleCaissierRequisError,
    UtilisateurHorsPerimetreError,
    designer,
    lire,
    retirer,
)
from app.modules.caisse.models import CaissierPrincipal
from app.modules.parameters.models import Agency
from app.modules.security.jwt import creer_access_token
from app.modules.security.models import Role, User, UserRole
from app.modules.security.password import hasher_mot_de_passe

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


@pytest.fixture
def client(db: Session) -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def agence(db: Session) -> Agency:
    agence = Agency(code=f"AG-{uuid.uuid4().hex[:6]}", name="Agence de test")
    db.add(agence)
    db.flush()
    return agence


def _utilisateur(db: Session, agence: Agency, role_code: str) -> User:
    role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
    suffixe = uuid.uuid4().hex[:8]
    user = User(
        matricule=f"MAT-{suffixe}",
        email=f"{suffixe}@example.com",
        username=f"u{suffixe}",
        password_hash=hasher_mot_de_passe("Motdepasse!123"),
        last_name="Test",
        first_name="U",
        primary_agency_id=agence.id,
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.flush()
    return user


def _entete(user: User, agence: Agency, role_code: str) -> dict[str, str]:
    jeton = creer_access_token(
        user_id=user.id, roles=[role_code], primary_agency_id=agence.id, agency_id=agence.id
    )
    return {"Authorization": f"Bearer {jeton}"}


# --- Service ----------------------------------------------------------------------------------


def test_designer_reussit(db: Session, agence: Agency) -> None:
    caissier = _utilisateur(db, agence, "CAISSIER")

    designation = designer(db, agence.id, caissier.id, motif="Premier caissier principal", par=None)

    assert designation.user_id == caissier.id
    assert lire(db, agence.id) is not None
    assert lire(db, agence.id).user_id == caissier.id  # type: ignore[union-attr]


def test_designer_remplace_lancien(db: Session, agence: Agency) -> None:
    premier = _utilisateur(db, agence, "CAISSIER")
    second = _utilisateur(db, agence, "CAISSIER")
    designer(db, agence.id, premier.id, motif="Premier", par=None)

    designer(db, agence.id, second.id, motif="Remplacement", par=None)

    designation = lire(db, agence.id)
    assert designation is not None
    assert designation.user_id == second.id
    # Un seul caissier principal à la fois -- une seule ligne, jamais un ajout.
    total = db.execute(
        select(CaissierPrincipal).where(CaissierPrincipal.agency_id == agence.id)
    ).scalars().all()
    assert len(total) == 1


def test_retirer_leve_la_designation(db: Session, agence: Agency) -> None:
    caissier = _utilisateur(db, agence, "CAISSIER")
    designer(db, agence.id, caissier.id, motif="Désignation", par=None)

    retirer(db, agence.id, par=None)

    assert lire(db, agence.id) is None


def test_retirer_idempotent_si_aucune_designation(db: Session, agence: Agency) -> None:
    retirer(db, agence.id, par=None)  # ne lève rien
    assert lire(db, agence.id) is None


def test_lire_aucune_designation_est_lisible(db: Session, agence: Agency) -> None:
    assert lire(db, agence.id) is None  # état légitime, pas une erreur


def test_designer_refuse_utilisateur_hors_agence(db: Session, agence: Agency) -> None:
    autre_agence = Agency(code=f"AG-{uuid.uuid4().hex[:6]}", name="Autre agence")
    db.add(autre_agence)
    db.flush()
    intrus = _utilisateur(db, autre_agence, "CAISSIER")

    with pytest.raises(UtilisateurHorsPerimetreError):
        designer(db, agence.id, intrus.id, motif="Tentative hors agence", par=None)


def test_designer_refuse_utilisateur_sans_role_caissier(db: Session, agence: Agency) -> None:
    comptable = _utilisateur(db, agence, "COMPTABLE")  # habilité à l'agence, mauvais rôle

    with pytest.raises(RoleCaissierRequisError):
        designer(db, agence.id, comptable.id, motif="Tentative avec un comptable", par=None)


# --- API ----------------------------------------------------------------------------------------


def test_api_lire_aucune_designation(client: TestClient, db: Session, agence: Agency) -> None:
    responsable = _utilisateur(db, agence, "RESPONSABLE_AGENCE")

    reponse = client.get(
        f"/caisse/agences/{agence.id}/caissier-principal",
        headers=_entete(responsable, agence, "RESPONSABLE_AGENCE"),
    )

    assert reponse.status_code == 200
    assert reponse.json()["caissier_principal"] is None


def test_api_designer_reussit(client: TestClient, db: Session, agence: Agency) -> None:
    responsable = _utilisateur(db, agence, "RESPONSABLE_AGENCE")
    caissier = _utilisateur(db, agence, "CAISSIER")

    reponse = client.put(
        f"/caisse/agences/{agence.id}/caissier-principal",
        json={"user_id": str(caissier.id), "motif": "Désignation initiale"},
        headers=_entete(responsable, agence, "RESPONSABLE_AGENCE"),
    )

    assert reponse.status_code == 200
    corps = reponse.json()["caissier_principal"]
    assert corps["id"] == str(caissier.id)


def test_api_retirer_reussit(client: TestClient, db: Session, agence: Agency) -> None:
    responsable = _utilisateur(db, agence, "RESPONSABLE_AGENCE")
    caissier = _utilisateur(db, agence, "CAISSIER")
    client.put(
        f"/caisse/agences/{agence.id}/caissier-principal",
        json={"user_id": str(caissier.id), "motif": "Désignation"},
        headers=_entete(responsable, agence, "RESPONSABLE_AGENCE"),
    )

    suppression = client.delete(
        f"/caisse/agences/{agence.id}/caissier-principal",
        headers=_entete(responsable, agence, "RESPONSABLE_AGENCE"),
    )
    assert suppression.status_code == 204

    reponse = client.get(
        f"/caisse/agences/{agence.id}/caissier-principal",
        headers=_entete(responsable, agence, "RESPONSABLE_AGENCE"),
    )
    assert reponse.json()["caissier_principal"] is None


def test_api_permission_absente_403(client: TestClient, db: Session, agence: Agency) -> None:
    caissier = _utilisateur(db, agence, "CAISSIER")  # n'a pas caisse.principale.manage

    reponse = client.get(
        f"/caisse/agences/{agence.id}/caissier-principal",
        headers=_entete(caissier, agence, "CAISSIER"),
    )

    assert reponse.status_code == 403


def test_api_cloisonnement_hors_agence_404(client: TestClient, db: Session, agence: Agency) -> None:
    autre_agence = Agency(code=f"AG-{uuid.uuid4().hex[:6]}", name="Autre agence")
    db.add(autre_agence)
    db.flush()
    intrus = _utilisateur(db, autre_agence, "RESPONSABLE_AGENCE")

    reponse = client.get(
        f"/caisse/agences/{agence.id}/caissier-principal",
        headers=_entete(intrus, autre_agence, "RESPONSABLE_AGENCE"),
    )

    assert reponse.status_code == 404


def test_api_designer_user_non_eligible_422(
    client: TestClient, db: Session, agence: Agency
) -> None:
    responsable = _utilisateur(db, agence, "RESPONSABLE_AGENCE")
    comptable = _utilisateur(db, agence, "COMPTABLE")  # habilité, mauvais rôle

    reponse = client.put(
        f"/caisse/agences/{agence.id}/caissier-principal",
        json={"user_id": str(comptable.id), "motif": "Tentative avec un comptable"},
        headers=_entete(responsable, agence, "RESPONSABLE_AGENCE"),
    )

    assert reponse.status_code == 422
    assert "rôle Caissier" in reponse.json()["detail"]
