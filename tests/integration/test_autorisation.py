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
from fastapi import APIRouter, Depends, FastAPI
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
    routes_api,
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
        return {"voit_tout": courant.voit_tout, "agence": str(courant.agency_id)}

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

    assert reponse.json() == {"voit_tout": False, "agence": str(user.primary_agency_id)}


def test_avec_la_permission_reseau_voit_tout(
    client: TestClient, utilisateur: Callable[[str], User]
) -> None:
    """AUDITEUR_INTERNE détient perimetre.reseau : aucun filtre d'agence ne s'appliquera."""
    user = utilisateur("AUDITEUR_INTERNE")

    reponse = client.get("/perimetre", headers=_entete(user, "AUDITEUR_INTERNE"))

    assert reponse.json()["voit_tout"] is True


# --- condition_perimetre : la condition SQL de cloisonnement -------------------------


def _courant(voit_tout: bool, agency_id: uuid.UUID | None) -> UtilisateurCourant:
    """Utilisateur courant fabriqué directement : c'est la PORTÉE qu'on teste, pas le jeton."""
    return UtilisateurCourant(
        user_id=uuid.uuid4(),
        roles=(),
        permissions=frozenset(),
        primary_agency_id=agency_id,
        agency_id=agency_id,
        voit_tout=voit_tout,
    )


@pytest.fixture
def deux_agences(db: Session, utilisateur: Callable[[str], User]) -> tuple[User, User]:
    """Deux utilisateurs dans deux agences distinctes, pour observer ce qu'un filtre laisse."""
    return utilisateur("CAISSIER"), utilisateur("CAISSIER")


def _visibles(
    db: Session, courant: UtilisateurCourant, connus: tuple[User, User]
) -> set[uuid.UUID]:
    """Ceux des deux utilisateurs connus que la condition laisse passer.

    Restreint aux deux lignes créées par le test : la base porte d'autres utilisateurs, et
    un test de cloisonnement doit être déterministe.
    """
    lignes = db.execute(
        select(User.id).where(
            User.id.in_([u.id for u in connus]),
            courant.condition_perimetre(User.primary_agency_id),
        )
    ).scalars()
    return set(lignes)


def test_la_portee_reseau_ne_filtre_rien(db: Session, deux_agences: tuple[User, User]) -> None:
    un, deux = deux_agences

    visibles = _visibles(db, _courant(voit_tout=True, agency_id=un.primary_agency_id), deux_agences)

    assert visibles == {un.id, deux.id}


def test_le_cloisonnement_ne_laisse_que_son_agence(
    db: Session, deux_agences: tuple[User, User]
) -> None:
    un, _ = deux_agences

    visibles = _visibles(
        db, _courant(voit_tout=False, agency_id=un.primary_agency_id), deux_agences
    )

    assert visibles == {un.id}


def test_sans_reseau_ni_agence_on_ne_voit_rien(
    db: Session, deux_agences: tuple[User, User]
) -> None:
    """RÉGRESSION DE SÉCURITÉ — le cas qui rendait omniscient.

    primary_agency_id est nullable : un compte peut n'être rattaché à aucune agence. Tant que
    la portée était rendue sous forme d'« agence à filtrer », ce compte donnait None, valeur
    que l'appelant lisait comme « voit tout le réseau » — une élévation de privilège obtenue
    sans aucune permission, invisible dans la matrice.

    Le cas indécidable doit être un REFUS. Si ce test passe au vert en voyant des lignes,
    c'est que le fail-secure a été perdu.
    """
    visibles = _visibles(db, _courant(voit_tout=False, agency_id=None), deux_agences)

    assert visibles == set()


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


def test_le_meta_test_voit_les_routes_montees_par_routeur() -> None:
    """LE test qui empêche le garde-fou de redevenir aveugle.

    app.routes ne contient pas les routes des routeurs inclus à plat : FastAPI y dépose un
    _IncludedRouter qui les garde dans .original_router.routes. Un parcours naïf ne voyait
    donc que les routes déclarées par @app.get — presque aucune, puisque tout module passe
    par un APIRouter. Le garde-fou restait vert en n'inspectant RIEN (défaut trouvé au bloc
    4b, alors qu'il était vert depuis deux commits).

    Ce test échoue si la descente cesse de fonctionner — par exemple après une montée de
    version de FastAPI qui réorganiserait ses internes.
    """
    chemins = {route.path for route in routes_api(application_reelle)}

    assert "/auth/login" in chemins, "les routes du routeur auth ne sont pas parcourues"
    assert "/users" in chemins, "les routes du routeur users ne sont pas parcourues"
    assert "/health" in chemins


def test_le_meta_test_detecte_une_route_nue_montee_par_routeur() -> None:
    """Le garde-fou ne sert à rien s'il ne détecte pas. On lui montre une route nue.

    Elle est montée PAR UN ROUTEUR, et non par @app.get : c'est ainsi que tous les modules
    à venir déclareront les leurs. Le premier jet de ce test utilisait @app.get et passait
    au vert alors que la détection ne descendait pas dans les routeurs — un test négatif qui
    ne reproduit pas les conditions réelles donne une fausse assurance.

    Sans lui, ROUTES_PUBLIQUES pourrait avaler la liste entière (ou route_protegee répondre
    toujours vrai) sans que rien ne le signale.
    """
    nue = FastAPI()
    routeur = APIRouter(prefix="/module")

    @routeur.get("/oubliee")
    def oubliee() -> dict[str, bool]:
        return {"ok": True}

    @routeur.get("/protegee", dependencies=[Depends(exige("users.read"))])
    def protegee() -> dict[str, bool]:
        return {"ok": True}

    nue.include_router(routeur)

    assert routes_sans_permission(nue, frozenset()) == ["GET /module/oubliee"]


def test_la_permission_de_portee_existe_bien_en_base(db: Session) -> None:
    """Le code de PERMISSION_RESEAU doit correspondre à une ligne réelle du seed.

    Sinon voit_tout serait faux pour tout le monde, silencieusement, et le cloisonnement
    s'appliquerait même à la Direction générale.
    """
    trouvee = db.execute(
        select(Permission).where(Permission.code == PERMISSION_RESEAU)
    ).scalar_one_or_none()

    assert trouvee is not None
