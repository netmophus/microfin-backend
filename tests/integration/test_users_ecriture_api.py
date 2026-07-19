"""Écritures sur les utilisateurs (bloc 4c) — tests d'intégration via TestClient.

Ce que ces tests protègent, par ordre d'importance :

  1. L'AUDIT DIT VRAI : acteur en user_id, cible en resource_id, et pas un secret dedans.
     Un journal immuable de cinq ans qui se tromperait d'auteur serait un faux définitif.
  2. LE PÉRIMÈTRE VAUT AUSSI EN ÉCRITURE : on ne modifie pas qui l'on ne peut pas voir, et
     le refus est un 404 — jamais un 403 qui confirmerait l'existence du compte.
  3. LE POUVOIR NE S'EXERCE PAS SUR SOI : ni se désactiver, ni se supprimer, ni lever son
     propre verrou, ni réinitialiser son propre mot de passe.
  4. FERMER UN COMPTE LE FERME VRAIMENT : les sessions sont révoquées, vérifié EN BASE.

UNE PARTICULARITÉ DU DÉCOR. Aucun rôle du seed ne détient users.update SANS la portée
réseau — ADMIN_FONCTIONNEL a les deux, RESPONSABLE_AGENCE n'a ni l'un ni l'autre. Or c'est
exactement la combinaison qui teste le cloisonnement en écriture. Les tests concernés
accordent donc la permission à RESPONSABLE_AGENCE dans la transaction du test : les
permissions étant résolues en base à chaque requête (4a), l'ajout prend effet aussitôt et
disparaît au rollback.
"""

import uuid
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.parameters.models import Agency
from app.modules.security.jwt import creer_access_token
from app.modules.security.models import (
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
    UserSession,
)
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


def _agence(db: Session, nom: str) -> Agency:
    agence = Agency(code=f"AG-{uuid.uuid4().hex[:6]}", name=nom)
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


@pytest.fixture
def accorder(db: Session) -> Callable[[str, str], None]:
    """Accorde une permission à un rôle, le temps du test (annulé au rollback)."""

    def _accorder(role_code: str, permission_code: str) -> None:
        role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
        permission = db.execute(
            select(Permission).where(Permission.code == permission_code)
        ).scalar_one()
        db.add(RolePermission(role_id=role.id, permission_id=permission.id))
        db.flush()

    return _accorder


@pytest.fixture
def agence_a(db: Session) -> Agency:
    return _agence(db, "Agence A")


@pytest.fixture
def agence_b(db: Session) -> Agency:
    return _agence(db, "Agence B")


@pytest.fixture
def admin(db: Session, agence_a: Agency) -> User:
    """ADMIN_FONCTIONNEL : users.create/update/delete/unlock/reset_password + portée réseau."""
    return _utilisateur(db, "Bah", "ADMIN_FONCTIONNEL", agence_a)


@pytest.fixture
def h_admin(admin: User) -> dict[str, str]:
    return _entete(admin, "ADMIN_FONCTIONNEL")


@pytest.fixture
def responsable(db: Session, agence_a: Agency) -> User:
    return _utilisateur(db, "Diallo", "RESPONSABLE_AGENCE", agence_a)


@pytest.fixture
def h_resp(responsable: User) -> dict[str, str]:
    return _entete(responsable, "RESPONSABLE_AGENCE")


@pytest.fixture
def cible_b(db: Session, agence_b: Agency) -> User:
    """Un compte de l'agence B — hors du périmètre d'un responsable de A."""
    return _utilisateur(db, "Traoré", "CAISSIER", agence_b)


def _corps(agence: Agency | None) -> dict[str, object]:
    suffixe = uuid.uuid4().hex[:8]
    return {
        "matricule": f"MAT-{suffixe}",
        "email": f"{suffixe}@example.com",
        "username": f"u{suffixe}",
        "last_name": "Nouveau",
        "first_name": "Compte",
        "primary_agency_id": str(agence.id) if agence else None,
    }


def _audit(db: Session, action: str) -> dict[str, object]:
    return dict(
        db.execute(
            text(
                "SELECT user_id, action, resource_type, resource_id, old_values, new_values "
                "  FROM audit.audit_logs WHERE action = :action "
                " ORDER BY occurred_at DESC LIMIT 1"
            ),
            {"action": action},
        )
        .mappings()
        .one()
    )


def _sessions_actives(db: Session, user_id: uuid.UUID) -> int:
    return int(
        db.execute(
            text(
                "SELECT count(*) FROM security.user_sessions "
                " WHERE user_id = :u AND revoked_at IS NULL"
            ),
            {"u": user_id},
        ).scalar_one()
    )


def _ouvrir_session(db: Session, user: User) -> UserSession:
    session = UserSession(
        id=uuid.uuid4(),
        user_id=user.id,
        refresh_token_hash="h" * 64,
        expires_at=datetime.now(UTC) + timedelta(hours=8),
    )
    db.add(session)
    db.flush()
    return session


# --- création ---------------------------------------------------------------------------


def test_creation_nominale(client: TestClient, h_admin: dict[str, str], agence_a: Agency) -> None:
    reponse = client.post("/users", headers=h_admin, json=_corps(agence_a))

    assert reponse.status_code == 201
    corps = reponse.json()
    assert corps["utilisateur"]["must_change_password"] is True
    assert corps["utilisateur"]["is_active"] is True
    assert len(corps["mot_de_passe_provisoire"]) >= 16


def test_creation_sans_la_permission_403(
    client: TestClient, h_resp: dict[str, str], agence_a: Agency
) -> None:
    """RESPONSABLE_AGENCE ne détient pas users.create."""
    assert client.post("/users", headers=h_resp, json=_corps(agence_a)).status_code == 403


def test_un_responsable_ne_peut_pas_creer_dans_une_autre_agence(
    client: TestClient,
    h_resp: dict[str, str],
    accorder: Callable[[str, str], None],
    agence_b: Agency,
) -> None:
    """LE PIÈGE DE LA CRÉATION.

    Sans ce contrôle, un responsable créerait un compte rattaché ailleurs et en perdrait la
    main à la seconde même : le compte sortirait aussitôt de son périmètre de lecture.
    Compte orphelin, invisible de son créateur, que seule la portée réseau rattraperait.

    422 et non 403 : la requête est recevable, c'est la VALEUR fournie qui ne l'est pas.
    """
    accorder("RESPONSABLE_AGENCE", "users.create")

    reponse = client.post("/users", headers=h_resp, json=_corps(agence_b))

    assert reponse.status_code == 422


def test_un_responsable_ne_peut_pas_creer_un_compte_sans_agence(
    client: TestClient, h_resp: dict[str, str], accorder: Callable[[str, str], None]
) -> None:
    """Un compte sans agence échappe au cloisonnement de TOUT responsable : réservé au réseau."""
    accorder("RESPONSABLE_AGENCE", "users.create")

    assert client.post("/users", headers=h_resp, json=_corps(None)).status_code == 422


def test_la_portee_reseau_permet_de_creer_partout(
    client: TestClient, h_admin: dict[str, str], agence_b: Agency
) -> None:
    assert client.post("/users", headers=h_admin, json=_corps(agence_b)).status_code == 201
    assert client.post("/users", headers=h_admin, json=_corps(None)).status_code == 201


def test_un_identifiant_deja_pris_donne_409(
    client: TestClient, h_admin: dict[str, str], agence_a: Agency
) -> None:
    """409 nommant le champ. La fuite (« cet email existe quelque part ») est assumée : un
    message générique rendrait l'outil inutilisable pour un administrateur légitime."""
    corps = _corps(agence_a)
    client.post("/users", headers=h_admin, json=corps)

    reponse = client.post("/users", headers=h_admin, json=corps)

    assert reponse.status_code == 409


def test_un_identifiant_libere_par_suppression_est_reutilisable(
    client: TestClient, h_admin: dict[str, str], agence_a: Agency
) -> None:
    """Le cycle rendu possible par la 0006, vu depuis l'API."""
    corps = _corps(agence_a)
    cree = client.post("/users", headers=h_admin, json=corps).json()
    client.delete(f"/users/{cree['utilisateur']['id']}", headers=h_admin)

    assert client.post("/users", headers=h_admin, json=corps).status_code == 201


# --- périmètre en écriture ---------------------------------------------------------------


def test_modifier_hors_perimetre_donne_404_et_pas_403(
    client: TestClient,
    h_resp: dict[str, str],
    accorder: Callable[[str, str], None],
    cible_b: User,
) -> None:
    """LE test du cloisonnement en écriture.

    Le responsable DÉTIENT users.update — le 403 est donc écarté — mais la cible est dans
    une autre agence. Il doit obtenir 404, indiscernable de « ce compte n'existe pas ». Un
    403 lui apprendrait qu'il existe et lui permettrait de sonder les autres agences.
    """
    accorder("RESPONSABLE_AGENCE", "users.update")

    reponse = client.patch(f"/users/{cible_b.id}", headers=h_resp, json={"phone": "70000000"})

    assert reponse.status_code == 404


def test_desactiver_hors_perimetre_donne_404(
    client: TestClient,
    h_resp: dict[str, str],
    accorder: Callable[[str, str], None],
    cible_b: User,
) -> None:
    accorder("RESPONSABLE_AGENCE", "users.update")

    assert client.post(f"/users/{cible_b.id}/deactivate", headers=h_resp).status_code == 404


def test_deverrouiller_hors_perimetre_donne_404(
    client: TestClient, h_resp: dict[str, str], cible_b: User
) -> None:
    """RESPONSABLE_AGENCE détient users.unlock nativement : rien à accorder ici."""
    assert client.post(f"/users/{cible_b.id}/unlock", headers=h_resp).status_code == 404


def test_le_404_hors_perimetre_est_indiscernable_de_l_inexistant(
    client: TestClient,
    h_resp: dict[str, str],
    accorder: Callable[[str, str], None],
    cible_b: User,
) -> None:
    accorder("RESPONSABLE_AGENCE", "users.update")

    hors = client.post(f"/users/{cible_b.id}/deactivate", headers=h_resp)
    inexistant = client.post(f"/users/{uuid.uuid4()}/deactivate", headers=h_resp)

    assert hors.status_code == inexistant.status_code == 404
    assert hors.json() == inexistant.json()


# --- suppression réservée à la portée réseau ----------------------------------------------


def test_un_responsable_ne_peut_pas_supprimer(
    client: TestClient,
    h_resp: dict[str, str],
    accorder: Callable[[str, str], None],
    db: Session,
    agence_a: Agency,
) -> None:
    """users.delete ne suffit pas : la suppression exige AUSSI la portée réseau.

    Un responsable désactive — geste d'exploitation courante. Il ne supprime pas : la
    suppression sort le compte de l'annuaire et libère ses identifiants (0006), c'est une
    décision institutionnelle.

    403 et non 404 : la cible est DANS son périmètre, il la voit. Ce qu'on lui refuse est
    le pouvoir, pas la connaissance — il n'y a donc rien à dissimuler.
    """
    accorder("RESPONSABLE_AGENCE", "users.delete")
    cible = _utilisateur(db, "Camara", "CAISSIER", agence_a)

    reponse = client.delete(f"/users/{cible.id}", headers=h_resp)

    assert reponse.status_code == 403


def test_la_portee_reseau_permet_de_supprimer(
    client: TestClient, h_admin: dict[str, str], db: Session, agence_a: Agency
) -> None:
    cible = _utilisateur(db, "Camara", "CAISSIER", agence_a)

    assert client.delete(f"/users/{cible.id}", headers=h_admin).status_code == 204
    # Sortie de l'annuaire : la lecture ne la trouve plus non plus.
    assert client.get(f"/users/{cible.id}", headers=h_admin).status_code == 404


def test_muter_l_agence_exige_la_portee_reseau(
    client: TestClient,
    h_resp: dict[str, str],
    accorder: Callable[[str, str], None],
    db: Session,
    agence_a: Agency,
    agence_b: Agency,
) -> None:
    """Déplacer un compte vers une autre agence le sort du périmètre de son responsable —
    acte de réseau, pas correction de fiche."""
    accorder("RESPONSABLE_AGENCE", "users.update")
    cible = _utilisateur(db, "Camara", "CAISSIER", agence_a)

    reponse = client.patch(
        f"/users/{cible.id}", headers=h_resp, json={"primary_agency_id": str(agence_b.id)}
    )

    assert reponse.status_code == 403


# --- pas de pouvoir sur soi-même ----------------------------------------------------------


def test_personne_ne_peut_se_desactiver(
    client: TestClient, h_admin: dict[str, str], admin: User
) -> None:
    reponse = client.post(f"/users/{admin.id}/deactivate", headers=h_admin)

    assert reponse.status_code == 403


def test_personne_ne_peut_se_supprimer(
    client: TestClient, h_admin: dict[str, str], admin: User
) -> None:
    assert client.delete(f"/users/{admin.id}", headers=h_admin).status_code == 403


def test_personne_ne_peut_lever_son_propre_verrou(
    client: TestClient, h_admin: dict[str, str], admin: User
) -> None:
    """Sinon le verrouillage progressif (C7) serait sans effet sur quiconque détient
    users.unlock : il se déverrouillerait lui-même à chaque fois."""
    assert client.post(f"/users/{admin.id}/unlock", headers=h_admin).status_code == 403


def test_personne_ne_peut_reinitialiser_son_propre_mot_de_passe(
    client: TestClient, h_admin: dict[str, str], admin: User
) -> None:
    """/auth/change-password est la voie propre, et elle exige de prouver l'ancien mot de
    passe. Passer par la réinitialisation permettrait à un jeton volé de contourner cette
    preuve et de s'approprier le compte."""
    assert client.post(f"/users/{admin.id}/reset-password", headers=h_admin).status_code == 403


def test_modifier_son_propre_contact_reste_permis(
    client: TestClient, h_admin: dict[str, str], admin: User
) -> None:
    """Contrôle en miroir : la règle vise les actes qui soustraient à un contrôle, pas
    l'entretien de sa propre fiche."""
    reponse = client.patch(f"/users/{admin.id}", headers=h_admin, json={"phone": "70000000"})

    assert reponse.status_code == 200
    assert reponse.json()["phone"] == "70000000"


# --- révocation des sessions ---------------------------------------------------------------


def test_desactiver_revoque_les_sessions(
    client: TestClient, db: Session, h_admin: dict[str, str], cible_b: User
) -> None:
    """LA RÈGLE QUI REFERME LA FENÊTRE.

    La brique d'autorisation ne relit pas l'état du compte : sans révocation, un compte
    désactivé continuerait de rafraîchir ses jetons jusqu'à 8 h. Désactiver sans révoquer
    ne désactive rien d'utile. Vérifié EN BASE, pas sur la réponse HTTP.
    """
    _ouvrir_session(db, cible_b)
    assert _sessions_actives(db, cible_b.id) == 1

    client.post(f"/users/{cible_b.id}/deactivate", headers=h_admin)

    assert _sessions_actives(db, cible_b.id) == 0


def test_supprimer_revoque_les_sessions(
    client: TestClient, db: Session, h_admin: dict[str, str], cible_b: User
) -> None:
    _ouvrir_session(db, cible_b)

    client.delete(f"/users/{cible_b.id}", headers=h_admin)

    assert _sessions_actives(db, cible_b.id) == 0


def test_reinitialiser_le_mot_de_passe_revoque_les_sessions(
    client: TestClient, db: Session, h_admin: dict[str, str], cible_b: User
) -> None:
    """On réinitialise souvent parce qu'un compte est suspect : laisser vivre les sessions
    laisserait l'intrus en place et viderait le geste de tout effet."""
    _ouvrir_session(db, cible_b)

    client.post(f"/users/{cible_b.id}/reset-password", headers=h_admin)

    assert _sessions_actives(db, cible_b.id) == 0


def test_reactiver_ne_restaure_aucune_session(
    client: TestClient, db: Session, h_admin: dict[str, str], cible_b: User
) -> None:
    _ouvrir_session(db, cible_b)
    client.post(f"/users/{cible_b.id}/deactivate", headers=h_admin)

    client.post(f"/users/{cible_b.id}/activate", headers=h_admin)

    assert _sessions_actives(db, cible_b.id) == 0


# --- déverrouillage -------------------------------------------------------------------------


def test_deverrouiller_leve_le_verrou_sans_effacer_la_progression(
    client: TestClient, db: Session, h_admin: dict[str, str], cible_b: User
) -> None:
    """lockout_count SURVIT : déverrouiller n'absout pas l'historique.

    Sinon un compte pilonné repartirait au palier le plus doux (15 min) à chaque
    intervention d'un administrateur pressé, et la progression 15/30/60/120 de C7 ne
    mordrait jamais. C7 la réinitialise seule après 24 h calmes.
    """
    cible_b.is_locked = True
    cible_b.locked_until = datetime.now(UTC) + timedelta(minutes=30)
    cible_b.failed_attempts = 5
    cible_b.lockout_count = 2
    db.flush()

    reponse = client.post(f"/users/{cible_b.id}/unlock", headers=h_admin)

    assert reponse.status_code == 200
    db.refresh(cible_b)
    assert cible_b.is_locked is False
    assert cible_b.locked_until is None
    assert cible_b.failed_attempts == 0
    assert cible_b.lockout_count == 2


# --- audit -----------------------------------------------------------------------------------


def test_l_audit_de_creation_distingue_acteur_et_cible(
    client: TestClient, db: Session, h_admin: dict[str, str], admin: User, agence_a: Agency
) -> None:
    """LE point le plus important du bloc.

    Si resource_id recopiait l'acteur, le journal affirmerait que le compte s'est créé
    lui-même — un faux dans une table immuable, conservée cinq ans, opposable au régulateur,
    et impossible à corriger après coup.
    """
    reponse = client.post("/users", headers=h_admin, json=_corps(agence_a))
    cree_id = uuid.UUID(reponse.json()["utilisateur"]["id"])

    ligne = _audit(db, "user.created")
    assert ligne["user_id"] == admin.id
    assert ligne["resource_id"] == cree_id
    assert ligne["resource_type"] == "user"


@pytest.mark.parametrize(
    ("chemin", "methode", "action"),
    [
        ("deactivate", "post", "user.deactivated"),
        ("activate", "post", "user.activated"),
        ("unlock", "post", "user.unlocked"),
        ("reset-password", "post", "user.password_reset"),
    ],
)
def test_chaque_ecriture_produit_son_audit_avec_la_bonne_cible(
    client: TestClient,
    db: Session,
    h_admin: dict[str, str],
    admin: User,
    cible_b: User,
    chemin: str,
    methode: str,
    action: str,
) -> None:
    getattr(client, methode)(f"/users/{cible_b.id}/{chemin}", headers=h_admin)

    ligne = _audit(db, action)
    assert ligne["user_id"] == admin.id
    assert ligne["resource_id"] == cible_b.id


def test_l_audit_de_modification_ne_porte_que_les_champs_modifies(
    client: TestClient, db: Session, h_admin: dict[str, str], cible_b: User
) -> None:
    """Un journal qui répète l'état complet à chaque retouche devient illisible : le lecteur
    ne distingue plus le changement du décor."""
    client.patch(f"/users/{cible_b.id}", headers=h_admin, json={"phone": "70111111"})

    ligne = _audit(db, "user.updated")
    assert ligne["old_values"] == {"phone": None}
    assert ligne["new_values"] == {"phone": "70111111"}


def test_l_audit_de_suppression_conserve_l_etat_complet(
    client: TestClient, db: Session, h_admin: dict[str, str], cible_b: User
) -> None:
    """Après suppression, la fiche sort de l'annuaire : le journal devient la seule trace
    lisible de ce qu'était ce compte."""
    matricule = cible_b.matricule

    client.delete(f"/users/{cible_b.id}", headers=h_admin)

    ligne = _audit(db, "user.deleted")
    assert isinstance(ligne["old_values"], dict)
    assert ligne["old_values"]["matricule"] == matricule


def test_le_mot_de_passe_provisoire_ne_fuit_nulle_part(
    client: TestClient, db: Session, h_admin: dict[str, str], agence_a: Agency
) -> None:
    """Il n'existe QUE dans la réponse de création : ni en base, ni dans l'audit.

    Vérifié sur le texte brut du journal, pas sur des clés : une fuite imbriquée dans un
    sous-objet doit être attrapée aussi.
    """
    reponse = client.post("/users", headers=h_admin, json=_corps(agence_a))
    mot_de_passe = reponse.json()["mot_de_passe_provisoire"]
    cree_id = uuid.UUID(reponse.json()["utilisateur"]["id"])

    # Ni en base : seul le hash Argon2 est stocké, et il ne contient pas le clair.
    cree = db.get(User, cree_id)
    assert cree is not None
    assert mot_de_passe not in cree.password_hash
    assert cree.password_hash.startswith("$argon2")

    # Ni dans le journal d'audit, nulle part.
    journal = db.execute(
        text(
            "SELECT coalesce(old_values::text, '') || coalesce(new_values::text, '') "
            "  FROM audit.audit_logs"
        )
    ).scalars()
    for valeurs in journal:
        assert mot_de_passe not in valeurs

    # Ni dans la fiche renvoyée.
    assert "password_hash" not in reponse.text


def test_aucune_reponse_d_ecriture_ne_contient_de_hash(
    client: TestClient, h_admin: dict[str, str], cible_b: User
) -> None:
    reponses = [
        client.patch(f"/users/{cible_b.id}", headers=h_admin, json={"phone": "70000000"}),
        client.post(f"/users/{cible_b.id}/deactivate", headers=h_admin),
        client.post(f"/users/{cible_b.id}/activate", headers=h_admin),
        client.post(f"/users/{cible_b.id}/reset-password", headers=h_admin),
    ]

    for reponse in reponses:
        texte = (reponse.text + str(reponse.headers)).lower()
        for interdit in ("password_hash", "argon2", "failed_attempts", "lockout_count"):
            assert interdit not in texte, (reponse.url, interdit)
