"""Autorisation (bloc 4a) — utilisateur courant, gate de permission, portée, méta-test.

Tests d'INTÉGRATION : la résolution rôles → permissions lit la matrice en base, donc il
faut une base seedée. Même isolation SAVEPOINT que les autres tests d'API (override de
get_db) : ce qu'un test écrit dans la matrice est annulé à la fin.

Les routes protégées testées ici vivent dans une application JETABLE, pas dans app.main :
y ajouter des routes fausserait le méta-test des routes non protégées, qui inspecte
justement la vraie application.
"""

import uuid
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import jwt as pyjwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import engine, get_db
from app.main import app as application_reelle
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import (
    PERMISSION_RESEAU,
    UtilisateurCourant,
    exige,
    routes_sans_permission,
    utilisateur_courant,
)
from app.modules.security.jwt import DUREE_ACCES, creer_access_token, creer_refresh_token
from app.modules.security.models import Permission, Role, RolePermission, User, UserRole

pytestmark = pytest.mark.integration


# --- fixtures ------------------------------------------------------------------------


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
    """Application jetable exposant les trois formes d'usage de la brique.

    - /gate      : gate pur, la route ignore qui est l'appelant
    - /qui       : l'utilisateur courant sans exiger de permission
    - /perimetre : gate + lecture de la portée, le cas des services métier
    """
    jetable = FastAPI()

    @jetable.get("/gate", dependencies=[Depends(exige("users.read"))])
    def gate() -> dict[str, bool]:
        return {"ok": True}

    @jetable.get("/qui")
    def qui(courant: Annotated[UtilisateurCourant, Depends(utilisateur_courant)]) -> dict[str, Any]:
        return {"user_id": str(courant.user_id), "roles": list(courant.roles)}

    @jetable.get("/perimetre")
    def perimetre(
        courant: Annotated[UtilisateurCourant, Depends(exige("users.read"))],
    ) -> dict[str, Any]:
        agence = courant.perimetre_agence()
        return {"voit_tout": courant.voit_tout, "perimetre": str(agence) if agence else None}

    jetable.dependency_overrides[get_db] = lambda: db
    with TestClient(jetable) as testclient:
        yield testclient


@pytest.fixture
def utilisateur(db: Session) -> Callable[[str], User]:
    """Fabrique un utilisateur rattaché à une agence et portant le rôle système demandé.

    Pas de mot de passe utilisable : ces tests n'authentifient pas, ils présentent un
    jeton déjà émis. C'est la brique d'autorisation qui est sous test, pas le login.
    """

    def _utilisateur(role_code: str) -> User:
        role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
        suffixe = uuid.uuid4().hex[:8]
        agence = Agency(code=f"AG-{suffixe}", name="Agence de test")
        db.add(agence)
        db.flush()
        user = User(
            matricule=f"MAT-{suffixe}",
            email=f"u.{suffixe}@example.com",
            username=f"user_{suffixe}",
            password_hash="x" * 32,
            last_name="Test",
            first_name="U",
            primary_agency_id=agence.id,
        )
        db.add(user)
        db.flush()
        db.add(UserRole(user_id=user.id, role_id=role.id))
        db.flush()
        return user

    return _utilisateur


def _entete(user: User, role_code: str) -> dict[str, str]:
    jeton = creer_access_token(
        user_id=user.id, roles=[role_code], primary_agency_id=user.primary_agency_id
    )
    return {"Authorization": f"Bearer {jeton}"}


# --- 401 : ne pas être authentifié ---------------------------------------------------


def test_sans_jeton_401(client: TestClient) -> None:
    reponse = client.get("/gate")

    assert reponse.status_code == 401
    assert reponse.headers["www-authenticate"] == "Bearer"


def test_jeton_illisible_401(client: TestClient) -> None:
    reponse = client.get("/gate", headers={"Authorization": "Bearer pas-un-jeton"})

    assert reponse.status_code == 401


def test_jeton_expire_401(client: TestClient, utilisateur: Callable[[str], User]) -> None:
    """Un access token authentiquement signé mais périmé ne vaut plus rien."""
    user = utilisateur("RESPONSABLE_AGENCE")
    passe = datetime.now(UTC) - timedelta(hours=1)
    perime = pyjwt.encode(
        {
            "sub": str(user.id),
            "jti": str(uuid.uuid4()),
            "iat": int((passe - DUREE_ACCES).timestamp()),
            "exp": int(passe.timestamp()),
            "type": "access",
            "roles": ["RESPONSABLE_AGENCE"],
            "primary_agency_id": str(user.primary_agency_id),
            "agency_id": str(user.primary_agency_id),
        },
        settings.JWT_SECRET.get_secret_value(),
        algorithm=settings.JWT_ALGORITHM,
    )

    reponse = client.get("/gate", headers={"Authorization": f"Bearer {perime}"})

    assert reponse.status_code == 401


def test_refresh_presente_comme_access_401(
    client: TestClient, utilisateur: Callable[[str], User]
) -> None:
    """Confusion de familles : un refresh valide n'ouvre aucune porte applicative."""
    user = utilisateur("RESPONSABLE_AGENCE")

    reponse = client.get(
        "/gate", headers={"Authorization": f"Bearer {creer_refresh_token(user.id)}"}
    )

    assert reponse.status_code == 401


# --- 403 : être authentifié sans avoir le droit --------------------------------------


def test_authentifie_sans_la_permission_403(
    client: TestClient, utilisateur: Callable[[str], User]
) -> None:
    """CAISSIER n'a aucune permission du périmètre Sécurité : 403, pas 401."""
    user = utilisateur("CAISSIER")

    reponse = client.get("/gate", headers=_entete(user, "CAISSIER"))

    assert reponse.status_code == 403


def test_authentifie_avec_la_permission_200(
    client: TestClient, utilisateur: Callable[[str], User]
) -> None:
    user = utilisateur("RESPONSABLE_AGENCE")  # détient users.read

    reponse = client.get("/gate", headers=_entete(user, "RESPONSABLE_AGENCE"))

    assert reponse.status_code == 200


def test_utilisateur_courant_sans_permission_reste_accessible(
    client: TestClient, utilisateur: Callable[[str], User]
) -> None:
    """utilisateur_courant authentifie, il n'autorise pas : un CAISSIER passe."""
    user = utilisateur("CAISSIER")

    reponse = client.get("/qui", headers=_entete(user, "CAISSIER"))

    assert reponse.status_code == 200
    assert reponse.json() == {"user_id": str(user.id), "roles": ["CAISSIER"]}


# --- les permissions viennent de la BASE, pas du jeton -------------------------------


def test_un_correctif_de_matrice_prend_effet_sans_reemettre_le_jeton(
    client: TestClient, db: Session, utilisateur: Callable[[str], User]
) -> None:
    """C'est la raison d'être de la résolution en base.

    Le jeton ne porte que des CODES DE RÔLES. On accorde users.read à CAISSIER en base,
    et le MÊME jeton, déjà émis, passe aussitôt — sans attendre les 15 min d'expiration.
    Corriger une erreur de matrice ne demande donc pas de déconnecter le réseau.
    """
    user = utilisateur("CAISSIER")
    entete = _entete(user, "CAISSIER")
    assert client.get("/gate", headers=entete).status_code == 403

    role = db.execute(select(Role).where(Role.code == "CAISSIER")).scalar_one()
    permission = db.execute(select(Permission).where(Permission.code == "users.read")).scalar_one()
    db.add(RolePermission(role_id=role.id, permission_id=permission.id))
    db.flush()

    assert client.get("/gate", headers=entete).status_code == 200


# --- portée (C6) ---------------------------------------------------------------------


def test_sans_permission_reseau_le_perimetre_est_l_agence(
    client: TestClient, utilisateur: Callable[[str], User]
) -> None:
    """RESPONSABLE_AGENCE est cloisonné : les services devront filtrer sur son agence."""
    user = utilisateur("RESPONSABLE_AGENCE")

    reponse = client.get("/perimetre", headers=_entete(user, "RESPONSABLE_AGENCE"))

    assert reponse.json() == {"voit_tout": False, "perimetre": str(user.primary_agency_id)}


def test_avec_la_permission_reseau_le_perimetre_est_nul(
    client: TestClient, utilisateur: Callable[[str], User]
) -> None:
    """AUDITEUR_INTERNE détient perimetre.reseau : aucun filtre d'agence ne s'applique."""
    user = utilisateur("AUDITEUR_INTERNE")

    reponse = client.get("/perimetre", headers=_entete(user, "AUDITEUR_INTERNE"))

    assert reponse.json() == {"voit_tout": True, "perimetre": None}


# --- méta-test : aucune route ne doit rester non protégée par oubli ------------------

# Allowlist des routes délibérément SANS permission. Y ajouter une ligne doit être un
# acte conscient, discuté en revue : c'est le seul endroit du code où l'on déclare qu'une
# route est ouverte.
#
#   /health            : sonde d'infrastructure, aucune donnée métier.
#   /auth/login        : la porte d'entrée — exiger une permission pour se connecter
#                        serait circulaire.
#   /auth/refresh      : porte son autorisation en lui-même (le refresh token du cookie).
#   /auth/logout(-all) : idem, et se déconnecter ne doit jamais être refusé.
ROUTES_PUBLIQUES = frozenset(
    {"/health", "/auth/login", "/auth/refresh", "/auth/logout", "/auth/logout-all"}
)


def test_aucune_route_n_est_exposee_sans_permission() -> None:
    """Garde-fou anti-oubli, valable pour TOUS les modules à venir.

    Une route ajoutée dans crédit, épargne ou caisse sans Depends(exige(...)) fait rougir
    ce test immédiatement — au lieu d'être découverte en production dans six mois.
    """
    manquantes = routes_sans_permission(application_reelle, ROUTES_PUBLIQUES)

    assert manquantes == [], (
        "Routes exposées sans permission : "
        + ", ".join(manquantes)
        + '. Protégez-les avec Depends(exige("...")), ou justifiez l\'ouverture en '
        "ajoutant la route à ROUTES_PUBLIQUES."
    )


def test_le_meta_test_detecte_bien_une_route_non_protegee() -> None:
    """Le garde-fou ne sert à rien s'il ne détecte pas. On lui montre une route nue.

    Sans ce test, ROUTES_PUBLIQUES pourrait avaler la liste entière (ou route_protegee
    répondre toujours vrai) sans que rien ne le signale.
    """
    nue = FastAPI()

    @nue.get("/oubliee")
    def oubliee() -> dict[str, bool]:
        return {"ok": True}

    @nue.get("/protegee", dependencies=[Depends(exige("users.read"))])
    def protegee() -> dict[str, bool]:
        return {"ok": True}

    assert routes_sans_permission(nue, frozenset()) == ["GET /oubliee"]


def test_la_permission_de_portee_existe_bien_en_base(db: Session) -> None:
    """Le code de PERMISSION_RESEAU doit correspondre à une ligne réelle du seed.

    Sinon voit_tout serait faux pour tout le monde, silencieusement, et le cloisonnement
    s'appliquerait même à la Direction générale.
    """
    trouvee = db.execute(
        select(Permission).where(Permission.code == PERMISSION_RESEAU)
    ).scalar_one_or_none()

    assert trouvee is not None
