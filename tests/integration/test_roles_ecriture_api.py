"""Écritures sur les rôles PERSONNALISÉS (lot 2) — POST/PATCH/DELETE /roles,
PUT /roles/{code}/permissions.

Ce que ces tests protègent :

  - RÔLES SYSTÈME HORS PÉRIMÈTRE : les quatre écritures refusent proprement (403) sur un
    rôle is_system=True — l'édition système est le lot 4, pas celui-ci.
  - is_system TOUJOURS FAUX À LA CRÉATION, jamais un paramètre client.
  - LE GARDE-FOU ANTI-BLOCAGE : il doit rester à tout moment au moins un rôle actif qui
    détient roles.permissions.manage, sur le chemin PUT .../permissions ET sur DELETE.
  - L'AUDIT DIT VRAI : chaque écriture pose une ligne, la lecture n'en pose aucune.

executer_seed(db) garantit que roles.permissions.manage et le reste de la matrice
courante existent dans CETTE transaction de test, indépendamment du moment où le vrai
seed aura été rejoué en base de dev (même patron que test_roles_api.py).
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.cli.seed_security import executer_seed
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


@pytest.fixture
def agence(db: Session) -> Agency:
    agence = Agency(code=f"AG-{uuid.uuid4().hex[:6]}", name="Agence de test")
    db.add(agence)
    db.flush()
    return agence


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


def _role_personnalise(db: Session, code: str, *permission_codes: str) -> Role:
    role = Role(code=code, name=f"Rôle {code}", is_system=False)
    db.add(role)
    db.flush()
    for permission_code in permission_codes:
        permission = db.execute(
            select(Permission).where(Permission.code == permission_code)
        ).scalar_one()
        db.add(RolePermission(role_id=role.id, permission_id=permission.id))
    db.flush()
    return role


def _revoquer(db: Session, role_code: str, permission_code: str) -> None:
    db.execute(
        text(
            "DELETE FROM security.role_permissions rp "
            "USING security.roles r, security.permissions p "
            "WHERE rp.role_id = r.id AND rp.permission_id = p.id "
            "  AND r.code = :role_code AND p.code = :permission_code"
        ),
        {"role_code": role_code, "permission_code": permission_code},
    )


# --- POST /roles -------------------------------------------------------------------------


def test_creer_un_role_personnalise(client: TestClient, db: Session, agence: Agency) -> None:
    executer_seed(db)
    technique = _utilisateur(db, "Technique", "ADMIN_TECHNIQUE", agence)

    reponse = client.post(
        "/roles",
        json={"code": "ROLE_TEST", "name": "Rôle de test", "description": "Un rôle personnalisé"},
        headers=_entete(technique, "ADMIN_TECHNIQUE"),
    )

    assert reponse.status_code == 201
    corps = reponse.json()
    assert corps["code"] == "ROLE_TEST"
    assert corps["is_system"] is False
    assert corps["nb_permissions"] == 0

    role = db.execute(select(Role).where(Role.code == "ROLE_TEST")).scalar_one()
    assert role.is_system is False
    # Lot 3 : né verrouillé, seed-security ne le touchera jamais.
    assert role.gere_manuellement is True

    ligne = _audit(db, "role.created")
    assert ligne["resource_id"] == role.id
    assert isinstance(ligne["new_values"], dict)
    assert ligne["new_values"]["code"] == "ROLE_TEST"


def test_creer_un_role_exige_roles_create(client: TestClient, db: Session, agence: Agency) -> None:
    executer_seed(db)
    caissier = _utilisateur(db, "Sans", "CAISSIER", agence)

    reponse = client.post(
        "/roles",
        json={"code": "ROLE_TEST", "name": "Rôle de test"},
        headers=_entete(caissier, "CAISSIER"),
    )

    assert reponse.status_code == 403


def test_creer_un_role_code_deja_utilise(client: TestClient, db: Session, agence: Agency) -> None:
    executer_seed(db)
    technique = _utilisateur(db, "Technique", "ADMIN_TECHNIQUE", agence)

    reponse = client.post(
        "/roles",
        json={"code": "CAISSIER", "name": "Doublon"},
        headers=_entete(technique, "ADMIN_TECHNIQUE"),
    )

    assert reponse.status_code == 409


# --- PATCH /roles/{code} -----------------------------------------------------------------


def test_modifier_un_role_personnalise(client: TestClient, db: Session, agence: Agency) -> None:
    executer_seed(db)
    technique = _utilisateur(db, "Technique", "ADMIN_TECHNIQUE", agence)
    _role_personnalise(db, "ROLE_PERSO")

    reponse = client.patch(
        "/roles/ROLE_PERSO",
        json={"description": "Nouvelle description"},
        headers=_entete(technique, "ADMIN_TECHNIQUE"),
    )

    assert reponse.status_code == 200
    assert reponse.json()["description"] == "Nouvelle description"

    ligne = _audit(db, "role.updated")
    assert ligne["new_values"] == {"description": "Nouvelle description"}

    # Lot 3 : cette modification verrouille le rôle.
    role = db.execute(select(Role).where(Role.code == "ROLE_PERSO")).scalar_one()
    assert role.gere_manuellement is True


def test_modifier_un_role_systeme_refuse(client: TestClient, db: Session, agence: Agency) -> None:
    executer_seed(db)
    technique = _utilisateur(db, "Technique", "ADMIN_TECHNIQUE", agence)

    reponse = client.patch(
        "/roles/CAISSIER",
        json={"description": "Tentative"},
        headers=_entete(technique, "ADMIN_TECHNIQUE"),
    )

    assert reponse.status_code == 403


def test_modifier_un_role_exige_roles_update(
    client: TestClient, db: Session, agence: Agency
) -> None:
    executer_seed(db)
    caissier = _utilisateur(db, "Sans", "CAISSIER", agence)
    _role_personnalise(db, "ROLE_PERSO")

    reponse = client.patch(
        "/roles/ROLE_PERSO",
        json={"description": "Tentative"},
        headers=_entete(caissier, "CAISSIER"),
    )

    assert reponse.status_code == 403


# --- DELETE /roles/{code} -----------------------------------------------------------------


def test_supprimer_un_role_personnalise(client: TestClient, db: Session, agence: Agency) -> None:
    executer_seed(db)
    technique = _utilisateur(db, "Technique", "ADMIN_TECHNIQUE", agence)
    role = _role_personnalise(db, "ROLE_PERSO")
    role_id = role.id

    reponse = client.delete("/roles/ROLE_PERSO", headers=_entete(technique, "ADMIN_TECHNIQUE"))

    assert reponse.status_code == 204
    assert db.execute(select(Role).where(Role.id == role_id)).scalar_one_or_none() is None

    ligne = _audit(db, "role.deleted")
    assert ligne["resource_id"] == role_id
    assert isinstance(ligne["old_values"], dict)
    assert ligne["old_values"]["code"] == "ROLE_PERSO"


def test_supprimer_un_role_systeme_refuse(client: TestClient, db: Session, agence: Agency) -> None:
    executer_seed(db)
    technique = _utilisateur(db, "Technique", "ADMIN_TECHNIQUE", agence)

    reponse = client.delete("/roles/CAISSIER", headers=_entete(technique, "ADMIN_TECHNIQUE"))

    assert reponse.status_code == 403
    assert db.execute(select(Role).where(Role.code == "CAISSIER")).scalar_one_or_none() is not None


def test_supprimer_un_role_exige_roles_delete(
    client: TestClient, db: Session, agence: Agency
) -> None:
    executer_seed(db)
    caissier = _utilisateur(db, "Sans", "CAISSIER", agence)
    _role_personnalise(db, "ROLE_PERSO")

    reponse = client.delete("/roles/ROLE_PERSO", headers=_entete(caissier, "CAISSIER"))

    assert reponse.status_code == 403


# --- PUT /roles/{code}/permissions ---------------------------------------------------------


def test_remplacer_les_permissions_dun_role_personnalise(
    client: TestClient, db: Session, agence: Agency
) -> None:
    executer_seed(db)
    technique = _utilisateur(db, "Technique", "ADMIN_TECHNIQUE", agence)
    _role_personnalise(db, "ROLE_PERSO", "tiers.read.basic")

    reponse = client.put(
        "/roles/ROLE_PERSO/permissions",
        json={
            "permission_codes": ["tiers.read.basic", "epargne.account.read"],
            "motif": "Ajustement du périmètre de ce rôle",
        },
        headers=_entete(technique, "ADMIN_TECHNIQUE"),
    )

    assert reponse.status_code == 200
    codes = {p["code"] for p in reponse.json()["permissions"]}
    assert codes == {"tiers.read.basic", "epargne.account.read"}

    ligne = _audit(db, "role.permissions_replaced")
    assert isinstance(ligne["new_values"], dict)
    assert isinstance(ligne["old_values"], dict)
    assert set(ligne["new_values"]["permissions"]) == {"tiers.read.basic", "epargne.account.read"}
    assert ligne["new_values"]["motif"] == "Ajustement du périmètre de ce rôle"
    assert ligne["old_values"]["permissions"] == ["tiers.read.basic"]

    # Lot 3 : ce remplacement verrouille le rôle.
    role = db.execute(select(Role).where(Role.code == "ROLE_PERSO")).scalar_one()
    assert role.gere_manuellement is True


def test_remplacer_les_permissions_dun_role_systeme_refuse(
    client: TestClient, db: Session, agence: Agency
) -> None:
    executer_seed(db)
    technique = _utilisateur(db, "Technique", "ADMIN_TECHNIQUE", agence)

    reponse = client.put(
        "/roles/CAISSIER/permissions",
        json={"permission_codes": [], "motif": "Tentative"},
        headers=_entete(technique, "ADMIN_TECHNIQUE"),
    )

    assert reponse.status_code == 403


def test_remplacer_les_permissions_exige_roles_permissions_manage(
    client: TestClient, db: Session, agence: Agency
) -> None:
    executer_seed(db)
    # ADMIN_FONCTIONNEL détient roles.update mais pas roles.permissions.manage.
    fonctionnel = _utilisateur(db, "Sans", "ADMIN_FONCTIONNEL", agence)
    _role_personnalise(db, "ROLE_PERSO")

    reponse = client.put(
        "/roles/ROLE_PERSO/permissions",
        json={"permission_codes": [], "motif": "Tentative"},
        headers=_entete(fonctionnel, "ADMIN_FONCTIONNEL"),
    )

    assert reponse.status_code == 403


def test_remplacer_les_permissions_code_inconnu(
    client: TestClient, db: Session, agence: Agency
) -> None:
    executer_seed(db)
    technique = _utilisateur(db, "Technique", "ADMIN_TECHNIQUE", agence)
    _role_personnalise(db, "ROLE_PERSO")

    reponse = client.put(
        "/roles/ROLE_PERSO/permissions",
        json={"permission_codes": ["ceci.nexiste.pas"], "motif": "Tentative"},
        headers=_entete(technique, "ADMIN_TECHNIQUE"),
    )

    assert reponse.status_code == 422


def test_remplacer_les_permissions_motif_obligatoire(
    client: TestClient, db: Session, agence: Agency
) -> None:
    executer_seed(db)
    technique = _utilisateur(db, "Technique", "ADMIN_TECHNIQUE", agence)
    _role_personnalise(db, "ROLE_PERSO")

    reponse = client.put(
        "/roles/ROLE_PERSO/permissions",
        json={"permission_codes": [], "motif": ""},
        headers=_entete(technique, "ADMIN_TECHNIQUE"),
    )

    assert reponse.status_code == 422


# --- garde-fou anti-blocage : au moins un gardien de roles.permissions.manage -------------


def test_anti_blocage_refuse_de_retirer_le_dernier_gardien_via_put(
    client: TestClient, db: Session, agence: Agency
) -> None:
    """ROLE_GARDIEN devient le SEUL porteur de roles.permissions.manage (on la retire à
    ADMIN_TECHNIQUE) : le PUT qui l'en priverait doit être refusé, 422."""
    executer_seed(db)
    _revoquer(db, "ADMIN_TECHNIQUE", "roles.permissions.manage")
    _role_personnalise(db, "ROLE_GARDIEN", "roles.permissions.manage")
    # L'acteur doit lui-même détenir roles.permissions.manage pour atteindre l'endpoint —
    # ROLE_GARDIEN étant désormais le SEUL porteur, l'acteur porte ce rôle-là.
    gardien = _utilisateur(db, "Gardien", "ROLE_GARDIEN", agence)

    reponse = client.put(
        "/roles/ROLE_GARDIEN/permissions",
        json={"permission_codes": [], "motif": "Retrait du dernier gardien"},
        headers=_entete(gardien, "ROLE_GARDIEN"),
    )

    assert reponse.status_code == 422
    assert "gérer les permissions" in reponse.json()["detail"]
    # Le rôle garde bien sa permission : rien n'a été écrit.
    lignes = db.execute(
        text(
            "SELECT p.code FROM security.role_permissions rp "
            "  JOIN security.roles r ON r.id = rp.role_id "
            "  JOIN security.permissions p ON p.id = rp.permission_id "
            " WHERE r.code = 'ROLE_GARDIEN'"
        )
    ).scalars()
    assert set(lignes) == {"roles.permissions.manage"}


def test_anti_blocage_refuse_de_supprimer_le_dernier_gardien(
    client: TestClient, db: Session, agence: Agency
) -> None:
    """Même garde-fou, chemin DELETE : supprimer ROLE_GARDIEN le ferait disparaître lui et
    sa permission — refusé pour la même raison que le PUT."""
    executer_seed(db)
    _revoquer(db, "ADMIN_TECHNIQUE", "roles.permissions.manage")
    role = _role_personnalise(db, "ROLE_GARDIEN", "roles.permissions.manage")
    role_id = role.id
    technique = _utilisateur(db, "Technique", "ADMIN_TECHNIQUE", agence)

    reponse = client.delete(
        "/roles/ROLE_GARDIEN", headers=_entete(technique, "ADMIN_TECHNIQUE")
    )

    assert reponse.status_code == 422
    assert db.execute(select(Role).where(Role.id == role_id)).scalar_one_or_none() is not None


def test_anti_blocage_autorise_si_un_autre_role_reste_gardien(
    client: TestClient, db: Session, agence: Agency
) -> None:
    """Contre-épreuve : ADMIN_TECHNIQUE garde roles.permissions.manage (cas réel du lot 2) —
    retirer un rôle personnalisé qui NE la détient pas ne doit jamais être bloqué."""
    executer_seed(db)
    role = _role_personnalise(db, "ROLE_ORDINAIRE", "tiers.read.basic")
    role_id = role.id
    technique = _utilisateur(db, "Technique", "ADMIN_TECHNIQUE", agence)

    reponse = client.delete(
        "/roles/ROLE_ORDINAIRE", headers=_entete(technique, "ADMIN_TECHNIQUE")
    )

    assert reponse.status_code == 204
    assert db.execute(select(Role).where(Role.id == role_id)).scalar_one_or_none() is None
