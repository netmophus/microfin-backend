"""Chantier coffre-fort/caisses, sous-chantier 1, Bloc 1 — paramétrage des niveaux caisse
(`caisse.niveaux_caisse`), lecture/écriture via `GET/PATCH /caisse/niveaux`.

Ce que ces tests protègent :
  - La table accepte SEULEMENT 'coffre'/'principale' (CHECK base) — pas 'secondaire' (qui vit
    ailleurs, sur les postes).
  - Le garde-fou `comptabilite.comptes.compte_caisse_valide` MORD : un compte sous 1011 est
    accepté, un compte hors 1011 est refusé — SANS toucher `compte_saisie_actif`, qui continue
    de servir épargne/parts sans cette contrainte (vérifié explicitement).
  - Un niveau non paramétré (`compte_caisse_id IS NULL`, ou aucune ligne du tout) est un état
    LISIBLE, jamais une erreur.
  - `compta.plan.manage` est exigée en écriture ; un rattachement hors 1011 ou vers une agence
    inexistante échoue proprement (422 / 404), rien n'est écrit.
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.comptabilite.comptes import (
    CompteHorsCaisseError,
    compte_caisse_valide,
    compte_saisie_actif,
)
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


def _cid(db: Session, numero: str) -> uuid.UUID:
    return db.execute(
        text("SELECT id FROM comptabilite.accounts WHERE account_number = :n"), {"n": numero}
    ).scalar_one()


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


# --- Table : CHECK base, vue MORDRE --------------------------------------------------------


def test_niveau_secondaire_rejete_par_la_base(db: Session, agence: Agency) -> None:
    # Le niveau secondaire vit sur les postes, jamais ici — la base doit refuser toute tentative.
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text(
                "INSERT INTO caisse.niveaux_caisse (agency_id, niveau) VALUES (:a, 'secondaire')"
            ),
            {"a": agence.id},
        )
    assert "niveau_valide" in str(exc.value)


def test_coffre_et_principale_acceptes_par_la_base(db: Session, agence: Agency) -> None:
    db.execute(
        text("INSERT INTO caisse.niveaux_caisse (agency_id, niveau) VALUES (:a, 'coffre')"),
        {"a": agence.id},
    )
    db.execute(
        text("INSERT INTO caisse.niveaux_caisse (agency_id, niveau) VALUES (:a, 'principale')"),
        {"a": agence.id},
    )
    n = db.execute(
        text("SELECT count(*) FROM caisse.niveaux_caisse WHERE agency_id = :a"), {"a": agence.id}
    ).scalar_one()
    assert n == 2


# --- Garde-fou service : compte_caisse_valide, vu MORDRE -----------------------------------


def test_compte_caisse_valide_accepte_un_compte_sous_1011(db: Session) -> None:
    compte = compte_caisse_valide(db, "101111")
    assert compte.account_number == "101111"


def test_compte_caisse_valide_refuse_un_compte_hors_1011(db: Session) -> None:
    with pytest.raises(CompteHorsCaisseError) as exc:
        compte_caisse_valide(db, "251111")
    assert "1011" in str(exc.value)


def test_compte_saisie_actif_ne_change_pas_pour_epargne_et_parts(db: Session) -> None:
    # Contre-épreuve explicite : le garde-fou caisse est une fonction SÉPARÉE — la fonction
    # générique continue d'accepter des comptes hors 1011, sans cette restriction.
    assert compte_saisie_actif(db, "251111").account_number == "251111"
    assert compte_saisie_actif(db, "571111").account_number == "571111"


# --- Endpoints ------------------------------------------------------------------------------


def test_lister_niveaux_non_parametre_est_lisible(
    client: TestClient, db: Session, agence: Agency
) -> None:
    comptable = _utilisateur(db, agence, "COMPTABLE")

    reponse = client.get("/caisse/niveaux", headers=_entete(comptable, agence, "COMPTABLE"))

    assert reponse.status_code == 200
    lignes = {a["agency_id"]: a for a in reponse.json()}
    assert str(agence.id) in lignes
    niveaux = {n["niveau"]: n["compte_caisse"] for n in lignes[str(agence.id)]["niveaux"]}
    assert niveaux == {"coffre": None, "principale": None}


def test_rattacher_niveau_ok(client: TestClient, db: Session, agence: Agency) -> None:
    comptable = _utilisateur(db, agence, "COMPTABLE")

    reponse = client.patch(
        f"/caisse/agences/{agence.id}/niveaux/coffre",
        json={"compte_caisse": "101111", "motif": "Paramétrage initial"},
        headers=_entete(comptable, agence, "COMPTABLE"),
    )

    assert reponse.status_code == 200
    niveaux = {n["niveau"]: n["compte_caisse"] for n in reponse.json()["niveaux"]}
    assert niveaux["coffre"]["account_number"] == "101111"
    assert niveaux["principale"] is None

    ligne = db.execute(
        text(
            "SELECT compte_caisse_id FROM caisse.niveaux_caisse "
            "WHERE agency_id = :a AND niveau = 'coffre'"
        ),
        {"a": agence.id},
    ).scalar_one()
    assert ligne == _cid(db, "101111")


def test_rattacher_niveau_hors_1011_refuse(
    client: TestClient, db: Session, agence: Agency
) -> None:
    comptable = _utilisateur(db, agence, "COMPTABLE")

    reponse = client.patch(
        f"/caisse/agences/{agence.id}/niveaux/principale",
        json={"compte_caisse": "251111", "motif": "Tentative"},
        headers=_entete(comptable, agence, "COMPTABLE"),
    )

    assert reponse.status_code == 422
    assert "1011" in reponse.json()["detail"]
    n = db.execute(
        text("SELECT count(*) FROM caisse.niveaux_caisse WHERE agency_id = :a"), {"a": agence.id}
    ).scalar_one()
    assert n == 0  # rien n'a été écrit


def test_rattacher_niveau_invalide_dans_lurl_refuse(
    client: TestClient, db: Session, agence: Agency
) -> None:
    comptable = _utilisateur(db, agence, "COMPTABLE")

    reponse = client.patch(
        f"/caisse/agences/{agence.id}/niveaux/secondaire",
        json={"compte_caisse": "101111", "motif": "Tentative"},
        headers=_entete(comptable, agence, "COMPTABLE"),
    )

    assert reponse.status_code == 422


def test_rattacher_niveau_exige_compta_plan_manage(
    client: TestClient, db: Session, agence: Agency
) -> None:
    caissier = _utilisateur(db, agence, "CAISSIER")

    reponse = client.patch(
        f"/caisse/agences/{agence.id}/niveaux/coffre",
        json={"compte_caisse": "101111", "motif": "Tentative"},
        headers=_entete(caissier, agence, "CAISSIER"),
    )

    assert reponse.status_code == 403


def test_rattacher_niveau_agence_introuvable(
    client: TestClient, db: Session, agence: Agency
) -> None:
    comptable = _utilisateur(db, agence, "COMPTABLE")

    reponse = client.patch(
        f"/caisse/agences/{uuid.uuid4()}/niveaux/coffre",
        json={"compte_caisse": "101111", "motif": "Tentative"},
        headers=_entete(comptable, agence, "COMPTABLE"),
    )

    assert reponse.status_code == 404


def test_rattacher_niveau_vider_est_legitime(
    client: TestClient, db: Session, agence: Agency
) -> None:
    comptable = _utilisateur(db, agence, "COMPTABLE")
    client.patch(
        f"/caisse/agences/{agence.id}/niveaux/coffre",
        json={"compte_caisse": "101111", "motif": "Paramétrage initial"},
        headers=_entete(comptable, agence, "COMPTABLE"),
    )

    reponse = client.patch(
        f"/caisse/agences/{agence.id}/niveaux/coffre",
        json={"compte_caisse": None, "motif": "Retrait du rattachement"},
        headers=_entete(comptable, agence, "COMPTABLE"),
    )

    assert reponse.status_code == 200
    niveaux = {n["niveau"]: n["compte_caisse"] for n in reponse.json()["niveaux"]}
    assert niveaux["coffre"] is None
