"""Le garde-fou partagé des rattachements (Bloc 5) : `compte_saisie_actif` et le sélecteur qui
l'alimente. Réutilisés par épargne (produits), agences (caisse) et parts sociales.

Double garde-fou à prouver : le SÉLECTEUR ne montre que des comptes de saisie actifs (défense
à l'affichage), et `compte_saisie_actif` refuse ENCORE si un numéro est soumis directement,
sans passer par le sélecteur (défense à l'écriture — la vraie protection).
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.comptabilite import comptes
from app.modules.comptabilite.comptes import CompteInvalideRattachementError
from app.modules.comptabilite.models import Account
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
    from sqlalchemy import text

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


# --- compte_saisie_actif : la fonction elle-même ------------------------------------------


def test_compte_saisie_actif_accepte_un_compte_de_saisie_actif(db: Session) -> None:
    _compte(db, "6T900")
    resultat = comptes.compte_saisie_actif(db, "6T900")
    assert resultat.account_number == "6T900"


def test_compte_saisie_actif_refuse_un_compte_de_regroupement(db: Session) -> None:
    _compte(db, "6T901", is_posting=False)
    with pytest.raises(CompteInvalideRattachementError, match="regroupement"):
        comptes.compte_saisie_actif(db, "6T901")


def test_compte_saisie_actif_refuse_un_compte_desactive(db: Session) -> None:
    _compte(db, "6T902", is_active=False)
    with pytest.raises(CompteInvalideRattachementError):
        comptes.compte_saisie_actif(db, "6T902")


def test_compte_saisie_actif_refuse_un_numero_inexistant(db: Session) -> None:
    with pytest.raises(CompteInvalideRattachementError, match="n'existe pas"):
        comptes.compte_saisie_actif(db, "6T999NOPE")


# --- Sélecteur : défense à l'AFFICHAGE ------------------------------------------------------


def test_selecteur_exclut_regroupement_et_inactifs(client: TestClient, db: Session) -> None:
    _compte(db, "6T910")  # saisie, actif -> proposé
    _compte(db, "6T911", is_posting=False)  # regroupement -> exclu
    _compte(db, "6T912", is_active=False)  # désactivé -> exclu
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.get(
        "/comptabilite/comptes/selecteur", params={"q": "6T91"}, headers=comptable
    )
    numeros = {c["account_number"] for c in reponse.json()}

    assert numeros == {"6T910"}


def test_selecteur_sans_permission_403(client: TestClient, db: Session) -> None:
    caissier = _entete_auth(db, "CAISSIER")
    reponse = client.get("/comptabilite/comptes/selecteur", headers=caissier)
    assert reponse.status_code == 403
