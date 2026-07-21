"""must_change_password réellement bloquant (bloc 4c) — et la seule porte pour le lever.

Ce que ces tests garantissent, dans l'ordre d'importance :

  1. un mot de passe provisoire n'ouvre RIEN, même à qui détient la permission ;
  2. /auth/change-password reste accessible malgré le blocage — sinon le compte est
     enfermé dehors, incapable de faire ce qu'on exige de lui ;
  3. le drapeau tombe une fois le mot de passe changé, et les jetons suivants sont libres ;
  4. changer exige de prouver l'ANCIEN mot de passe, même authentifié.
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import CODE_MOT_DE_PASSE_A_RENOUVELER
from app.modules.security.jwt import creer_access_token
from app.modules.security.models import Role, User, UserPasswordHistory, UserRole
from app.modules.security.mots_de_passe import generer_mot_de_passe
from app.modules.security.mots_lisibles import MOTS_LISIBLES
from app.modules.security.password import (
    hasher_mot_de_passe,
    valider_politique,
    verifier_mot_de_passe,
)

pytestmark = pytest.mark.integration

MOT_DE_PASSE = "Ancien!MotDePasse9"
NOUVEAU = "Nouveau!MotDePasse7"


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
def utilisateur(db: Session) -> User:
    """RESPONSABLE_AGENCE : il DÉTIENT users.read, ce qui rend le blocage démonstratif."""
    role = db.execute(select(Role).where(Role.code == "RESPONSABLE_AGENCE")).scalar_one()
    suffixe = uuid.uuid4().hex[:8]
    agence = Agency(code=f"AG-{suffixe}", name="Agence de test")
    db.add(agence)
    db.flush()
    user = User(
        matricule=f"MAT-{suffixe}",
        email=f"{suffixe}@example.com",
        username=f"u{suffixe}",
        password_hash=hasher_mot_de_passe(MOT_DE_PASSE),
        last_name="Kané",
        first_name="Fatou",
        primary_agency_id=agence.id,
        must_change_password=True,
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.flush()
    return user


def _entete(user: User, *, doit_changer: bool) -> dict[str, str]:
    jeton = creer_access_token(
        user_id=user.id,
        roles=["RESPONSABLE_AGENCE"],
        primary_agency_id=user.primary_agency_id,
        must_change_password=doit_changer,
    )
    return {"Authorization": f"Bearer {jeton}"}


# --- 1. le blocage ---------------------------------------------------------------------


def test_un_mot_de_passe_provisoire_bloque_meme_avec_la_permission(
    client: TestClient, utilisateur: User
) -> None:
    """LE test du mécanisme. RESPONSABLE_AGENCE détient users.read, et pourtant : 403.

    Le contrôle est placé AVANT celui de la permission, dans exige() — le point de passage
    obligé de toute route protégée. Il hérite donc de la couverture du méta-test au lieu
    d'être un contrôle de plus à écrire dans chaque module.
    """
    reponse = client.get("/users", headers=_entete(utilisateur, doit_changer=True))

    assert reponse.status_code == 403
    assert reponse.headers["x-erreur-code"] == CODE_MOT_DE_PASSE_A_RENOUVELER


def test_le_meme_jeton_sans_le_drapeau_passe(client: TestClient, utilisateur: User) -> None:
    """Contrôle en miroir : c'est bien le drapeau qui bloque, pas autre chose."""
    reponse = client.get("/users", headers=_entete(utilisateur, doit_changer=False))

    assert reponse.status_code == 200


def test_le_blocage_couvre_aussi_la_fiche(client: TestClient, utilisateur: User) -> None:
    reponse = client.get(
        f"/users/{utilisateur.id}", headers=_entete(utilisateur, doit_changer=True)
    )

    assert reponse.status_code == 403


# --- 2. la porte de sortie reste ouverte -----------------------------------------------


def test_change_password_reste_accessible_malgre_le_blocage(
    client: TestClient, utilisateur: User
) -> None:
    """Sans cette exception, le compte serait mort-né : enfermé dehors par la contrainte
    même qu'on lui demande de lever."""
    reponse = client.post(
        "/auth/change-password",
        headers=_entete(utilisateur, doit_changer=True),
        json={"mot_de_passe_actuel": MOT_DE_PASSE, "nouveau_mot_de_passe": NOUVEAU},
    )

    assert reponse.status_code == 204


def test_change_password_exige_un_jeton(client: TestClient) -> None:
    """« Sans permission » ne veut pas dire « ouverte » : il faut être authentifié."""
    reponse = client.post(
        "/auth/change-password",
        json={"mot_de_passe_actuel": MOT_DE_PASSE, "nouveau_mot_de_passe": NOUVEAU},
    )

    assert reponse.status_code == 401


# --- 3. le drapeau tombe ----------------------------------------------------------------


def test_changer_le_mot_de_passe_leve_le_drapeau_et_libere(
    client: TestClient, db: Session, utilisateur: User
) -> None:
    client.post(
        "/auth/change-password",
        headers=_entete(utilisateur, doit_changer=True),
        json={"mot_de_passe_actuel": MOT_DE_PASSE, "nouveau_mot_de_passe": NOUVEAU},
    )

    db.refresh(utilisateur)
    assert utilisateur.must_change_password is False
    assert verifier_mot_de_passe(NOUVEAU, utilisateur.password_hash)
    # Un jeton émis APRÈS le changement ne porte plus la restriction.
    assert client.get("/users", headers=_entete(utilisateur, doit_changer=False)).status_code == 200


def test_l_ancien_hash_part_dans_l_historique(
    client: TestClient, db: Session, utilisateur: User
) -> None:
    """C12 : l'ANCIEN hash est historisé, pas le nouveau — qui vit déjà dans users."""
    ancien_hash = utilisateur.password_hash

    client.post(
        "/auth/change-password",
        headers=_entete(utilisateur, doit_changer=True),
        json={"mot_de_passe_actuel": MOT_DE_PASSE, "nouveau_mot_de_passe": NOUVEAU},
    )

    historises = list(
        db.execute(
            select(UserPasswordHistory.password_hash).where(
                UserPasswordHistory.user_id == utilisateur.id
            )
        ).scalars()
    )
    assert historises == [ancien_hash]


# --- 4. il faut prouver l'ancien --------------------------------------------------------


def test_un_mauvais_mot_de_passe_actuel_refuse(client: TestClient, utilisateur: User) -> None:
    """Un jeton volé ne doit pas suffire à s'approprier le compte.

    Sans cette preuve, le vol d'une session — temporaire par nature — deviendrait une prise
    de contrôle définitive.
    """
    reponse = client.post(
        "/auth/change-password",
        headers=_entete(utilisateur, doit_changer=True),
        json={"mot_de_passe_actuel": "PasLeBon!123456", "nouveau_mot_de_passe": NOUVEAU},
    )

    assert reponse.status_code == 400


def test_un_mot_de_passe_non_conforme_est_refuse(client: TestClient, utilisateur: User) -> None:
    reponse = client.post(
        "/auth/change-password",
        headers=_entete(utilisateur, doit_changer=True),
        json={"mot_de_passe_actuel": MOT_DE_PASSE, "nouveau_mot_de_passe": "court"},
    )

    assert reponse.status_code == 422
    assert "longueur_minimale" in reponse.json()["detail"]["violations"]


def test_reutiliser_le_mot_de_passe_courant_est_refuse(
    client: TestClient, utilisateur: User
) -> None:
    """C12 — le mot de passe courant compte comme « déjà utilisé »."""
    reponse = client.post(
        "/auth/change-password",
        headers=_entete(utilisateur, doit_changer=True),
        json={"mot_de_passe_actuel": MOT_DE_PASSE, "nouveau_mot_de_passe": MOT_DE_PASSE},
    )

    assert reponse.status_code == 409


def test_reutiliser_un_ancien_mot_de_passe_est_refuse(
    client: TestClient, db: Session, utilisateur: User
) -> None:
    """Deux changements successifs, puis retour au tout premier : refusé."""
    entete = _entete(utilisateur, doit_changer=False)
    client.post(
        "/auth/change-password",
        headers=entete,
        json={"mot_de_passe_actuel": MOT_DE_PASSE, "nouveau_mot_de_passe": NOUVEAU},
    )

    reponse = client.post(
        "/auth/change-password",
        headers=entete,
        json={"mot_de_passe_actuel": NOUVEAU, "nouveau_mot_de_passe": MOT_DE_PASSE},
    )

    assert reponse.status_code == 409


def test_aucun_mot_de_passe_ne_transparait_dans_la_reponse(
    client: TestClient, utilisateur: User
) -> None:
    for corps in (
        {"mot_de_passe_actuel": "PasLeBon!123456", "nouveau_mot_de_passe": NOUVEAU},
        {"mot_de_passe_actuel": MOT_DE_PASSE, "nouveau_mot_de_passe": "court"},
    ):
        reponse = client.post(
            "/auth/change-password", headers=_entete(utilisateur, doit_changer=True), json=corps
        )
        texte = reponse.text + str(reponse.headers)
        assert NOUVEAU not in texte
        assert MOT_DE_PASSE not in texte
        assert "argon2" not in texte.lower()


# --- le générateur ----------------------------------------------------------------------


def test_le_mot_de_passe_genere_est_conforme_et_jamais_deux_fois_le_meme() -> None:
    tirages = {generer_mot_de_passe().clair for _ in range(50)}

    assert len(tirages) == 50
    for clair in tirages:
        # Conforme À LA POLITIQUE — la vraie exigence, quel que soit le format.
        assert valider_politique(clair).est_conforme


def test_le_mot_de_passe_genere_est_lisible_et_dictable() -> None:
    """La forme « Mot-mot-mot-mot-mot-NN » : cinq mots de la liste, séparés par des tirets,
    le premier capitalisé, deux chiffres à la fin. C'est ce qui le rend dictable."""
    clair = generer_mot_de_passe().clair
    corps, chiffres = clair.rsplit("-", 1)
    mots = corps.split("-")

    assert len(mots) == 5
    assert chiffres.isdigit() and len(chiffres) == 2
    # Le premier mot est capitalisé, les autres en minuscules.
    assert mots[0][0].isupper()
    assert all(mot.islower() for mot in mots[1:])
    # CHAQUE mot vient de la liste fermée — rien n'est généré librement. C'est ce qui
    # garantit qu'aucune grossièreté ne peut sortir.
    for mot in mots:
        assert mot.lower() in MOTS_LISIBLES


def test_les_chiffres_evitent_zero_et_un() -> None:
    """0 et 1 se confondent à l'écrit avec O et l/I : on les écarte pour la recopie."""
    for _ in range(50):
        chiffres = generer_mot_de_passe().clair.rsplit("-", 1)[1]
        assert "0" not in chiffres and "1" not in chiffres


def test_le_hash_genere_correspond_au_clair() -> None:
    resultat = generer_mot_de_passe()

    assert verifier_mot_de_passe(resultat.clair, resultat.hash)
    assert resultat.clair not in resultat.hash
