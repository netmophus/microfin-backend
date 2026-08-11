"""API rattachement comptable de la caisse par agence (Bloc 5 du paramétrage comptable).

Même patron que les rattachements épargne : lecture résolue en numéro (jamais un UUID),
motif obligatoire et tracé, vider le rattachement est légitime, et le DOUBLE garde-fou sur
les comptes (sélecteur qui filtre à l'affichage + compte_saisie_actif qui revérifie à
l'écriture) est prouvé en contournant le sélecteur directement.
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.caisse.models import Poste
from app.modules.comptabilite.models import Account
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


def _agence_id(db: Session) -> uuid.UUID:
    return db.execute(text("SELECT id FROM parameters.agencies LIMIT 1")).scalar_one()


def _entete_auth(db: Session, role_code: str) -> dict[str, str]:
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


def _compte(db: Session, numero: str, **overrides: object) -> Account:
    valeurs = {
        "account_number": numero,
        "name": f"Compte {numero}",
        "account_class": int(numero[0]),
        "normal_side": "D",
        "is_posting": True,
        "is_system": False,
        **overrides,
    }
    compte = Account(**valeurs)
    db.add(compte)
    db.flush()
    return compte


def _agence(db: Session, **overrides: object) -> Agency:
    valeurs = {"code": f"AG{uuid.uuid4().hex[:6]}", "name": "Agence de test", **overrides}
    agence = Agency(**valeurs)
    db.add(agence)
    db.flush()
    return agence


def test_lecture_resout_le_compte_en_numero(client: TestClient, db: Session) -> None:
    caisse = _compte(db, "5T9A1", normal_side="D")
    _agence(db, code="AG-LEC", compte_caisse_id=caisse.id)
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.get("/agencies/rattachements", headers=comptable)

    ligne = next(a for a in reponse.json() if a["code"] == "AG-LEC")
    assert ligne["compte_caisse"] == {"account_number": "5T9A1", "name": "Compte 5T9A1"}


def test_modification_reussie_avec_motif_trace(client: TestClient, db: Session) -> None:
    ancien = _compte(db, "5T9A2", normal_side="D")
    _compte(db, "5T9A3", normal_side="D")
    agence = _agence(db, code="AG-MOD", compte_caisse_id=ancien.id)
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.patch(
        f"/agencies/{agence.id}/compte-caisse",
        json={"compte_caisse": "5T9A3", "motif": "Correction du compte de caisse"},
        headers=comptable,
    )

    assert reponse.status_code == 200
    assert reponse.json()["compte_caisse"]["account_number"] == "5T9A3"

    ligne = db.execute(
        text(
            "SELECT old_values, new_values FROM audit.audit_logs "
            "WHERE action = 'parameters.agency.compte_caisse_updated' AND resource_id = :r"
        ),
        {"r": agence.id},
    ).one()
    assert ligne.old_values["compte_caisse"] == "5T9A2"
    assert ligne.new_values["compte_caisse"] == "5T9A3"
    assert ligne.new_values["motif"] == "Correction du compte de caisse"


def test_vider_le_rattachement_est_legitime(client: TestClient, db: Session) -> None:
    caisse = _compte(db, "5T9A4", normal_side="D")
    agence = _agence(db, code="AG-VIDE", compte_caisse_id=caisse.id)
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.patch(
        f"/agencies/{agence.id}/compte-caisse",
        json={"compte_caisse": None, "motif": "Retrait du rattachement, à reconfigurer"},
        headers=comptable,
    )

    assert reponse.status_code == 200
    assert reponse.json()["compte_caisse"] is None


def test_motif_absent_refuse(client: TestClient, db: Session) -> None:
    agence = _agence(db, code="AG-NOMOTIF")
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.patch(
        f"/agencies/{agence.id}/compte-caisse",
        json={"compte_caisse": None, "motif": ""},
        headers=comptable,
    )
    assert reponse.status_code == 422


def test_agence_introuvable_404(client: TestClient, db: Session) -> None:
    comptable = _entete_auth(db, "COMPTABLE")
    reponse = client.patch(
        f"/agencies/{uuid.uuid4()}/compte-caisse",
        json={"compte_caisse": None, "motif": "Tentative"},
        headers=comptable,
    )
    assert reponse.status_code == 404


def test_modification_sans_permission_403(client: TestClient, db: Session) -> None:
    agence = _agence(db, code="AG-403")
    caissier = _entete_auth(db, "CAISSIER")
    reponse = client.patch(
        f"/agencies/{agence.id}/compte-caisse",
        json={"compte_caisse": None, "motif": "Tentative"},
        headers=caissier,
    )
    assert reponse.status_code == 403


# --- Double garde-fou : contourner le sélecteur, prouver que ça mord quand même -----------


def test_compte_de_regroupement_soumis_directement_est_refuse(
    client: TestClient, db: Session
) -> None:
    regroupement = _compte(db, "5T9A5", is_posting=False, normal_side="D")
    agence = _agence(db, code="AG-GROUP")
    db.commit()  # checkpoint : le rollback de la requête (422) ne doit pas emporter ce setup.
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.patch(
        f"/agencies/{agence.id}/compte-caisse",
        json={
            "compte_caisse": regroupement.account_number,
            "motif": "Tentative de contournement du sélecteur",
        },
        headers=comptable,
    )

    assert reponse.status_code == 422
    assert "regroupement" in reponse.json()["detail"].lower()
    assert (
        db.execute(select(Agency.compte_caisse_id).where(Agency.id == agence.id)).scalar_one()
        is None
    )


def test_compte_desactive_soumis_directement_est_refuse(client: TestClient, db: Session) -> None:
    desactive = _compte(db, "5T9A6", is_active=False, normal_side="D")
    agence = _agence(db, code="AG-INACTIF")
    db.commit()
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.patch(
        f"/agencies/{agence.id}/compte-caisse",
        json={
            "compte_caisse": desactive.account_number,
            "motif": "Tentative de contournement du sélecteur",
        },
        headers=comptable,
    )

    assert reponse.status_code == 422
    assert (
        db.execute(select(Agency.compte_caisse_id).where(Agency.id == agence.id)).scalar_one()
        is None
    )


# --- Coexistence Bloc A/B : divergence entre le rattachement agence et les postes ----------


def test_pas_de_divergence_quand_les_comptes_concordent(client: TestClient, db: Session) -> None:
    caisse = _compte(db, "5T9A7", normal_side="D")
    agence = _agence(db, code="AG-OK", compte_caisse_id=caisse.id)
    db.add(Poste(agency_id=agence.id, code="01", libelle="Principal", compte_caisse_id=caisse.id))
    db.flush()
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.get("/agencies/rattachements", headers=comptable)

    ligne = next(a for a in reponse.json() if a["code"] == "AG-OK")
    assert ligne["postes_divergents"] == []


def test_divergence_signalee_quand_le_poste_pointe_ailleurs(
    client: TestClient, db: Session
) -> None:
    """Le point que la coexistence Bloc A/B expose au risque de dérive silencieuse : un
    comptable qui repointe ce rattachement doit voir, ici même, que le poste « 01 » diverge."""
    ancien = _compte(db, "5T9A8", normal_side="D")
    nouveau = _compte(db, "5T9A9", normal_side="D")
    agence = _agence(db, code="AG-DIV", compte_caisse_id=nouveau.id)
    db.add(
        Poste(agency_id=agence.id, code="01", libelle="Principal", compte_caisse_id=ancien.id)
    )
    db.flush()
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.get("/agencies/rattachements", headers=comptable)

    ligne = next(a for a in reponse.json() if a["code"] == "AG-DIV")
    assert len(ligne["postes_divergents"]) == 1
    assert ligne["postes_divergents"][0]["code"] == "01"
    assert ligne["postes_divergents"][0]["compte_caisse"]["account_number"] == "5T9A8"


def test_poste_inactif_ninfluence_pas_la_divergence(client: TestClient, db: Session) -> None:
    ancien = _compte(db, "5T9B1", normal_side="D")
    nouveau = _compte(db, "5T9B2", normal_side="D")
    agence = _agence(db, code="AG-INACT", compte_caisse_id=nouveau.id)
    db.add(
        Poste(
            agency_id=agence.id,
            code="01",
            libelle="Fermé",
            compte_caisse_id=ancien.id,
            is_active=False,
        )
    )
    db.flush()
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.get("/agencies/rattachements", headers=comptable)

    ligne = next(a for a in reponse.json() if a["code"] == "AG-INACT")
    assert ligne["postes_divergents"] == []
