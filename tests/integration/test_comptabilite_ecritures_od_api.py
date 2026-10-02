"""API — Saisie manuelle d'écriture, journal OD (chantier P1, lot 1).

  - journal OD UNIQUEMENT : jamais un champ accepté côté API — impossible à demander, donc
    impossible à contourner ; une pièce d'un AUTRE journal (CA, posée ici par un autre module)
    est invisible depuis cette API (404, pas distingué d'une inexistante) ;
  - brouillon : peut être déséquilibré, se modifie en étant supprimé/recréé, pas de numéro ;
  - validation : exige l'équilibre et >= 2 lignes (délégué au moteur, ecritures.py) ;
  - contre-passation : réservée à une pièce validée, refuse une deuxième fois ;
  - pas de liste noire de compte (décision actée) : tout compte de saisie actif passe ;
  - extra="forbid" : un champ inattendu (ex. journal_id) est un 422, jamais ignoré ;
  - permissions : compta.ecriture.read/post/reverse (déjà attribuées à COMPTABLE), 403 sinon.
"""

import uuid
from collections.abc import Generator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.comptabilite import ecritures, journee
from app.modules.comptabilite.ecritures import LigneSaisie
from app.modules.comptabilite.models import Account, Journal
from app.modules.security.jwt import creer_access_token
from app.modules.security.models import Role, User, UserRole
from app.modules.security.password import hasher_mot_de_passe

pytestmark = pytest.mark.integration

JOUR = date(2026, 6, 15)  # dans l'exercice 2026 réel, déjà ouvert sur cette base de dev


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


@pytest.fixture(autouse=True)
def _journee_ouverte(request: pytest.FixtureRequest) -> None:
    """Chantier P1bis lot 3 : les points de datation (decaissement, remboursement, épargne,
    OD/contre-passation, affectation du résultat...) exigent désormais une journée comptable
    ouverte — ouverte ici, via le VRAI service, pour les tests qui utilisent la fixture `db`
    partagée. `request.fixturenames` évite de la créer pour un test qui ne l'utilise pas."""
    if "db" not in request.fixturenames:
        return
    db = request.getfixturevalue("db")
    journee.ouvrir_journee(db, journee.prochaine_date_ouvree(db), None)


@pytest.fixture
def client(db: Session) -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _agence_id(db: Session) -> uuid.UUID:
    return db.execute(text("SELECT id FROM parameters.agencies LIMIT 1")).scalar_one()


def _entete(db: Session, role_code: str) -> dict[str, str]:
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


def _journal_id(db: Session, code: str) -> uuid.UUID:
    return db.execute(select(Journal.id).where(Journal.code == code)).scalar_one()


def _ligne(account_number: str, side: str, amount: int, label: str | None = None) -> dict:
    return {"account_number": account_number, "side": side, "amount": amount, "label": label}


# --- Création du brouillon -----------------------------------------------------------------


def test_cree_un_brouillon_dans_le_journal_od(client: TestClient, db: Session) -> None:
    _compte(db, "6T901")
    _compte(db, "6T902")
    comptable = _entete(db, "COMPTABLE")

    reponse = client.post(
        "/comptabilite/ecritures",
        json={
            "entry_date": str(JOUR),
            "description": "Test OD manuelle",
            "lignes": [_ligne("6T901", "D", 10000), _ligne("6T902", "C", 10000)],
        },
        headers=comptable,
    )

    assert reponse.status_code == 201
    corps = reponse.json()
    assert corps["status"] == "brouillon"
    assert corps["entry_number"] is None
    assert corps["total_debit"] == 10000
    assert corps["total_credit"] == 10000
    assert corps["equilibree"] is True
    assert {ligne["account_number"] for ligne in corps["lignes"]} == {"6T901", "6T902"}

    # Le journal posé est bien OD — vérifié directement en base, pas seulement via la réponse.
    od_id = _journal_id(db, "OD")
    journal_reel = db.execute(
        text("SELECT journal_id FROM comptabilite.journal_entries WHERE id = :id"),
        {"id": corps["id"]},
    ).scalar_one()
    assert journal_reel == od_id


def test_brouillon_peut_etre_desequilibre(client: TestClient, db: Session) -> None:
    _compte(db, "6T903")
    comptable = _entete(db, "COMPTABLE")

    reponse = client.post(
        "/comptabilite/ecritures",
        json={
            "entry_date": str(JOUR),
            "description": "Brouillon seul (une ligne)",
            "lignes": [_ligne("6T903", "D", 5000)],
        },
        headers=comptable,
    )

    assert reponse.status_code == 201
    assert reponse.json()["equilibree"] is False


def test_compte_inexistant_refuse_en_422_message_clair(client: TestClient, db: Session) -> None:
    comptable = _entete(db, "COMPTABLE")

    reponse = client.post(
        "/comptabilite/ecritures",
        json={
            "entry_date": str(JOUR),
            "description": "Compte inconnu",
            "lignes": [_ligne("9Z999999", "D", 1000), _ligne("9Z999998", "C", 1000)],
        },
        headers=comptable,
    )

    assert reponse.status_code == 422
    assert "9Z999999" in reponse.json()["detail"]


def test_compte_de_regroupement_refuse(client: TestClient, db: Session) -> None:
    _compte(db, "6T904", is_posting=False)
    _compte(db, "6T905")
    comptable = _entete(db, "COMPTABLE")

    reponse = client.post(
        "/comptabilite/ecritures",
        json={
            "entry_date": str(JOUR),
            "description": "Regroupement interdit",
            "lignes": [_ligne("6T904", "D", 1000), _ligne("6T905", "C", 1000)],
        },
        headers=comptable,
    )

    assert reponse.status_code == 422


def test_champ_inattendu_refuse_extra_forbid(client: TestClient, db: Session) -> None:
    comptable = _entete(db, "COMPTABLE")

    reponse = client.post(
        "/comptabilite/ecritures",
        json={
            "entry_date": str(JOUR),
            "description": "Tentative de forcer un autre journal",
            "journal_id": str(uuid.uuid4()),
            "lignes": [_ligne("6T901", "D", 1000), _ligne("6T902", "C", 1000)],
        },
        headers=comptable,
    )

    assert reponse.status_code == 422


def test_sans_permission_refuse_403(client: TestClient, db: Session) -> None:
    caissier = _entete(db, "CAISSIER")

    reponse = client.post(
        "/comptabilite/ecritures",
        json={
            "entry_date": str(JOUR), "description": "Refusé",
            "lignes": [_ligne("6T901", "D", 1000)],
        },
        headers=caissier,
    )

    assert reponse.status_code == 403


# --- Validation ------------------------------------------------------------------------


def test_valide_une_piece_equilibree(client: TestClient, db: Session) -> None:
    _compte(db, "6T906")
    _compte(db, "6T907")
    comptable = _entete(db, "COMPTABLE")
    brouillon = client.post(
        "/comptabilite/ecritures",
        json={
            "entry_date": str(JOUR), "description": "À valider",
            "lignes": [_ligne("6T906", "D", 25000), _ligne("6T907", "C", 25000)],
        },
        headers=comptable,
    ).json()

    reponse = client.post(
        f"/comptabilite/ecritures/{brouillon['id']}/validation", headers=comptable
    )

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["status"] == "validee"
    assert corps["entry_number"] is not None


def test_refuse_de_valider_une_piece_desequilibree(client: TestClient, db: Session) -> None:
    _compte(db, "6T908")
    comptable = _entete(db, "COMPTABLE")
    brouillon = client.post(
        "/comptabilite/ecritures",
        json={
            "entry_date": str(JOUR), "description": "Jamais équilibrée",
            "lignes": [_ligne("6T908", "D", 1000)],
        },
        headers=comptable,
    ).json()

    reponse = client.post(
        f"/comptabilite/ecritures/{brouillon['id']}/validation", headers=comptable
    )

    assert reponse.status_code == 422


def test_refuse_de_revalider_une_piece_deja_validee(client: TestClient, db: Session) -> None:
    _compte(db, "6T909")
    _compte(db, "6T910")
    comptable = _entete(db, "COMPTABLE")
    brouillon = client.post(
        "/comptabilite/ecritures",
        json={
            "entry_date": str(JOUR), "description": "Déjà validée",
            "lignes": [_ligne("6T909", "D", 2000), _ligne("6T910", "C", 2000)],
        },
        headers=comptable,
    ).json()
    client.post(f"/comptabilite/ecritures/{brouillon['id']}/validation", headers=comptable)

    reponse = client.post(
        f"/comptabilite/ecritures/{brouillon['id']}/validation", headers=comptable
    )

    assert reponse.status_code == 422


# --- Contre-passation --------------------------------------------------------------------


def test_contre_passe_une_piece_validee(client: TestClient, db: Session) -> None:
    _compte(db, "6T911")
    _compte(db, "6T912")
    comptable = _entete(db, "COMPTABLE")
    brouillon = client.post(
        "/comptabilite/ecritures",
        json={
            "entry_date": str(JOUR), "description": "À contre-passer",
            "lignes": [_ligne("6T911", "D", 3000), _ligne("6T912", "C", 3000)],
        },
        headers=comptable,
    ).json()
    validee = client.post(
        f"/comptabilite/ecritures/{brouillon['id']}/validation", headers=comptable
    ).json()

    reponse = client.post(
        f"/comptabilite/ecritures/{validee['id']}/contre-passation", headers=comptable
    )

    assert reponse.status_code == 201
    inverse = reponse.json()
    assert inverse["status"] == "validee"
    assert inverse["id"] != validee["id"]
    # Lignes inversées : ce qui était D est devenu C.
    cote = {ligne["account_number"]: ligne["side"] for ligne in inverse["lignes"]}
    assert cote["6T911"] == "C"
    assert cote["6T912"] == "D"

    # La pièce d'origine se sait maintenant contre-passée.
    origine = client.get(
        f"/comptabilite/ecritures/{validee['id']}", headers=comptable
    ).json()
    assert origine["deja_contre_passee"] is True
    assert inverse["est_contre_passation"] is True


def test_refuse_de_contre_passer_un_brouillon(client: TestClient, db: Session) -> None:
    _compte(db, "6T913")
    comptable = _entete(db, "COMPTABLE")
    brouillon = client.post(
        "/comptabilite/ecritures",
        json={
            "entry_date": str(JOUR), "description": "Encore brouillon",
            "lignes": [_ligne("6T913", "D", 1000)],
        },
        headers=comptable,
    ).json()

    reponse = client.post(
        f"/comptabilite/ecritures/{brouillon['id']}/contre-passation", headers=comptable
    )

    assert reponse.status_code == 422


def test_refuse_de_contre_passer_deux_fois(client: TestClient, db: Session) -> None:
    _compte(db, "6T914")
    _compte(db, "6T915")
    comptable = _entete(db, "COMPTABLE")
    brouillon = client.post(
        "/comptabilite/ecritures",
        json={
            "entry_date": str(JOUR), "description": "Double contre-passation",
            "lignes": [_ligne("6T914", "D", 4000), _ligne("6T915", "C", 4000)],
        },
        headers=comptable,
    ).json()
    validee = client.post(
        f"/comptabilite/ecritures/{brouillon['id']}/validation", headers=comptable
    ).json()
    client.post(f"/comptabilite/ecritures/{validee['id']}/contre-passation", headers=comptable)

    reponse = client.post(
        f"/comptabilite/ecritures/{validee['id']}/contre-passation", headers=comptable
    )

    assert reponse.status_code == 422


# --- Suppression -----------------------------------------------------------------------


def test_supprime_un_brouillon(client: TestClient, db: Session) -> None:
    _compte(db, "6T916")
    comptable = _entete(db, "COMPTABLE")
    brouillon = client.post(
        "/comptabilite/ecritures",
        json={
            "entry_date": str(JOUR), "description": "À supprimer",
            "lignes": [_ligne("6T916", "D", 1000)],
        },
        headers=comptable,
    ).json()

    reponse = client.delete(f"/comptabilite/ecritures/{brouillon['id']}", headers=comptable)
    assert reponse.status_code == 204

    introuvable = client.get(f"/comptabilite/ecritures/{brouillon['id']}", headers=comptable)
    assert introuvable.status_code == 404


def test_refuse_de_supprimer_une_piece_validee(client: TestClient, db: Session) -> None:
    _compte(db, "6T917")
    _compte(db, "6T918")
    comptable = _entete(db, "COMPTABLE")
    brouillon = client.post(
        "/comptabilite/ecritures",
        json={
            "entry_date": str(JOUR), "description": "Validée, pas supprimable",
            "lignes": [_ligne("6T917", "D", 1000), _ligne("6T918", "C", 1000)],
        },
        headers=comptable,
    ).json()
    validee = client.post(
        f"/comptabilite/ecritures/{brouillon['id']}/validation", headers=comptable
    ).json()

    reponse = client.delete(f"/comptabilite/ecritures/{validee['id']}", headers=comptable)
    assert reponse.status_code == 422


# --- Cloisonnement au journal OD : rien d'un autre journal n'est visible ici --------------


def test_une_piece_dun_autre_journal_est_invisible_depuis_cet_ecran(
    client: TestClient, db: Session
) -> None:
    """Une pièce posée par un AUTRE module (ici simulée dans le journal CA) ne doit JAMAIS
    apparaître ni être manipulable via l'API de saisie manuelle OD — sinon cet écran deviendrait
    une porte dérobée vers n'importe quelle pièce du système."""
    a = _compte(db, "6T919")
    b = _compte(db, "6T920")
    ca_id = _journal_id(db, "CA")
    entry = ecritures.creer_brouillon(
        db, journal_id=ca_id, entry_date=JOUR, description="Pièce de caisse (pas OD)",
        lignes=[
            LigneSaisie(account_id=a.id, side="D", amount=1000),
            LigneSaisie(account_id=b.id, side="C", amount=1000),
        ],
        par=None,
    )
    db.commit()
    comptable = _entete(db, "COMPTABLE")

    detail = client.get(f"/comptabilite/ecritures/{entry.id}", headers=comptable)
    assert detail.status_code == 404

    validation = client.post(
        f"/comptabilite/ecritures/{entry.id}/validation", headers=comptable
    )
    assert validation.status_code == 404

    liste = client.get("/comptabilite/ecritures", headers=comptable).json()
    assert str(entry.id) not in {ligne["id"] for ligne in liste["lignes"]}


# --- Liste -------------------------------------------------------------------------------


def test_liste_les_ecritures_od_les_plus_recentes_dabord(client: TestClient, db: Session) -> None:
    # `created_at` horodaté EXPLICITEMENT après chaque création : la fixture `db` (SAVEPOINT)
    # garde la MÊME transaction PostgreSQL ouverte sur tout le test, donc NOW() (server_default
    # de created_at) renvoie la même valeur pour les deux pièces — sans cet horodatage explicite,
    # le tri "plus récent d'abord" deviendrait ambigu entre les deux. Un vrai déploiement n'a
    # jamais ce problème : chaque requête HTTP est sa propre transaction.
    _compte(db, "6T921")
    _compte(db, "6T922")
    comptable = _entete(db, "COMPTABLE")
    premiere = client.post(
        "/comptabilite/ecritures",
        json={
            "entry_date": str(JOUR), "description": "Première",
            "lignes": [_ligne("6T921", "D", 1000), _ligne("6T922", "C", 1000)],
        },
        headers=comptable,
    ).json()
    db.execute(
        text("UPDATE comptabilite.journal_entries SET created_at = NOW() - interval '1 second' "
             "WHERE id = :id"),
        {"id": premiere["id"]},
    )
    seconde = client.post(
        "/comptabilite/ecritures",
        json={
            "entry_date": str(JOUR), "description": "Seconde",
            "lignes": [_ligne("6T921", "D", 2000), _ligne("6T922", "C", 2000)],
        },
        headers=comptable,
    ).json()

    reponse = client.get("/comptabilite/ecritures", headers=comptable)

    assert reponse.status_code == 200
    corps = reponse.json()
    ids = [ligne["id"] for ligne in corps["lignes"]]
    assert ids.index(seconde["id"]) < ids.index(premiere["id"])


def test_lecture_sans_permission_refuse_403(client: TestClient, db: Session) -> None:
    caissier = _entete(db, "CAISSIER")

    reponse = client.get("/comptabilite/ecritures", headers=caissier)

    assert reponse.status_code == 403
