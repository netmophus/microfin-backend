"""Caisse — Bloc B : postes (CRUD), rattachement comptable, assignation des guichetiers.

Trois périmètres distincts, vus MORDRE :
  - caisse.poste.manage (RESPONSABLE_AGENCE) : CRUD + assignation, jamais hors de SON agence.
  - compta.plan.manage (existant) : rattachement comptable SEUL, institution entière.
  - poste non assigné à l'acteur (Bloc C, `ouvrir_session()`) : un poste actif et rattaché de
    SON agence, mais où il n'a jamais été affecté, refuse par `PosteIntrouvableError` — même
    discipline IDOR que le reste (404, jamais 403). L'ancienne ambiguïté « deux postes actifs »
    (PosteAmbiguError) a disparu structurellement avec la sélection explicite : filtrer sur
    `Poste.id == poste_id` (clé primaire) ne peut plus jamais rendre deux lignes.
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.caisse import postes as postes_service
from app.modules.caisse.models import PosteAssignation
from app.modules.caisse.postes import (
    CodeDejaUtiliseError,
    PosteEnUsageError,
    PosteIntrouvableError,
    UtilisateurHorsPerimetreError,
)
from app.modules.caisse.service import ouvrir_session
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.security.jwt import creer_access_token
from app.modules.security.models import Role, User, UserAgency, UserRole
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


def _cid(db: Session, numero: str) -> uuid.UUID:
    return db.execute(
        text("SELECT id FROM comptabilite.accounts WHERE account_number = :n"), {"n": numero}
    ).scalar_one()


def _agence(db: Session, code: str, *, avec_compte: bool = True) -> Agency:
    """Agence rattachée (ou non) — SANS poste : le Bloc B crée les postes, contrairement à
    `_agence()` de test_caisse_sessions.py qui en pose un pour ses propres besoins."""
    agence = Agency(
        code=code,
        name=f"Agence {code}",
        compte_caisse_id=_cid(db, "101111") if avec_compte else None,
    )
    db.add(agence)
    db.flush()
    return agence


def _utilisateur(db: Session, agence: Agency, suffixe: str, role_code: str = "CAISSIER") -> User:
    role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
    user = User(
        matricule=f"MAT-CXP-{suffixe}",
        email=f"cxp{suffixe}@ex.com",
        username=f"cxp{suffixe}",
        password_hash=hasher_mot_de_passe("Motdepasse!123"),
        last_name="Test",
        first_name=suffixe,
        primary_agency_id=agence.id,
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.flush()
    return user


def _responsable(user: User, agence: Agency) -> UtilisateurCourant:
    return UtilisateurCourant(
        user_id=user.id,
        roles=("RESPONSABLE_AGENCE",),
        permissions=frozenset({"caisse.poste.manage"}),
        primary_agency_id=agence.id,
        agency_id=agence.id,
        voit_tout=False,
    )


def _comptable(user: User, agence: Agency) -> UtilisateurCourant:
    return UtilisateurCourant(
        user_id=user.id,
        roles=("COMPTABLE",),
        permissions=frozenset({"compta.plan.manage"}),
        primary_agency_id=agence.id,
        agency_id=agence.id,
        voit_tout=False,
    )


def _caissier(user: User, agence: Agency) -> UtilisateurCourant:
    return UtilisateurCourant(
        user_id=user.id,
        roles=("CAISSIER",),
        permissions=frozenset(
            {"caisse.session.open", "caisse.session.close", "caisse.session.read"}
        ),
        primary_agency_id=agence.id,
        agency_id=agence.id,
        voit_tout=False,
    )


def _entete(user: User, agence: Agency, role_code: str) -> dict[str, str]:
    jeton = creer_access_token(
        user_id=user.id, roles=[role_code], primary_agency_id=agence.id, agency_id=agence.id
    )
    return {"Authorization": f"Bearer {jeton}"}


# --- CRUD : SON agence, jamais au-delà --------------------------------------------------------


def test_creer_poste_pour_son_agence(db: Session) -> None:
    agence = _agence(db, "CXP1")
    responsable = _responsable(_utilisateur(db, agence, "1", "RESPONSABLE_AGENCE"), agence)

    poste = postes_service.creer(
        db, responsable, code="02", libelle="Guichet 2", motif="ouverture d'un second guichet"
    )

    assert poste.agency_id == agence.id  # jamais soumis par le client, dérivé de l'acteur
    assert poste.code == "02"
    assert poste.compte_caisse_id is None  # non rattaché à la création, état légitime


def test_creer_poste_code_deja_utilise_refuse(db: Session) -> None:
    agence = _agence(db, "CXP2")
    responsable = _responsable(_utilisateur(db, agence, "2", "RESPONSABLE_AGENCE"), agence)
    postes_service.creer(db, responsable, code="01", libelle="Principal", motif="init")

    with pytest.raises(CodeDejaUtiliseError):
        postes_service.creer(db, responsable, code="01", libelle="Doublon", motif="erreur")


def test_renommer_poste(db: Session) -> None:
    agence = _agence(db, "CXP3")
    responsable = _responsable(_utilisateur(db, agence, "3", "RESPONSABLE_AGENCE"), agence)
    poste = postes_service.creer(db, responsable, code="01", libelle="Ancien nom", motif="init")

    postes_service.renommer(
        db, responsable, poste, code="01", libelle="Nouveau nom", motif="correction"
    )

    assert poste.libelle == "Nouveau nom"


def test_desactiver_poste_avec_session_ouverte_refuse(db: Session) -> None:
    """Jamais désactiver un objet avec un engagement actif — même règle que partout ailleurs."""
    agence = _agence(db, "CXP4")
    responsable = _responsable(_utilisateur(db, agence, "4", "RESPONSABLE_AGENCE"), agence)
    poste = postes_service.creer(db, responsable, code="01", libelle="Principal", motif="init")
    postes_service.rattacher_compte(
        db, responsable, poste, compte_caisse_number="101111", motif="rattachement initial"
    )
    caissier_user = _utilisateur(db, agence, "4c", "CAISSIER")
    postes_service.assigner(db, responsable, poste, user_id=caissier_user.id)
    caissier = _caissier(caissier_user, agence)
    ouvrir_session(db, caissier, poste_id=poste.id, fonds_initial=10_000)
    db.flush()

    with pytest.raises(PosteEnUsageError):
        postes_service.changer_activation(
            db, responsable, poste, is_active=False, motif="tentative de fermeture"
        )


def test_desactiver_puis_reactiver_poste_sans_session(db: Session) -> None:
    agence = _agence(db, "CXP5")
    responsable = _responsable(_utilisateur(db, agence, "5", "RESPONSABLE_AGENCE"), agence)
    poste = postes_service.creer(db, responsable, code="01", libelle="Principal", motif="init")

    postes_service.changer_activation(
        db, responsable, poste, is_active=False, motif="fermeture définitive du guichet"
    )
    assert poste.is_active is False

    postes_service.changer_activation(db, responsable, poste, is_active=True, motif="réouverture")
    assert poste.is_active is True


def test_charger_poste_gere_hors_agence_refuse(db: Session) -> None:
    """IDOR : un responsable ne gère jamais le poste d'une autre agence — 404, jamais 403."""
    agence_a = _agence(db, "CXP6")
    agence_b = _agence(db, "CXP7")
    responsable_a = _responsable(_utilisateur(db, agence_a, "6", "RESPONSABLE_AGENCE"), agence_a)
    responsable_b = _responsable(_utilisateur(db, agence_b, "7", "RESPONSABLE_AGENCE"), agence_b)
    poste_b = postes_service.creer(
        db, responsable_b, code="01", libelle="Poste B", motif="init"
    )

    with pytest.raises(PosteIntrouvableError):
        postes_service.charger_poste_gere(db, responsable_a, poste_b.id)


def test_lister_postes_responsable_voit_son_agence_seulement(db: Session) -> None:
    agence_a = _agence(db, "CXP8")
    agence_b = _agence(db, "CXP9")
    responsable_a = _responsable(_utilisateur(db, agence_a, "8", "RESPONSABLE_AGENCE"), agence_a)
    responsable_b = _responsable(_utilisateur(db, agence_b, "9", "RESPONSABLE_AGENCE"), agence_b)
    poste_a = postes_service.creer(db, responsable_a, code="01", libelle="A", motif="init")
    postes_service.creer(db, responsable_b, code="01", libelle="B", motif="init")

    vus = postes_service.lister(db, responsable_a)

    assert [p.id for p in vus] == [poste_a.id]


# --- Rattachement comptable : compta.plan.manage, institution entière -------------------------


def test_rattacher_compte_poste(db: Session) -> None:
    agence = _agence(db, "CXQ1", avec_compte=False)
    responsable = _responsable(_utilisateur(db, agence, "10", "RESPONSABLE_AGENCE"), agence)
    poste = postes_service.creer(db, responsable, code="01", libelle="Principal", motif="init")
    comptable = _comptable(_utilisateur(db, agence, "11", "COMPTABLE"), agence)

    postes_service.rattacher_compte(
        db, comptable, poste, compte_caisse_number="101111", motif="rattachement"
    )

    assert poste.compte_caisse_id == _cid(db, "101111")


def test_rattacher_compte_poste_vide_est_legitime(db: Session) -> None:
    agence = _agence(db, "CXQ2")
    responsable = _responsable(_utilisateur(db, agence, "12", "RESPONSABLE_AGENCE"), agence)
    poste = postes_service.creer(db, responsable, code="01", libelle="Principal", motif="init")
    comptable = _comptable(_utilisateur(db, agence, "13", "COMPTABLE"), agence)
    postes_service.rattacher_compte(
        db, comptable, poste, compte_caisse_number="101111", motif="init"
    )

    postes_service.rattacher_compte(db, comptable, poste, compte_caisse_number=None, motif="vidé")

    assert poste.compte_caisse_id is None


def test_rattachement_compte_institution_entiere_hors_agence_du_comptable(db: Session) -> None:
    """compta.plan.manage rattache N'IMPORTE QUEL poste, pas seulement celui de SON agence —
    même portée que les 3 autres écrans Bloc 5."""
    agence_poste = _agence(db, "CXQ3", avec_compte=False)
    agence_comptable = _agence(db, "CXQ4")
    responsable = _responsable(
        _utilisateur(db, agence_poste, "14", "RESPONSABLE_AGENCE"), agence_poste
    )
    poste = postes_service.creer(db, responsable, code="01", libelle="Principal", motif="init")
    comptable = _comptable(_utilisateur(db, agence_comptable, "15", "COMPTABLE"), agence_comptable)

    postes_service.rattacher_compte(
        db, comptable, poste, compte_caisse_number="101111", motif="rattachement réseau"
    )

    assert poste.compte_caisse_id == _cid(db, "101111")


def test_lister_postes_comptable_voit_toutes_les_agences(db: Session) -> None:
    agence_a = _agence(db, "CXQ5")
    agence_b = _agence(db, "CXQ6")
    responsable_a = _responsable(
        _utilisateur(db, agence_a, "16", "RESPONSABLE_AGENCE"), agence_a
    )
    responsable_b = _responsable(
        _utilisateur(db, agence_b, "17", "RESPONSABLE_AGENCE"), agence_b
    )
    poste_a = postes_service.creer(db, responsable_a, code="01", libelle="A", motif="init")
    poste_b = postes_service.creer(db, responsable_b, code="01", libelle="B", motif="init")
    comptable = _comptable(_utilisateur(db, agence_a, "18", "COMPTABLE"), agence_a)

    vus = {p.id for p in postes_service.lister(db, comptable)}

    assert {poste_a.id, poste_b.id} <= vus


# --- Assignation : rattaché OU habilité à l'agence du poste ------------------------------------


def test_assigner_un_caissier_habilite(db: Session) -> None:
    agence = _agence(db, "CXR1")
    responsable = _responsable(_utilisateur(db, agence, "19", "RESPONSABLE_AGENCE"), agence)
    poste = postes_service.creer(db, responsable, code="01", libelle="Principal", motif="init")
    caissier = _utilisateur(db, agence, "20", "CAISSIER")

    postes_service.assigner(db, responsable, poste, user_id=caissier.id)

    assignes = postes_service.lister_assignations(db, poste)
    assert [u.id for u in assignes] == [caissier.id]


def test_assigner_via_habilitation_pas_seulement_rattachement(db: Session) -> None:
    """« Rattaché OU habilité » — un caissier d'une AUTRE agence, mais habilité via
    user_agencies, peut être assigné (même définition que la connexion)."""
    agence_a = _agence(db, "CXR2")
    agence_b = _agence(db, "CXR3")
    responsable_a = _responsable(_utilisateur(db, agence_a, "21", "RESPONSABLE_AGENCE"), agence_a)
    poste_a = postes_service.creer(db, responsable_a, code="01", libelle="A", motif="init")
    caissier_b = _utilisateur(db, agence_b, "22", "CAISSIER")  # rattaché à B, pas A
    db.add(UserAgency(user_id=caissier_b.id, agency_id=agence_a.id))
    db.flush()

    postes_service.assigner(db, responsable_a, poste_a, user_id=caissier_b.id)

    assignes = postes_service.lister_assignations(db, poste_a)
    assert [u.id for u in assignes] == [caissier_b.id]


def test_assigner_utilisateur_hors_perimetre_refuse(db: Session) -> None:
    agence_a = _agence(db, "CXR4")
    agence_b = _agence(db, "CXR5")
    responsable_a = _responsable(_utilisateur(db, agence_a, "23", "RESPONSABLE_AGENCE"), agence_a)
    poste_a = postes_service.creer(db, responsable_a, code="01", libelle="A", motif="init")
    caissier_b = _utilisateur(db, agence_b, "24", "CAISSIER")  # ni rattaché ni habilité à A

    with pytest.raises(UtilisateurHorsPerimetreError):
        postes_service.assigner(db, responsable_a, poste_a, user_id=caissier_b.id)


def test_assigner_idempotent(db: Session) -> None:
    agence = _agence(db, "CXR6")
    responsable = _responsable(_utilisateur(db, agence, "25", "RESPONSABLE_AGENCE"), agence)
    poste = postes_service.creer(db, responsable, code="01", libelle="Principal", motif="init")
    caissier = _utilisateur(db, agence, "26", "CAISSIER")

    postes_service.assigner(db, responsable, poste, user_id=caissier.id)
    postes_service.assigner(db, responsable, poste, user_id=caissier.id)  # rejoué, sans erreur

    nb = db.execute(
        select(PosteAssignation).where(
            PosteAssignation.poste_id == poste.id, PosteAssignation.user_id == caissier.id
        )
    ).all()
    assert len(nb) == 1


def test_revoquer_assignation(db: Session) -> None:
    agence = _agence(db, "CXR7")
    responsable = _responsable(_utilisateur(db, agence, "27", "RESPONSABLE_AGENCE"), agence)
    poste = postes_service.creer(db, responsable, code="01", libelle="Principal", motif="init")
    caissier = _utilisateur(db, agence, "28", "CAISSIER")
    postes_service.assigner(db, responsable, poste, user_id=caissier.id)

    postes_service.revoquer(db, responsable, poste, user_id=caissier.id)

    assert postes_service.lister_assignations(db, poste) == []


def test_revoquer_absente_idempotent(db: Session) -> None:
    agence = _agence(db, "CXR8")
    responsable = _responsable(_utilisateur(db, agence, "29", "RESPONSABLE_AGENCE"), agence)
    poste = postes_service.creer(db, responsable, code="01", libelle="Principal", motif="init")
    caissier = _utilisateur(db, agence, "30", "CAISSIER")

    postes_service.revoquer(db, responsable, poste, user_id=caissier.id)  # jamais assigné


# --- Bloc C : poste choisi mais non assigné à l'acteur -----------------------------------------


def test_ouvrir_session_poste_non_assigne_refuse(db: Session) -> None:
    """Le poste existe, est actif, rattaché, dans SA propre agence — mais l'acteur n'y a jamais
    été affecté (`postes_service.assigner` jamais appelé). Même discipline IDOR que le reste :
    `PosteIntrouvableError` (404), jamais une exception qui révélerait que le poste existe."""
    agence = _agence(db, "CXS1")
    responsable = _responsable(_utilisateur(db, agence, "31", "RESPONSABLE_AGENCE"), agence)
    poste = postes_service.creer(db, responsable, code="01", libelle="Principal", motif="init")
    postes_service.rattacher_compte(
        db, responsable, poste, compte_caisse_number="101111", motif="init"
    )
    caissier_user = _utilisateur(db, agence, "32", "CAISSIER")
    caissier = _caissier(caissier_user, agence)

    with pytest.raises(PosteIntrouvableError):
        ouvrir_session(db, caissier, poste_id=poste.id, fonds_initial=10_000)


# --- Routeur : permissions, bout en bout -------------------------------------------------------


def test_endpoint_creer_poste_sans_permission_403(client: TestClient, db: Session) -> None:
    agence = _agence(db, "CXT1")
    role = db.execute(select(Role).where(Role.code == "CAISSIER")).scalar_one()
    user = User(
        matricule="MAT-CXT1", email="cxt1@ex.com", username="cxt1",
        password_hash=hasher_mot_de_passe("Motdepasse!123"), last_name="T", first_name="C",
        primary_agency_id=agence.id,
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.flush()

    reponse = client.post(
        "/caisse/postes",
        json={"code": "01", "libelle": "Principal", "motif": "test"},
        headers=_entete(user, agence, "CAISSIER"),
    )
    assert reponse.status_code == 403


def test_endpoint_creer_poste_bout_en_bout(client: TestClient, db: Session) -> None:
    agence = _agence(db, "CXT2")
    responsable_user = _utilisateur(db, agence, "33", "RESPONSABLE_AGENCE")

    reponse = client.post(
        "/caisse/postes",
        json={"code": "01", "libelle": "Guichet principal", "motif": "ouverture"},
        headers=_entete(responsable_user, agence, "RESPONSABLE_AGENCE"),
    )

    assert reponse.status_code == 201
    corps = reponse.json()
    assert corps["code"] == "01"
    assert corps["agency_id"] == str(agence.id)
    assert corps["compte_caisse_number"] is None


def test_endpoint_activation_poste_avec_session_ouverte_422(
    client: TestClient, db: Session
) -> None:
    agence = _agence(db, "CXT3")
    responsable_user = _utilisateur(db, agence, "34", "RESPONSABLE_AGENCE")
    responsable = _responsable(responsable_user, agence)
    poste = postes_service.creer(db, responsable, code="01", libelle="Principal", motif="init")
    postes_service.rattacher_compte(
        db, responsable, poste, compte_caisse_number="101111", motif="init"
    )
    caissier_user = _utilisateur(db, agence, "35", "CAISSIER")
    postes_service.assigner(db, responsable, poste, user_id=caissier_user.id)
    ouvrir_session(db, _caissier(caissier_user, agence), poste_id=poste.id, fonds_initial=10_000)
    db.commit()

    reponse = client.patch(
        f"/caisse/postes/{poste.id}/activation",
        json={"is_active": False, "motif": "tentative"},
        headers=_entete(responsable_user, agence, "RESPONSABLE_AGENCE"),
    )

    assert reponse.status_code == 422


def test_endpoint_rattacher_compte_poste_sans_compta_plan_manage_403(
    client: TestClient, db: Session
) -> None:
    agence = _agence(db, "CXT4")
    responsable_user = _utilisateur(db, agence, "36", "RESPONSABLE_AGENCE")
    poste = postes_service.creer(
        db, _responsable(responsable_user, agence), code="01", libelle="Principal", motif="init"
    )
    db.commit()

    reponse = client.patch(
        f"/caisse/postes/{poste.id}/compte-caisse",
        json={"compte_caisse": "101111", "motif": "test"},
        headers=_entete(responsable_user, agence, "RESPONSABLE_AGENCE"),
    )

    assert reponse.status_code == 403


def test_endpoint_assignations_bout_en_bout(client: TestClient, db: Session) -> None:
    agence = _agence(db, "CXT5")
    responsable_user = _utilisateur(db, agence, "37", "RESPONSABLE_AGENCE")
    poste = postes_service.creer(
        db, _responsable(responsable_user, agence), code="01", libelle="Principal", motif="init"
    )
    caissier_user = _utilisateur(db, agence, "38", "CAISSIER")
    db.commit()
    entete = _entete(responsable_user, agence, "RESPONSABLE_AGENCE")

    ajout = client.post(
        f"/caisse/postes/{poste.id}/assignations",
        json={"user_id": str(caissier_user.id)},
        headers=entete,
    )
    assert ajout.status_code == 201
    assert len(ajout.json()) == 1

    retrait = client.delete(
        f"/caisse/postes/{poste.id}/assignations/{caissier_user.id}", headers=entete
    )
    assert retrait.status_code == 204

    liste = client.get(f"/caisse/postes/{poste.id}/assignations", headers=entete)
    assert liste.json() == []


def test_endpoint_ouverture_poste_non_assigne_404(client: TestClient, db: Session) -> None:
    """Pendant HTTP de `test_ouvrir_session_poste_non_assigne_refuse` : poste réel, actif,
    rattaché — jamais assigné au caissier qui tente de l'ouvrir. 404, jamais 403 (IDOR)."""
    agence = _agence(db, "CXT6")
    responsable = _responsable(_utilisateur(db, agence, "39", "RESPONSABLE_AGENCE"), agence)
    poste = postes_service.creer(db, responsable, code="01", libelle="Principal", motif="init")
    postes_service.rattacher_compte(
        db, responsable, poste, compte_caisse_number="101111", motif="init"
    )
    caissier_user = _utilisateur(db, agence, "40", "CAISSIER")
    db.commit()

    reponse = client.post(
        "/caisse/sessions",
        json={"fonds_initial": 10_000, "poste_id": str(poste.id)},
        headers=_entete(caissier_user, agence, "CAISSIER"),
    )

    assert reponse.status_code == 404
