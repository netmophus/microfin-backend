"""Attribution / retrait de rôles (bloc 4d) — GET /roles, POST/DELETE /users/{id}/roles.

Ce que ces tests protègent :

  - la SÉPARATION DES POUVOIRS : personne ne modifie ses propres rôles ;
  - le PÉRIMÈTRE : on n'attribue pas un rôle à quelqu'un qu'on ne voit pas (404) ;
  - l'AUDIT : chaque attribution et chaque retrait laisse une trace acteur ≠ cible.
"""

import uuid
from collections.abc import Callable, Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.parameters.models import Agency
from app.modules.security.jwt import creer_access_token
from app.modules.security.models import Permission, Role, RolePermission, User, UserRole
from app.modules.security.password import hasher_mot_de_passe

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


@pytest.fixture
def client(db: Session) -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _utilisateur(db: Session, nom: str, role_code: str, agence: Agency) -> User:
    role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
    suffixe = uuid.uuid4().hex[:8]
    user = User(
        matricule=f"MAT-{suffixe}",
        email=f"{suffixe}@example.com",
        username=f"u{suffixe}",
        password_hash=hasher_mot_de_passe("Motdepasse!123"),
        last_name=nom,
        first_name="Test",
        primary_agency_id=agence.id,
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.flush()
    return user


def _entete(user: User, role_code: str) -> dict[str, str]:
    jeton = creer_access_token(
        user_id=user.id, roles=[role_code], primary_agency_id=user.primary_agency_id
    )
    return {"Authorization": f"Bearer {jeton}"}


@pytest.fixture
def accorder(db: Session) -> Callable[[str, str], None]:
    def _accorder(role_code: str, permission_code: str) -> None:
        role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
        permission = db.execute(
            select(Permission).where(Permission.code == permission_code)
        ).scalar_one()
        db.add(RolePermission(role_id=role.id, permission_id=permission.id))
        db.flush()

    return _accorder


@pytest.fixture
def agence(db: Session) -> Agency:
    agence = Agency(code=f"AG-{uuid.uuid4().hex[:6]}", name="Agence de test")
    db.add(agence)
    db.flush()
    return agence


@pytest.fixture
def admin(db: Session, agence: Agency) -> User:
    """ADMIN_FONCTIONNEL détient roles.read + roles.assign + portée réseau."""
    return _utilisateur(db, "Bah", "ADMIN_FONCTIONNEL", agence)


@pytest.fixture
def h_admin(admin: User) -> dict[str, str]:
    return _entete(admin, "ADMIN_FONCTIONNEL")


@pytest.fixture
def cible(db: Session, agence: Agency) -> User:
    return _utilisateur(db, "Diallo", "CAISSIER", agence)


def _roles(db: Session, user_id: uuid.UUID) -> set[str]:
    return set(
        db.execute(
            select(Role.code)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user_id)
        ).scalars()
    )


def _audit(db: Session, action: str) -> dict[str, object]:
    return dict(
        db.execute(
            text(
                "SELECT user_id, resource_id, new_values, old_values FROM audit.audit_logs "
                "WHERE action = :a ORDER BY occurred_at DESC LIMIT 1"
            ),
            {"a": action},
        )
        .mappings()
        .one()
    )


# --- GET /roles ------------------------------------------------------------------------


def test_liste_les_roles(client: TestClient, h_admin: dict[str, str]) -> None:
    reponse = client.get("/roles", headers=h_admin)

    assert reponse.status_code == 200
    codes = {r["code"] for r in reponse.json()}
    assert "CAISSIER" in codes and "ADMIN_FONCTIONNEL" in codes


def test_liste_des_roles_exige_roles_read(client: TestClient, db: Session, agence: Agency) -> None:
    caissier = _utilisateur(db, "Sans", "CAISSIER", agence)  # pas de roles.read

    assert client.get("/roles", headers=_entete(caissier, "CAISSIER")).status_code == 403


# --- attribution ------------------------------------------------------------------------


def test_attribuer_un_role(
    client: TestClient, db: Session, h_admin: dict[str, str], cible: User
) -> None:
    reponse = client.post(
        f"/users/{cible.id}/roles", headers=h_admin, json={"role_code": "COMPTABLE"}
    )

    assert reponse.status_code == 200
    assert "COMPTABLE" in {r["code"] for r in reponse.json()["roles"]}
    assert _roles(db, cible.id) == {"CAISSIER", "COMPTABLE"}


def test_attribuer_sans_roles_assign_403(
    client: TestClient, db: Session, agence: Agency, cible: User
) -> None:
    """RESPONSABLE_AGENCE n'a pas roles.assign."""
    resp = _utilisateur(db, "Resp", "RESPONSABLE_AGENCE", agence)

    reponse = client.post(
        f"/users/{cible.id}/roles",
        headers=_entete(resp, "RESPONSABLE_AGENCE"),
        json={"role_code": "COMPTABLE"},
    )

    assert reponse.status_code == 403


def test_on_ne_modifie_pas_ses_propres_roles(
    client: TestClient, admin: User, h_admin: dict[str, str]
) -> None:
    """Séparation des pouvoirs : sinon un détenteur de roles.assign s'auto-promeut."""
    reponse = client.post(
        f"/users/{admin.id}/roles", headers=h_admin, json={"role_code": "DIRECTION_GENERALE"}
    )

    assert reponse.status_code == 403


def test_attribuer_a_une_cible_hors_perimetre_donne_404(
    client: TestClient,
    db: Session,
    accorder: Callable[[str, str], None],
) -> None:
    """Un responsable de l'agence A ne voit pas un caissier de l'agence B : 404, pas 403."""
    agence_a = Agency(code=f"AG-{uuid.uuid4().hex[:6]}", name="A")
    agence_b = Agency(code=f"AG-{uuid.uuid4().hex[:6]}", name="B")
    db.add_all([agence_a, agence_b])
    db.flush()
    resp_a = _utilisateur(db, "Resp", "RESPONSABLE_AGENCE", agence_a)
    accorder("RESPONSABLE_AGENCE", "roles.assign")
    cible_b = _utilisateur(db, "Traoré", "CAISSIER", agence_b)

    reponse = client.post(
        f"/users/{cible_b.id}/roles",
        headers=_entete(resp_a, "RESPONSABLE_AGENCE"),
        json={"role_code": "COMPTABLE"},
    )

    assert reponse.status_code == 404


def test_role_deja_attribue_donne_409(
    client: TestClient, h_admin: dict[str, str], cible: User
) -> None:
    reponse = client.post(
        f"/users/{cible.id}/roles", headers=h_admin, json={"role_code": "CAISSIER"}
    )

    assert reponse.status_code == 409


def test_role_inexistant_donne_404(
    client: TestClient, h_admin: dict[str, str], cible: User
) -> None:
    reponse = client.post(
        f"/users/{cible.id}/roles", headers=h_admin, json={"role_code": "ROLE_FANTOME"}
    )

    assert reponse.status_code == 404


# --- retrait ----------------------------------------------------------------------------


def test_retirer_un_role(
    client: TestClient, db: Session, h_admin: dict[str, str], cible: User
) -> None:
    client.post(f"/users/{cible.id}/roles", headers=h_admin, json={"role_code": "COMPTABLE"})

    reponse = client.delete(f"/users/{cible.id}/roles/COMPTABLE", headers=h_admin)

    assert reponse.status_code == 200
    assert _roles(db, cible.id) == {"CAISSIER"}


def test_retirer_un_role_non_attribue_donne_404(
    client: TestClient, h_admin: dict[str, str], cible: User
) -> None:
    assert client.delete(f"/users/{cible.id}/roles/COMPTABLE", headers=h_admin).status_code == 404


def test_on_ne_retire_pas_ses_propres_roles(
    client: TestClient, admin: User, h_admin: dict[str, str]
) -> None:
    assert (
        client.delete(f"/users/{admin.id}/roles/ADMIN_FONCTIONNEL", headers=h_admin).status_code
        == 403
    )


# --- audit ------------------------------------------------------------------------------


def test_l_attribution_est_auditee_acteur_et_cible(
    client: TestClient, db: Session, admin: User, h_admin: dict[str, str], cible: User
) -> None:
    client.post(f"/users/{cible.id}/roles", headers=h_admin, json={"role_code": "COMPTABLE"})

    ligne = _audit(db, "user.role_assigned")
    assert ligne["user_id"] == admin.id  # l'acteur
    assert ligne["resource_id"] == cible.id  # la cible
    assert ligne["new_values"] == {"role": "COMPTABLE"}


def test_le_retrait_est_audite(
    client: TestClient, db: Session, admin: User, h_admin: dict[str, str], cible: User
) -> None:
    client.post(f"/users/{cible.id}/roles", headers=h_admin, json={"role_code": "COMPTABLE"})

    client.delete(f"/users/{cible.id}/roles/COMPTABLE", headers=h_admin)

    ligne = _audit(db, "user.role_removed")
    assert ligne["user_id"] == admin.id
    assert ligne["resource_id"] == cible.id
    assert ligne["old_values"] == {"role": "COMPTABLE"}
