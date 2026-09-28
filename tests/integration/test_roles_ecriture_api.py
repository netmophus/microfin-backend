"""Écritures sur les rôles (lot 2/4) — POST/PATCH/DELETE /roles, PUT /roles/{code}/permissions,
POST /roles/{code}/reinitialiser.

Ce que ces tests protègent :

  - PATCH et PUT .../permissions s'appliquent DÉSORMAIS aux rôles système (lot 4) — et les
    verrouillent (gere_manuellement=TRUE). Seul DELETE reste interdit sur un rôle système
    (on n'en supprime jamais un — le trigger DB, migration 0004, le confirme).
  - is_system TOUJOURS FAUX À LA CRÉATION, jamais un paramètre client.
  - LE GARDE-FOU ANTI-BLOCAGE : il doit rester à tout moment au moins un rôle actif qui
    détient roles.permissions.manage, sur PUT .../permissions, DELETE ET réinitialiser.
  - RÉINITIALISER (lot 4) rejoue le seed pour un rôle système verrouillé, qui redevient
    alors sous contrôle du seed — recoupé avec le test qui mord du lot 3
    (test_gere_manuellement_protege_les_trois_convergences).
  - L'AUDIT DIT VRAI : chaque écriture pose une ligne, la lecture n'en pose aucune.

executer_seed(db) garantit que roles.permissions.manage et le reste de la matrice
courante existent dans CETTE transaction de test, indépendamment du moment où le vrai
seed aura été rejoué en base de dev (même patron que test_roles_api.py).
"""

import uuid
from collections.abc import Generator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.cli.seed_security import MATRICE, executer_seed
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


def test_modifier_un_role_systeme_autorise_et_verrouille(
    client: TestClient, db: Session, agence: Agency
) -> None:
    """Lot 4 : un rôle système reste modifiable, mais l'édition le verrouille — c'est ce
    qui rend l'édition sûre (le seed ne l'écrasera plus, voir lot 3)."""
    executer_seed(db)
    technique = _utilisateur(db, "Technique", "ADMIN_TECHNIQUE", agence)
    role_avant = db.execute(select(Role).where(Role.code == "CAISSIER")).scalar_one()
    assert role_avant.gere_manuellement is False

    reponse = client.patch(
        "/roles/CAISSIER",
        json={"description": "Description modifiée à l'écran"},
        headers=_entete(technique, "ADMIN_TECHNIQUE"),
    )

    assert reponse.status_code == 200
    assert reponse.json()["description"] == "Description modifiée à l'écran"
    assert reponse.json()["gere_manuellement"] is True

    role = db.execute(select(Role).where(Role.code == "CAISSIER")).scalar_one()
    assert role.gere_manuellement is True


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
    assert reponse.json()["detail"] == "Un rôle système ne peut pas être supprimé."
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


def test_remplacer_les_permissions_dun_role_systeme_autorise_et_verrouille(
    client: TestClient, db: Session, agence: Agency
) -> None:
    """Lot 4 : PUT .../permissions s'applique aussi aux rôles système, et verrouille."""
    executer_seed(db)
    technique = _utilisateur(db, "Technique", "ADMIN_TECHNIQUE", agence)

    reponse = client.put(
        "/roles/CHARGE_CLIENTELE/permissions",
        json={"permission_codes": ["tiers.read.basic"], "motif": "Réduction du périmètre"},
        headers=_entete(technique, "ADMIN_TECHNIQUE"),
    )

    assert reponse.status_code == 200
    assert reponse.json()["gere_manuellement"] is True
    codes = {p["code"] for p in reponse.json()["permissions"]}
    assert codes == {"tiers.read.basic"}

    role = db.execute(select(Role).where(Role.code == "CHARGE_CLIENTELE")).scalar_one()
    assert role.gere_manuellement is True


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


def test_anti_blocage_sur_put_retire_dernier_gardien_admin_technique(
    client: TestClient, db: Session, agence: Agency
) -> None:
    """Lot 4, demande explicite : PUT .../permissions sur ADMIN_TECHNIQUE lui-même, alors
    qu'il est le SEUL porteur de roles.permissions.manage, doit être refusé."""
    executer_seed(db)
    technique = _utilisateur(db, "Technique", "ADMIN_TECHNIQUE", agence)

    reponse = client.put(
        "/roles/ADMIN_TECHNIQUE/permissions",
        json={"permission_codes": ["sessions.read"], "motif": "Retrait de mes propres droits"},
        headers=_entete(technique, "ADMIN_TECHNIQUE"),
    )

    assert reponse.status_code == 422
    assert "gérer les permissions" in reponse.json()["detail"]
    role = db.execute(select(Role).where(Role.code == "ADMIN_TECHNIQUE")).scalar_one()
    assert role.gere_manuellement is False  # rien n'a été écrit, y compris le verrou


# --- POST /roles/{code}/reinitialiser (lot 4) ----------------------------------------------


def test_reinitialiser_un_role_systeme_verrouille(
    client: TestClient, db: Session, agence: Agency
) -> None:
    """Édite CAISSIER (le verrouille), puis réinitialise : métadonnées ET permissions
    reviennent EXACTEMENT à ce que déclare la matrice, le verrou se lève."""
    executer_seed(db)
    technique = _utilisateur(db, "Technique", "ADMIN_TECHNIQUE", agence)

    client.patch(
        "/roles/CAISSIER",
        json={"name": "Nom modifié à l'écran", "description": "Description modifiée"},
        headers=_entete(technique, "ADMIN_TECHNIQUE"),
    )
    client.put(
        "/roles/CAISSIER/permissions",
        json={"permission_codes": ["tiers.read.basic"], "motif": "Réduction"},
        headers=_entete(technique, "ADMIN_TECHNIQUE"),
    )
    role_verrouille = db.execute(select(Role).where(Role.code == "CAISSIER")).scalar_one()
    assert role_verrouille.gere_manuellement is True

    reponse = client.post(
        "/roles/CAISSIER/reinitialiser",
        json={},
        headers=_entete(technique, "ADMIN_TECHNIQUE"),
    )

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["gere_manuellement"] is False
    assert corps["name"] == "Caissier"
    assert corps["description"] == "Opérations de guichet, encaissements/décaissements"
    assert {p["code"] for p in corps["permissions"]} == set(MATRICE["CAISSIER"])

    role = db.execute(select(Role).where(Role.code == "CAISSIER")).scalar_one()
    assert role.gere_manuellement is False
    assert role.name == "Caissier"

    ligne = _audit(db, "role.reset_to_default")
    assert ligne["resource_id"] == role.id


def test_reinitialiser_recoupe_avec_le_test_qui_mord_du_lot_3(
    client: TestClient, db: Session, agence: Agency
) -> None:
    """Recoupement explicite avec le lot 3 : après réinitialisation, CAISSIER doit être
    REDEVENU un rôle ordinaire sous contrôle du seed — un executer_seed ultérieur ne doit
    plus rien y trouver à corriger."""
    executer_seed(db)
    technique = _utilisateur(db, "Technique", "ADMIN_TECHNIQUE", agence)

    client.patch(
        "/roles/CAISSIER",
        json={"name": "Nom modifié"},
        headers=_entete(technique, "ADMIN_TECHNIQUE"),
    )
    client.post(
        "/roles/CAISSIER/reinitialiser", json={}, headers=_entete(technique, "ADMIN_TECHNIQUE")
    )

    executer_seed(db)

    # Auto-contenu : on ne dépend pas de la convergence globale de la base (d'autres rôles
    # pourraient avoir leurs propres écarts, hors du périmètre de ce test), seulement de
    # CAISSIER — la preuve qu'IL est redevenu sous contrôle du seed.
    role = db.execute(select(Role).where(Role.code == "CAISSIER")).scalar_one()
    assert role.name == "Caissier"
    assert role.gere_manuellement is False
    codes = set(
        db.execute(
            text(
                "SELECT p.code FROM security.role_permissions rp "
                "  JOIN security.roles r ON r.id = rp.role_id "
                "  JOIN security.permissions p ON p.id = rp.permission_id "
                " WHERE r.code = 'CAISSIER'"
            )
        ).scalars()
    )
    assert codes == set(MATRICE["CAISSIER"])


def test_reinitialiser_role_non_verrouille_refuse(
    client: TestClient, db: Session, agence: Agency
) -> None:
    executer_seed(db)
    technique = _utilisateur(db, "Technique", "ADMIN_TECHNIQUE", agence)

    reponse = client.post(
        "/roles/CAISSIER/reinitialiser", json={}, headers=_entete(technique, "ADMIN_TECHNIQUE")
    )

    assert reponse.status_code == 422


def test_reinitialiser_role_personnalise_refuse(
    client: TestClient, db: Session, agence: Agency
) -> None:
    executer_seed(db)
    technique = _utilisateur(db, "Technique", "ADMIN_TECHNIQUE", agence)
    _role_personnalise(db, "ROLE_PERSO")

    reponse = client.post(
        "/roles/ROLE_PERSO/reinitialiser",
        json={},
        headers=_entete(technique, "ADMIN_TECHNIQUE"),
    )

    assert reponse.status_code == 422


def test_reinitialiser_exige_roles_permissions_manage(
    client: TestClient, db: Session, agence: Agency
) -> None:
    executer_seed(db)
    fonctionnel = _utilisateur(db, "Sans", "ADMIN_FONCTIONNEL", agence)

    reponse = client.post(
        "/roles/CAISSIER/reinitialiser",
        json={},
        headers=_entete(fonctionnel, "ADMIN_FONCTIONNEL"),
    )

    assert reponse.status_code == 403


def test_reinitialiser_motif_facultatif_trace_si_fourni(
    client: TestClient, db: Session, agence: Agency
) -> None:
    executer_seed(db)
    technique = _utilisateur(db, "Technique", "ADMIN_TECHNIQUE", agence)
    client.patch(
        "/roles/CAISSIER",
        json={"description": "Tentative"},
        headers=_entete(technique, "ADMIN_TECHNIQUE"),
    )

    reponse = client.post(
        "/roles/CAISSIER/reinitialiser",
        json={"motif": "Erreur de saisie à corriger"},
        headers=_entete(technique, "ADMIN_TECHNIQUE"),
    )

    assert reponse.status_code == 200
    ligne = _audit(db, "role.reset_to_default")
    assert isinstance(ligne["new_values"], dict)
    assert ligne["new_values"]["motif"] == "Erreur de saisie à corriger"


def test_anti_blocage_sur_reinitialiser(
    client: TestClient, db: Session, agence: Agency
) -> None:
    """Matrice future simulée où ADMIN_TECHNIQUE ne détiendrait plus roles.permissions.
    manage : réinitialiser ADMIN_TECHNIQUE (verrouillé au préalable) doit être refusé,
    puisque plus personne ne la détiendrait après reconvergence."""
    executer_seed(db)
    technique = _utilisateur(db, "Technique", "ADMIN_TECHNIQUE", agence)
    client.patch(
        "/roles/ADMIN_TECHNIQUE",
        json={"description": "Verrouillage préalable"},
        headers=_entete(technique, "ADMIN_TECHNIQUE"),
    )

    matrice_sans_gardienne = dict(MATRICE)
    matrice_sans_gardienne["ADMIN_TECHNIQUE"] = frozenset(
        MATRICE["ADMIN_TECHNIQUE"] - {"roles.permissions.manage"}
    )

    with patch("app.modules.security.roles_ecriture.MATRICE", matrice_sans_gardienne):
        reponse = client.post(
            "/roles/ADMIN_TECHNIQUE/reinitialiser",
            json={},
            headers=_entete(technique, "ADMIN_TECHNIQUE"),
        )

    assert reponse.status_code == 422
    role = db.execute(select(Role).where(Role.code == "ADMIN_TECHNIQUE")).scalar_one()
    assert role.gere_manuellement is True  # toujours verrouillé, rien n'a bougé
