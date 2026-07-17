"""Vérifie le seed du socle Sécurité : 11 rôles système, 17 permissions, matrice (§4, §5)."""

from collections.abc import Generator

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.cli.seed_security import MATRICE, PERMISSIONS, ROLES, executer_seed
from app.core.database import SessionLocal

pytestmark = pytest.mark.integration


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """Session dont la transaction est toujours annulée : le seed n'est jamais committé ici."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def _codes_accordes(db: Session, role_code: str) -> set[str]:
    lignes = db.execute(
        text(
            "SELECT p.code "
            "  FROM security.role_permissions rp "
            "  JOIN security.roles r ON r.id = rp.role_id "
            "  JOIN security.permissions p ON p.id = rp.permission_id "
            " WHERE r.code = :role_code"
        ),
        {"role_code": role_code},
    ).scalars()
    return set(lignes)


def test_les_donnees_declarees_sont_coherentes() -> None:
    # Garde-fou hors base : la matrice ne peut citer que des rôles et permissions déclarés.
    codes_roles = {role.code for role in ROLES}
    codes_permissions = {permission.code for permission in PERMISSIONS}

    assert len(ROLES) == 11
    assert len(PERMISSIONS) == 17
    assert set(MATRICE) == codes_roles
    for role_code, accordees in MATRICE.items():
        assert accordees <= codes_permissions, f"{role_code} cite une permission inconnue"


def test_le_seed_installe_les_roles_et_permissions(session: Session) -> None:
    executer_seed(session)

    nb_roles = session.execute(
        text("SELECT count(*) FROM security.roles WHERE is_system")
    ).scalar_one()
    nb_permissions = session.execute(text("SELECT count(*) FROM security.permissions")).scalar_one()

    assert nb_roles == 11
    assert nb_permissions == 17


def test_le_seed_est_idempotent(session: Session) -> None:
    premier = executer_seed(session)
    second = executer_seed(session)

    assert premier.roles == second.roles
    assert premier.accords == second.accords
    # Rien à révoquer au second passage : la base est déjà convergée.
    assert second.revocations == 0

    nb_roles = session.execute(text("SELECT count(*) FROM security.roles")).scalar_one()
    nb_liens = session.execute(text("SELECT count(*) FROM security.role_permissions")).scalar_one()
    assert nb_roles == 11
    assert nb_liens == sum(len(accordees) for accordees in MATRICE.values())


def test_la_base_reflete_exactement_la_matrice(session: Session) -> None:
    executer_seed(session)

    for role_code, accordees in MATRICE.items():
        assert _codes_accordes(session, role_code) == set(accordees), role_code


def test_les_roles_operationnels_nont_aucun_droit_securite(session: Session) -> None:
    # Moindre privilège : un caissier n'a rien à faire dans users.* ni roles.*.
    executer_seed(session)

    for role_code in (
        "CAISSIER",
        "CHARGE_CLIENTELE",
        "CHARGE_PRET",
        "MEMBRE_COMITE_CREDIT",
        "COMPTABLE",
    ):
        assert _codes_accordes(session, role_code) == set(), role_code


def test_lauditeur_interne_est_en_lecture_seule(session: Session) -> None:
    executer_seed(session)

    accordees = _codes_accordes(session, "AUDITEUR_INTERNE")

    assert "audit.read" in accordees
    assert "audit.export" in accordees
    interdits = {
        "users.create",
        "users.update",
        "users.delete",
        "users.unlock",
        "users.reset_password",
        "users.reset_2fa",
        "users.manage_agencies",
        "roles.create",
        "roles.update",
        "roles.delete",
        "roles.assign",
        "sessions.revoke",
    }
    assert accordees & interdits == set()


def test_la_separation_des_pouvoirs_est_respectee(session: Session) -> None:
    # Personne ne détient à la fois « définir un rôle » et « l'attribuer » : sinon un
    # administrateur pourrait forger un rôle sur mesure puis se l'octroyer.
    executer_seed(session)

    for role_code in MATRICE:
        accordees = _codes_accordes(session, role_code)
        definit = accordees & {"roles.create", "roles.update", "roles.delete"}
        attribue = "roles.assign" in accordees
        assert not (definit and attribue), role_code


def test_une_permission_hors_matrice_est_revoquee(session: Session) -> None:
    # Convergence : un droit retiré de la matrice doit disparaître des bases déjà installées.
    executer_seed(session)

    session.execute(
        text(
            "INSERT INTO security.role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM security.roles r, security.permissions p "
            " WHERE r.code = 'CAISSIER' AND p.code = 'users.delete'"
        )
    )
    assert "users.delete" in _codes_accordes(session, "CAISSIER")

    rapport = executer_seed(session)

    assert rapport.revocations == 1
    assert _codes_accordes(session, "CAISSIER") == set()


def test_les_habilitations_dun_role_personnalise_sont_preservees(session: Session) -> None:
    # La convergence ne touche que les rôles système : les rôles d'une IMF lui appartiennent.
    executer_seed(session)
    session.execute(
        text(
            "INSERT INTO security.roles (code, name, is_system) "
            "VALUES ('ROLE_MAISON', 'Rôle propre à l''IMF', FALSE)"
        )
    )
    session.execute(
        text(
            "INSERT INTO security.role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM security.roles r, security.permissions p "
            " WHERE r.code = 'ROLE_MAISON' AND p.code = 'users.read'"
        )
    )

    rapport = executer_seed(session)

    assert rapport.revocations == 0
    assert _codes_accordes(session, "ROLE_MAISON") == {"users.read"}


def test_les_profils_sensibles_imposent_la_2fa(session: Session) -> None:
    executer_seed(session)

    lignes = session.execute(
        text("SELECT code FROM security.roles WHERE requires_2fa AND password_expiry_days = 60")
    ).scalars()

    assert set(lignes) == {
        "AUDITEUR_INTERNE",
        "DIRECTION_GENERALE",
        "ADMIN_FONCTIONNEL",
        "ADMIN_TECHNIQUE",
    }
