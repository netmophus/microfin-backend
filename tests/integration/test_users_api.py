"""Consultation de l'annuaire (bloc 4b) — tests d'intégration via TestClient.

C'est ici que la brique d'autorisation (4a) est éprouvée sur de VRAIES routes. Trois tests
valident la conception plus qu'ils ne valident du code :

  - test_fiche_hors_agence_donne_404_et_pas_403   -> le cloisonnement ne fuit pas ;
  - test_sans_agence_ni_reseau_la_liste_est_vide  -> le fail-secure de condition_perimetre ;
  - test_la_recherche_ignore_accents_et_casse     -> unaccent (migration 0005).

DÉCOR. Deux agences neuves par test, donc des effectifs connus : les assertions de comptage
portent sur des agences créées à l'instant, jamais sur l'état global de la base. Un test de
cloisonnement qui dépendrait de lignes résiduelles ne prouverait rien.

Isolation SAVEPOINT et override de get_db, comme test_auth_api.
"""

import uuid
from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.parameters.models import Agency
from app.modules.security.jwt import creer_access_token
from app.modules.security.models import Role, User, UserAgency, UserRole

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


def _agence(db: Session, nom: str) -> Agency:
    agence = Agency(code=f"AG-{uuid.uuid4().hex[:6]}", name=nom)
    db.add(agence)
    db.flush()
    return agence


def _utilisateur(
    db: Session,
    nom: str,
    prenom: str,
    role_code: str,
    agence: Agency,
    habilitee: Agency | None = None,
) -> User:
    """Crée un utilisateur. password_hash est un bouchon : ces tests n'authentifient pas."""
    role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
    suffixe = uuid.uuid4().hex[:8]
    user = User(
        matricule=f"MAT-{suffixe}",
        email=f"{suffixe}@example.com",
        # Sans underscore : le test des jokers cherche « _ » littéral, et un identifiant qui
        # en contiendrait le ferait passer pour une fuite d'échappement.
        username=f"u{suffixe}",
        password_hash="x" * 32,
        last_name=nom,
        first_name=prenom,
        primary_agency_id=agence.id,
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    if habilitee is not None:
        db.add(UserAgency(user_id=user.id, agency_id=habilitee.id))
    db.flush()
    return user


def _entete(user: User, role_code: str, agence: Agency | None = None) -> dict[str, str]:
    """Jeton de `user`. `agence` force l'agence COURANTE (C6), sinon le rattachement."""
    jeton = creer_access_token(
        user_id=user.id,
        roles=[role_code],
        primary_agency_id=user.primary_agency_id,
        agency_id=agence.id if agence else user.primary_agency_id,
    )
    return {"Authorization": f"Bearer {jeton}"}


@dataclass
class Decor:
    """Deux agences neuves et leurs occupants — effectifs parfaitement connus.

    Agence A : Diallo (responsable), Kané, Camara, plus Sow qui y est HABILITÉ.
    Agence B : Traoré, Bah (auditeur), Sow (rattaché).
    """

    agence_a: Agency
    agence_b: Agency
    responsable_a: User
    kane: User
    camara: User
    traore: User
    auditeur_b: User
    sow: User

    @property
    def effectif_a(self) -> set[str]:
        """Ce qu'un responsable de l'agence A doit voir — Sow compris (habilité)."""
        return {"Diallo", "Kané", "Camara", "Sow"}


@pytest.fixture
def decor(db: Session) -> Decor:
    agence_a = _agence(db, "Agence A")
    agence_b = _agence(db, "Agence B")
    return Decor(
        agence_a=agence_a,
        agence_b=agence_b,
        responsable_a=_utilisateur(db, "Diallo", "Amadou", "RESPONSABLE_AGENCE", agence_a),
        kane=_utilisateur(db, "Kané", "Fatou", "CAISSIER", agence_a),
        camara=_utilisateur(db, "Camara", "Sekou", "CAISSIER", agence_a),
        traore=_utilisateur(db, "Traoré", "Ibrahim", "CAISSIER", agence_b),
        auditeur_b=_utilisateur(db, "Bah", "Aissatou", "AUDITEUR_INTERNE", agence_b),
        # Rattaché à B, habilité à A : le cas qui a tranché « rattachement OU habilitation ».
        sow=_utilisateur(db, "Sow", "Moussa", "CAISSIER", agence_b, habilitee=agence_a),
    )


@pytest.fixture
def resp(decor: Decor) -> dict[str, str]:
    return _entete(decor.responsable_a, "RESPONSABLE_AGENCE")


@pytest.fixture
def audit(decor: Decor) -> dict[str, str]:
    return _entete(decor.auditeur_b, "AUDITEUR_INTERNE")


def _noms(reponse: object) -> set[str]:
    corps = reponse.json()  # type: ignore[attr-defined]
    return {ligne["last_name"] for ligne in corps["lignes"]}


# --- authentification et permission --------------------------------------------------


def test_liste_sans_jeton_401(client: TestClient) -> None:
    assert client.get("/users").status_code == 401


def test_fiche_sans_jeton_401(client: TestClient, decor: Decor) -> None:
    assert client.get(f"/users/{decor.kane.id}").status_code == 401


def test_liste_sans_users_read_403(client: TestClient, decor: Decor) -> None:
    """CAISSIER est authentifié mais n'a aucune permission Sécurité : 403, pas 401."""
    entete = _entete(decor.camara, "CAISSIER")

    assert client.get("/users", headers=entete).status_code == 403


def test_fiche_sans_users_read_403(client: TestClient, decor: Decor) -> None:
    entete = _entete(decor.camara, "CAISSIER")

    assert client.get(f"/users/{decor.kane.id}", headers=entete).status_code == 403


# --- cloisonnement par agence --------------------------------------------------------


def test_le_responsable_ne_voit_que_son_agence(
    client: TestClient, decor: Decor, resp: dict[str, str]
) -> None:
    reponse = client.get("/users", headers=resp, params={"taille": 100})

    assert reponse.status_code == 200
    assert _noms(reponse) == decor.effectif_a
    assert "Traoré" not in _noms(reponse)


def test_un_agent_habilite_est_visible_du_responsable_de_l_agence_ou_il_travaille(
    client: TestClient, decor: Decor, resp: dict[str, str]
) -> None:
    """Sow est RATTACHÉ à B et HABILITÉ à A. Le responsable de A doit le voir.

    C'est le cas qui a fait retenir « rattachement OU habilitation » plutôt que le seul
    rattachement : sans lui, un responsable ne pourrait pas déverrouiller (4d) le compte
    d'un agent qu'il a devant lui, au guichet de son agence.
    """
    reponse = client.get("/users", headers=resp, params={"taille": 100})

    assert "Sow" in _noms(reponse)
    assert client.get(f"/users/{decor.sow.id}", headers=resp).status_code == 200


def test_le_total_respecte_le_perimetre(
    client: TestClient, decor: Decor, resp: dict[str, str], audit: dict[str, str]
) -> None:
    """Le compteur est une fuite potentielle, pas un confort d'affichage.

    Un total calculé sans le filtre d'agence annoncerait au responsable de A l'effectif du
    réseau entier — en ne lui montrant aucune ligne, donc sans que rien ne paraisse anormal.
    """
    total_resp = client.get("/users", headers=resp, params={"taille": 100}).json()["total"]
    total_audit = client.get("/users", headers=audit, params={"taille": 100}).json()["total"]

    assert total_resp == len(decor.effectif_a)
    assert total_audit > total_resp


def test_la_portee_reseau_voit_les_deux_agences(
    client: TestClient, decor: Decor, audit: dict[str, str]
) -> None:
    """AUDITEUR_INTERNE détient perimetre.reseau : aucun filtre d'agence."""
    reponse = client.get("/users", headers=audit, params={"taille": 100})

    assert decor.effectif_a | {"Traoré", "Bah"} <= _noms(reponse)


def test_fiche_hors_agence_donne_404_et_pas_403(
    client: TestClient, decor: Decor, resp: dict[str, str]
) -> None:
    """LE test qui valide la conception de 4a.

    Traoré existe, mais pas dans le périmètre du responsable de A. La réponse doit être
    404 — indiscernable de « ce compte n'existe pas ». Un 403 signifierait « il existe, mais
    pas pour toi » : le responsable de A pourrait alors cartographier l'agence B en sondant
    des identifiants, et en déduire son effectif sans jamais en voir une ligne.

    Si ce test devient 403, c'est que le filtre a quitté la requête pour devenir un contrôle
    après lecture.
    """
    reponse = client.get(f"/users/{decor.traore.id}", headers=resp)

    assert reponse.status_code == 404


def test_fiche_inexistante_donne_le_meme_404(
    client: TestClient, decor: Decor, resp: dict[str, str]
) -> None:
    """Le 404 « hors périmètre » et le 404 « n'existe pas » doivent être indistinguables."""
    hors_perimetre = client.get(f"/users/{decor.traore.id}", headers=resp)
    inexistant = client.get(f"/users/{uuid.uuid4()}", headers=resp)

    assert hors_perimetre.status_code == inexistant.status_code == 404
    assert hors_perimetre.json() == inexistant.json()


def test_sans_agence_ni_reseau_la_liste_est_vide(
    client: TestClient, db: Session, decor: Decor
) -> None:
    """RÉGRESSION DE SÉCURITÉ (correctif 4a), vue depuis la vraie route HTTP.

    Un compte sans agence de rattachement et sans perimetre.reseau ne doit voir AUCUNE
    ligne. Avant le correctif, sa portée valait None — la même valeur que « voit tout le
    réseau » — et il devenait omniscient sans qu'aucune permission ne le montre.
    """
    role = db.execute(select(Role).where(Role.code == "RESPONSABLE_AGENCE")).scalar_one()
    suffixe = uuid.uuid4().hex[:8]
    sans_agence = User(
        matricule=f"MAT-{suffixe}",
        email=f"{suffixe}@example.com",
        username=f"u_{suffixe}",
        password_hash="x" * 32,
        last_name="Sans",
        first_name="Agence",
        primary_agency_id=None,
    )
    db.add(sans_agence)
    db.flush()
    db.add(UserRole(user_id=sans_agence.id, role_id=role.id))
    db.flush()
    entete = _entete(sans_agence, "RESPONSABLE_AGENCE")

    reponse = client.get("/users", headers=entete, params={"taille": 100})

    assert reponse.status_code == 200
    assert reponse.json()["lignes"] == []
    assert reponse.json()["total"] == 0
    # Et il ne peut pas davantage atteindre une fiche par son identifiant.
    assert client.get(f"/users/{decor.kane.id}", headers=entete).status_code == 404


# --- recherche ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("recherche", "attendu"),
    [
        ("KANE", "Kané"),  # sans accent, majuscules -> trouve l'accentué
        ("kane", "Kané"),
        ("Kané", "Kané"),  # accentué -> se trouve lui-même
        ("traore", "Traoré"),
        ("TRAORÉ", "Traoré"),
        ("Traoré", "Traoré"),
    ],
)
def test_la_recherche_ignore_accents_et_casse(
    client: TestClient, decor: Decor, audit: dict[str, str], recherche: str, attendu: str
) -> None:
    """« KANE » doit trouver « Kané », « traore » doit trouver « Traoré ».

    Enjeu réel en Afrique de l'Ouest : le même patronyme est saisi avec ou sans accents
    selon l'opérateur et le clavier. Une recherche stricte ne retrouve pas des gens qui
    EXISTENT, et un agent qui ne trouve personne cesse d'utiliser l'outil.
    """
    reponse = client.get("/users", headers=audit, params={"q": recherche, "taille": 100})

    assert attendu in _noms(reponse)


def test_la_recherche_porte_aussi_sur_le_matricule_et_l_email(
    client: TestClient, decor: Decor, audit: dict[str, str]
) -> None:
    par_matricule = client.get("/users", headers=audit, params={"q": decor.kane.matricule})
    par_email = client.get("/users", headers=audit, params={"q": decor.kane.email.upper()})

    assert _noms(par_matricule) == {"Kané"}
    assert _noms(par_email) == {"Kané"}


def test_la_recherche_reste_dans_le_perimetre(
    client: TestClient, decor: Decor, resp: dict[str, str]
) -> None:
    """La recherche ne doit pas être une porte dérobée vers les autres agences."""
    reponse = client.get("/users", headers=resp, params={"q": "traore"})

    assert reponse.json()["lignes"] == []
    assert reponse.json()["total"] == 0


def test_les_jokers_sql_sont_neutralises(
    client: TestClient, decor: Decor, audit: dict[str, str]
) -> None:
    """Un joker saisi doit être cherché comme un CARACTÈRE, pas interprété.

    Les valeurs restent des paramètres liés — ce n'est pas une injection — mais un joker non
    échappé fait mentir la recherche sur son propre résultat.

    « Kan_ » est le cas probant : non échappé, « _ » vaut « n'importe quel caractère » et
    trouverait « Kané » ; échappé, il ne trouve rien, puisque personne ne s'appelle
    littéralement « Kan_ ». Le simple « % » ne prouverait que la moitié de la chose.
    """
    for joker in ("%", "_", "%%", "Kan_", "%ané"):
        reponse = client.get("/users", headers=audit, params={"q": joker, "taille": 100})
        assert reponse.json()["total"] == 0, joker

    # Contrôle en miroir : sans le joker, la même racine trouve bien la personne.
    assert _noms(client.get("/users", headers=audit, params={"q": "Kan"})) == {"Kané"}


# --- filtres, pagination --------------------------------------------------------------


def test_le_filtre_activation(
    client: TestClient, db: Session, decor: Decor, resp: dict[str, str]
) -> None:
    decor.camara.is_active = False
    db.flush()

    actifs = client.get("/users", headers=resp, params={"is_active": True, "taille": 100})
    inactifs = client.get("/users", headers=resp, params={"is_active": False, "taille": 100})

    assert "Camara" not in _noms(actifs)
    assert _noms(inactifs) == {"Camara"}


def test_le_filtre_par_role(client: TestClient, decor: Decor, audit: dict[str, str]) -> None:
    reponse = client.get(
        "/users", headers=audit, params={"role": "AUDITEUR_INTERNE", "taille": 100}
    )

    assert "Bah" in _noms(reponse)
    assert "Kané" not in _noms(reponse)


def test_la_pagination_decoupe_sans_recouvrement(
    client: TestClient, decor: Decor, resp: dict[str, str]
) -> None:
    """Pages disjointes, total stable : le tri doit être déterministe (nom, prénom, id)."""
    page1 = client.get("/users", headers=resp, params={"taille": 2, "page": 1}).json()
    page2 = client.get("/users", headers=resp, params={"taille": 2, "page": 2}).json()

    assert len(page1["lignes"]) == 2
    assert page1["total"] == page2["total"] == len(decor.effectif_a)
    ids_1 = {ligne["id"] for ligne in page1["lignes"]}
    ids_2 = {ligne["id"] for ligne in page2["lignes"]}
    assert ids_1 & ids_2 == set()
    assert len(ids_1 | ids_2) == len(decor.effectif_a)


def test_la_taille_de_page_est_plafonnee(
    client: TestClient, decor: Decor, resp: dict[str, str]
) -> None:
    """Sans plafond, ?taille=100000 transformerait l'annuaire en export."""
    reponse = client.get("/users", headers=resp, params={"taille": 100000})

    assert reponse.status_code == 422


# --- soft-delete ----------------------------------------------------------------------


def test_un_utilisateur_supprime_disparait(
    client: TestClient, db: Session, decor: Decor, resp: dict[str, str]
) -> None:
    decor.kane.deleted_at = datetime.now(UTC)
    db.flush()

    liste = client.get("/users", headers=resp, params={"taille": 100})

    assert "Kané" not in _noms(liste)
    assert liste.json()["total"] == len(decor.effectif_a) - 1
    # Et sa fiche n'est pas davantage atteignable en direct.
    assert client.get(f"/users/{decor.kane.id}", headers=resp).status_code == 404


# --- rien de sensible en sortie -------------------------------------------------------

# Champs de security.users qui ne doivent JAMAIS franchir l'API.
CHAMPS_INTERDITS = (
    "password_hash",
    "failed_attempts",
    "lockout_count",
    "last_login_ip",
    "failed_2fa_attempts",
    "secret_encrypted",
    "refresh_token_hash",
)


def test_aucun_champ_sensible_ne_sort(
    client: TestClient, decor: Decor, resp: dict[str, str]
) -> None:
    """Sur la liste comme sur la fiche, corps ET en-têtes.

    Le test porte sur le TEXTE brut de la réponse, pas sur les clés du JSON : une fuite
    imbriquée dans un sous-objet doit être attrapée aussi.
    """
    liste = client.get("/users", headers=resp, params={"taille": 100})
    fiche = client.get(f"/users/{decor.kane.id}", headers=resp)

    for reponse in (liste, fiche):
        texte = reponse.text.lower() + str(reponse.headers).lower()
        for champ in CHAMPS_INTERDITS:
            assert champ not in texte, champ
        assert "argon2" not in texte


def test_la_fiche_expose_exactement_les_champs_prevus(
    client: TestClient, decor: Decor, resp: dict[str, str]
) -> None:
    """Liste FIGÉE : ajouter un champ à la sortie doit être un acte délibéré, pas un effet
    de bord d'une colonne ajoutée à la table users."""
    reponse = client.get(f"/users/{decor.kane.id}", headers=resp)

    assert set(reponse.json()) == {
        "id",
        "matricule",
        "username",
        "email",
        "phone",
        "last_name",
        "first_name",
        "agence_principale",
        "agences_habilitees",
        "roles",
        "is_active",
        "is_locked",
        "locked_until",
        "must_change_password",
        "created_at",
        "updated_at",
    }


def test_la_fiche_montre_roles_et_habilitations(
    client: TestClient, decor: Decor, audit: dict[str, str]
) -> None:
    reponse = client.get(f"/users/{decor.sow.id}", headers=audit)

    corps = reponse.json()
    assert [role["code"] for role in corps["roles"]] == ["CAISSIER"]
    assert corps["agence_principale"]["id"] == str(decor.agence_b.id)
    assert [a["id"] for a in corps["agences_habilitees"]] == [str(decor.agence_a.id)]
