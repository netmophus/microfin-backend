"""Consultation du journal d'audit — GET /audit (lecture seule, audit.read).

Ce que ces tests protègent :

  - LECTURE SEULE et gardée : audit.read exigé, aucune route d'écriture ;
  - le journal est trié du plus RÉCENT au plus ancien, paginé ;
  - les filtres (action, acteur, cible, période) restreignent correctement ;
  - acteur et cible sont résolus en NOMS, et un compte supprimé n'efface pas l'événement.

Le journal étant immuable, on ne peut pas y insérer via l'ORM : les fixtures écrivent par le
service d'audit (ecrire_audit), le seul chemin d'écriture légitime.
"""

import uuid
from collections.abc import Callable, Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
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


def _utilisateur(db: Session, nom: str, role_code: str) -> User:
    role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
    suffixe = uuid.uuid4().hex[:8]
    agence = Agency(code=f"AG-{suffixe}", name="Agence de test")
    db.add(agence)
    db.flush()
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
    jeton = creer_access_token(user_id=user.id, roles=[role_code])
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
def auditeur(db: Session) -> User:
    """AUDITEUR_INTERNE détient audit.read (+ portée réseau)."""
    return _utilisateur(db, "Bah", "AUDITEUR_INTERNE")


@pytest.fixture
def h_audit(auditeur: User) -> dict[str, str]:
    return _entete(auditeur, "AUDITEUR_INTERNE")


def _ecrire(
    db: Session,
    action: str,
    *,
    acteur: User | None = None,
    cible: User | None = None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
    old: dict[str, object] | None = None,
    new: dict[str, object] | None = None,
) -> None:
    ecrire_audit(
        db,
        action=action,
        contexte=contexte,
        acteur_id=acteur.id if acteur else None,
        resource_type="user" if cible else None,
        resource_id=cible.id if cible else None,
        old_values=old,
        new_values=new,
    )
    db.flush()


# --- lecture seule et permission -------------------------------------------------------


def test_liste_exige_audit_read(client: TestClient, db: Session) -> None:
    caissier = _utilisateur(db, "Sans", "CAISSIER")  # pas d'audit.read

    assert client.get("/audit", headers=_entete(caissier, "CAISSIER")).status_code == 403


def test_liste_sans_jeton_401(client: TestClient) -> None:
    assert client.get("/audit").status_code == 401


def test_aucune_route_d_ecriture_sur_audit(client: TestClient, h_audit: dict[str, str]) -> None:
    """Le journal est inviolable : ni POST, ni PATCH, ni DELETE. FastAPI répond 405."""
    for methode in ("post", "patch", "delete", "put"):
        reponse = getattr(client, methode)("/audit", headers=h_audit)
        assert reponse.status_code == 405


# --- contenu et tri --------------------------------------------------------------------


def test_le_journal_resout_acteur_et_cible_en_noms(
    client: TestClient, db: Session, auditeur: User, h_audit: dict[str, str]
) -> None:
    admin = _utilisateur(db, "Diallo", "ADMIN_FONCTIONNEL")
    cible = _utilisateur(db, "Traoré", "CAISSIER")
    _ecrire(db, "user.created", acteur=admin, cible=cible, new={"matricule": cible.matricule})

    lignes = client.get("/audit", headers=h_audit).json()["lignes"]
    ligne = next(item for item in lignes if item["action"] == "user.created")

    assert ligne["acteur_nom"] == "Test Diallo"
    assert ligne["cible_nom"] == "Test Traoré"
    assert ligne["new_values"] == {"matricule": cible.matricule}


def test_le_plus_recent_est_en_haut(
    client: TestClient, db: Session, auditeur: User, h_audit: dict[str, str]
) -> None:
    """Tri par date DÉCROISSANTE.

    now() est FIGÉ au début d'une transaction : trois écritures dans une même transaction de
    test partageraient occurred_at, et l'ordre retomberait sur le tiebreak (id, un UUID
    aléatoire) — pas sur l'insertion. En production ce cas ne se pose pas, chaque événement
    ayant sa propre transaction, donc son propre instant. On reproduit donc ici des instants
    DISTINCTS, par un INSERT direct (le trigger de chaînage pose chain_hash lui-même)."""
    marqueur = uuid.uuid4().hex[:8]
    instants = ["2026-03-01T08:00:00Z", "2026-03-01T09:00:00Z", "2026-03-01T10:00:00Z"]
    for i, instant in enumerate(instants):
        db.execute(
            text(
                "INSERT INTO audit.audit_logs (occurred_at, action, user_id) "
                "VALUES (CAST(:t AS timestamptz), :action, CAST(:acteur AS uuid))"
            ),
            {"t": instant, "action": f"test.tri.{marqueur}.{i}", "acteur": str(auditeur.id)},
        )
    db.flush()

    lignes = client.get(
        "/audit", headers=h_audit, params={"acteur_id": str(auditeur.id), "taille": 100}
    ).json()["lignes"]
    ordre = [item["action"] for item in lignes if item["action"].startswith(f"test.tri.{marqueur}")]
    # Le plus récent (index 2, 10 h) doit précéder le plus ancien (index 0, 8 h).
    assert ordre == [f"test.tri.{marqueur}.2", f"test.tri.{marqueur}.1", f"test.tri.{marqueur}.0"]


def test_l_ip_est_rendue_en_chaine(
    client: TestClient, db: Session, auditeur: User, h_audit: dict[str, str]
) -> None:
    """Dette INET->IPv4Address : l'IP doit ressortir comme chaîne, pas comme objet."""
    _ecrire(
        db,
        "auth.login.success",
        acteur=auditeur,
        contexte=ContexteRequete(ip="203.0.113.7"),
    )

    ligne = client.get("/audit", headers=h_audit, params={"action": "auth.login.success"}).json()[
        "lignes"
    ][0]
    assert ligne["ip_address"] == "203.0.113.7"


# --- filtres ---------------------------------------------------------------------------


def test_filtre_par_action(
    client: TestClient, db: Session, auditeur: User, h_audit: dict[str, str]
) -> None:
    _ecrire(db, "user.created", acteur=auditeur)
    _ecrire(db, "user.deleted", acteur=auditeur)

    lignes = client.get("/audit", headers=h_audit, params={"action": "user.deleted"}).json()[
        "lignes"
    ]
    assert lignes and all(item["action"] == "user.deleted" for item in lignes)


def test_filtre_par_acteur(
    client: TestClient, db: Session, auditeur: User, h_audit: dict[str, str]
) -> None:
    admin = _utilisateur(db, "Diallo", "ADMIN_FONCTIONNEL")
    autre = _utilisateur(db, "Kone", "ADMIN_FONCTIONNEL")
    _ecrire(db, "user.created", acteur=admin)
    _ecrire(db, "user.created", acteur=autre)

    lignes = client.get("/audit", headers=h_audit, params={"acteur_id": str(admin.id)}).json()[
        "lignes"
    ]
    assert lignes and all(item["acteur_id"] == str(admin.id) for item in lignes)


def test_filtre_par_cible(
    client: TestClient, db: Session, auditeur: User, h_audit: dict[str, str]
) -> None:
    admin = _utilisateur(db, "Diallo", "ADMIN_FONCTIONNEL")
    cible = _utilisateur(db, "Traoré", "CAISSIER")
    _ecrire(db, "user.updated", acteur=admin, cible=cible)
    _ecrire(db, "user.created", acteur=admin)  # sans cible

    lignes = client.get("/audit", headers=h_audit, params={"cible_id": str(cible.id)}).json()[
        "lignes"
    ]
    assert lignes and all(item["cible_id"] == str(cible.id) for item in lignes)


def test_filtre_par_periode(
    client: TestClient, db: Session, auditeur: User, h_audit: dict[str, str]
) -> None:
    """Une plage dans un futur lointain ne renvoie rien : le filtre borne bien occurred_at."""
    _ecrire(db, "user.created", acteur=auditeur)

    corps = client.get(
        "/audit",
        headers=h_audit,
        params={"date_debut": "2099-01-01T00:00:00Z", "date_fin": "2099-12-31T00:00:00Z"},
    ).json()
    assert corps["total"] == 0
    assert corps["lignes"] == []


# --- pagination ------------------------------------------------------------------------


def test_pagination_ne_charge_pas_tout(
    client: TestClient, db: Session, auditeur: User, h_audit: dict[str, str]
) -> None:
    for _ in range(5):
        _ecrire(db, "user.created", acteur=auditeur)

    page = client.get("/audit", headers=h_audit, params={"taille": 2, "page": 1}).json()

    assert len(page["lignes"]) == 2
    assert page["total"] >= 5  # le total reflète tout, la page en montre 2


def test_taille_plafonnee(client: TestClient, h_audit: dict[str, str]) -> None:
    assert client.get("/audit", headers=h_audit, params={"taille": 100000}).status_code == 422
