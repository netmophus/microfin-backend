"""Service d'audit partagé (bloc 4c) — acteur ≠ cible, old_values, refus des secrets.

Deux garanties tenues ici, et elles ne sont pas de même nature :

  - ACTEUR ≠ CIBLE est une garantie de VÉRITÉ du journal. Confondre les deux ferait dire
    au journal qu'un compte s'est créé lui-même. Dans une table immuable conservée cinq
    ans et opposable au régulateur, un faux ne se corrige pas.
  - LE REFUS DES SECRETS est une garantie de CONFIDENTIALITÉ. Une fuite dans un journal
    immuable ne peut être ni effacée ni réécrite : elle doit être empêchée à l'écriture.
"""

import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine
from app.modules.audit.service import (
    CONTEXTE_VIDE,
    ContexteRequete,
    SecretDansAuditError,
    ecrire_audit,
)
from app.modules.parameters.models import Agency
from app.modules.security.models import Role, User, UserRole

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


def _utilisateur(db: Session, nom: str) -> User:
    role = db.execute(select(Role).where(Role.code == "CAISSIER")).scalar_one()
    suffixe = uuid.uuid4().hex[:8]
    agence = Agency(code=f"AG-{suffixe}", name="Agence de test")
    db.add(agence)
    db.flush()
    user = User(
        matricule=f"MAT-{suffixe}",
        email=f"{suffixe}@example.com",
        username=f"u{suffixe}",
        password_hash="x" * 32,
        last_name=nom,
        first_name="Test",
        primary_agency_id=agence.id,
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.flush()
    return user


def _derniere_ligne(db: Session, action: str) -> dict[str, object]:
    return dict(
        db.execute(
            text(
                "SELECT user_id, action, resource_type, resource_id, old_values, "
                "       new_values, agency_id, ip_address "
                "  FROM audit.audit_logs WHERE action = :action "
                " ORDER BY occurred_at DESC LIMIT 1"
            ),
            {"action": action},
        )
        .mappings()
        .one()
    )


# --- acteur ≠ cible -------------------------------------------------------------------


def test_l_acteur_et_la_cible_sont_journalises_separement(db: Session) -> None:
    """LE point du bloc : le journal doit dire QUI a agi SUR QUI.

    Si resource_id venait à recopier l'acteur, le journal affirmerait qu'un compte s'est
    créé lui-même — un faux définitif dans une table immuable.
    """
    admin = _utilisateur(db, "Admin")
    cible = _utilisateur(db, "Cible")
    action = f"test.user.created.{uuid.uuid4().hex[:8]}"

    ecrire_audit(
        db,
        action=action,
        contexte=CONTEXTE_VIDE,
        acteur_id=admin.id,
        resource_type="user",
        resource_id=cible.id,
    )

    ligne = _derniere_ligne(db, action)
    assert ligne["user_id"] == admin.id
    assert ligne["resource_id"] == cible.id
    assert ligne["resource_type"] == "user"
    assert ligne["user_id"] != ligne["resource_id"]


def test_les_valeurs_avant_et_apres_sont_journalisees(db: Session) -> None:
    admin = _utilisateur(db, "Admin")
    cible = _utilisateur(db, "Cible")
    action = f"test.user.updated.{uuid.uuid4().hex[:8]}"

    ecrire_audit(
        db,
        action=action,
        contexte=CONTEXTE_VIDE,
        acteur_id=admin.id,
        resource_type="user",
        resource_id=cible.id,
        old_values={"phone": "70000000"},
        new_values={"phone": "70111111"},
    )

    ligne = _derniere_ligne(db, action)
    assert ligne["old_values"] == {"phone": "70000000"}
    assert ligne["new_values"] == {"phone": "70111111"}


def test_le_contexte_de_requete_est_journalise(db: Session) -> None:
    admin = _utilisateur(db, "Admin")
    action = f"test.contexte.{uuid.uuid4().hex[:8]}"
    contexte = ContexteRequete(ip="10.1.2.3", user_agent="pytest", request_id=uuid.uuid4())

    ecrire_audit(db, action=action, contexte=contexte, acteur_id=admin.id)

    ligne = _derniere_ligne(db, action)
    # Dette connue : la colonne INET revient en IPv4Address, pas en str.
    assert str(ligne["ip_address"]) == "10.1.2.3"


# --- refus des secrets ------------------------------------------------------------------


@pytest.mark.parametrize(
    "champ",
    [
        "password",
        "password_hash",
        "new_password",
        "mot_de_passe",
        "mot_de_passe_genere",
        "refresh_token_hash",
        "token",
        "secret_encrypted",
        "PASSWORD_HASH",  # la détection ignore la casse
    ],
)
def test_un_champ_sensible_fait_lever(db: Session, champ: str) -> None:
    """LÈVE, et ne filtre pas en silence.

    Un filtre discret protégerait la base mais laisserait le développeur croire qu'il a
    journalisé la valeur. L'erreur doit tomber au premier test, pas être absorbée.
    """
    admin = _utilisateur(db, "Admin")

    with pytest.raises(SecretDansAuditError):
        ecrire_audit(
            db,
            action="test.fuite",
            contexte=CONTEXTE_VIDE,
            acteur_id=admin.id,
            new_values={champ: "valeur"},
        )


def test_le_refus_couvre_aussi_old_values(db: Session) -> None:
    admin = _utilisateur(db, "Admin")

    with pytest.raises(SecretDansAuditError):
        ecrire_audit(
            db,
            action="test.fuite",
            contexte=CONTEXTE_VIDE,
            acteur_id=admin.id,
            old_values={"password_hash": "$argon2id$..."},
        )


def test_un_champ_anodin_passe(db: Session) -> None:
    """Contrôle en miroir : la détection ne doit pas être si large qu'elle bloque tout."""
    admin = _utilisateur(db, "Admin")
    action = f"test.anodin.{uuid.uuid4().hex[:8]}"

    ecrire_audit(
        db,
        action=action,
        contexte=CONTEXTE_VIDE,
        acteur_id=admin.id,
        new_values={"phone": "70000000", "is_active": False, "last_name": "Kané"},
    )

    assert _derniere_ligne(db, action)["new_values"] == {
        "phone": "70000000",
        "is_active": False,
        "last_name": "Kané",
    }


# --- non-régression de l'audit d'authentification ---------------------------------------


def test_l_audit_d_auth_reste_sans_cible_distincte(db: Session) -> None:
    """Pour une connexion, l'acteur EST le sujet : resource_id doit rester vide.

    Renseigner une cible identique à l'acteur donnerait à croire qu'un tiers est intervenu.
    """
    from app.modules.security.auth import ActionAudit, _ecrire_audit

    user = _utilisateur(db, "Titulaire")

    _ecrire_audit(
        db,
        action=ActionAudit.LOGIN_SUCCESS,
        contexte=CONTEXTE_VIDE,
        user_id=user.id,
        details={"roles": ["CAISSIER"]},
    )

    ligne = _derniere_ligne(db, ActionAudit.LOGIN_SUCCESS.value)
    assert ligne["user_id"] == user.id
    assert ligne["resource_id"] is None
    assert ligne["new_values"] == {"roles": ["CAISSIER"]}
