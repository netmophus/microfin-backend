"""API Rapports comptables (R1 grand livre, R2 balance) — lecture pure.

  - permissions : compta.rapport.read, 403 sinon ;
  - sélecteur dédié (/comptes/selecteur-rapport) : un compte désactivé mais de saisie apparaît
    (à la différence du sélecteur de rattachement, qui l'exclut) ;
  - grand livre : 404 compte inexistant, 422 compte de regroupement, solde cumulé correct ;
  - LE POINT SENSIBLE : le solde cumulé reste EXACT à travers la pagination — un compte avec
    plusieurs pages de mouvements, le solde en tête de chaque page doit être identique à ce
    qu'on obtiendrait en calculant tout d'un coup, sans pagination ;
  - balance : Σdébit = Σcrédit (invariant natif de la partie double), solde ouverture/clôture.
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
from app.modules.comptabilite import ecritures, rapports
from app.modules.comptabilite.ecritures import LigneSaisie
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


def _caisse_id(db: Session) -> uuid.UUID:
    """Contrepartie JETABLE pour équilibrer les pièces de test — jamais un compte système réel
    (1011 peut légitimement être verrouillé par verrouiller_saisie sans casser ces tests).
    Idempotent : une seule créée par transaction de test, quel que soit le nombre d'appels."""
    existant = db.execute(
        text("SELECT id FROM comptabilite.accounts WHERE account_number = '6TCAIS'")
    ).scalar_one_or_none()
    if existant is not None:
        return existant
    return _compte(db, "6TCAIS").id


def _mouvement(db: Session, compte: Account, side: str, amount: int, jour: date) -> None:
    """Pose une VRAIE pièce validée : `compte` du côté `side`, la caisse de l'autre côté."""
    journal_id = db.execute(
        text("SELECT id FROM comptabilite.journals WHERE code = 'OD'")
    ).scalar_one()
    autre = "C" if side == "D" else "D"
    entry = ecritures.creer_brouillon(
        db,
        journal_id=journal_id,
        entry_date=jour,
        description=f"Mouvement test {amount}",
        lignes=[
            LigneSaisie(account_id=compte.id, side=side, amount=amount),
            LigneSaisie(account_id=_caisse_id(db), side=autre, amount=amount),
        ],
        par=None,
    )
    ecritures.valider(db, entry, par=None)


# --- Permissions ---------------------------------------------------------------------------


def test_grand_livre_sans_permission_403(client: TestClient, db: Session) -> None:
    compte = _compte(db, "6T900")
    caissier = _entete(db, "CAISSIER")  # ne détient pas compta.rapport.read
    reponse = client.get(
        "/comptabilite/grand-livre", params={"compte_id": str(compte.id)}, headers=caissier
    )
    assert reponse.status_code == 403


def test_balance_sans_permission_403(client: TestClient, db: Session) -> None:
    caissier = _entete(db, "CAISSIER")
    reponse = client.get("/comptabilite/balance", headers=caissier)
    assert reponse.status_code == 403


@pytest.mark.parametrize("role_code", ["COMPTABLE", "AUDITEUR_INTERNE", "DIRECTION_GENERALE"])
def test_grand_livre_et_balance_accessibles_aux_3_roles(
    client: TestClient, db: Session, role_code: str
) -> None:
    compte = _compte(db, f"6T9{role_code[:2]}")
    entete = _entete(db, role_code)

    grand_livre = client.get(
        "/comptabilite/grand-livre", params={"compte_id": str(compte.id)}, headers=entete
    )
    assert grand_livre.status_code == 200

    balance = client.get("/comptabilite/balance", headers=entete)
    assert balance.status_code == 200


# --- Sélecteur dédié -----------------------------------------------------------------------


def test_selecteur_rapport_inclut_les_comptes_desactives(client: TestClient, db: Session) -> None:
    _compte(db, "6T910", is_active=False)
    comptable = _entete(db, "COMPTABLE")

    reponse = client.get(
        "/comptabilite/comptes/selecteur-rapport", params={"q": "6T910"}, headers=comptable
    )
    assert reponse.status_code == 200
    ligne = next(c for c in reponse.json() if c["account_number"] == "6T910")
    assert ligne["is_active"] is False

    # Le sélecteur de RATTACHEMENT, lui, l'exclut toujours (comportement inchangé).
    rattachement = client.get(
        "/comptabilite/comptes/selecteur", params={"q": "6T910"}, headers=comptable
    )
    assert rattachement.json() == []


# --- Grand livre — erreurs -------------------------------------------------------------------


def test_grand_livre_compte_introuvable_404(client: TestClient, db: Session) -> None:
    comptable = _entete(db, "COMPTABLE")
    reponse = client.get(
        "/comptabilite/grand-livre", params={"compte_id": str(uuid.uuid4())}, headers=comptable
    )
    assert reponse.status_code == 404


def test_grand_livre_compte_de_regroupement_422(client: TestClient, db: Session) -> None:
    regroupement = _compte(db, "6T920", is_posting=False)
    comptable = _entete(db, "COMPTABLE")
    reponse = client.get(
        "/comptabilite/grand-livre",
        params={"compte_id": str(regroupement.id)},
        headers=comptable,
    )
    assert reponse.status_code == 422


def test_grand_livre_reste_accessible_apres_verrouillage_avec_historique(
    client: TestClient, db: Session
) -> None:
    """Un compte verrouillé APRÈS COUP (verrouiller_saisie, ex. un officiel remplacé par une
    extension à 6 chiffres) garde des écritures réelles — son grand livre doit rester
    consultable, contrairement à un compte de regroupement qui n'en a JAMAIS eu."""
    compte = _compte(db, "6T921")  # sens D
    _mouvement(db, compte, "D", 1000, date(2026, 7, 1))
    compte.is_posting = False
    db.flush()
    comptable = _entete(db, "COMPTABLE")

    reponse = client.get(
        "/comptabilite/grand-livre", params={"compte_id": str(compte.id)}, headers=comptable
    )
    assert reponse.status_code == 200
    assert reponse.json()["lignes"][0]["solde_cumule"] == 1000


# --- Grand livre — solde cumulé (correction de base) ------------------------------------------


def test_grand_livre_solde_cumule_correct(client: TestClient, db: Session) -> None:
    compte = _compte(db, "6T930")  # sens D
    _mouvement(db, compte, "D", 1000, date(2026, 7, 1))
    _mouvement(db, compte, "C", 400, date(2026, 7, 2))
    _mouvement(db, compte, "D", 250, date(2026, 7, 3))
    comptable = _entete(db, "COMPTABLE")

    reponse = client.get(
        "/comptabilite/grand-livre", params={"compte_id": str(compte.id)}, headers=comptable
    )
    corps = reponse.json()
    assert corps["compte"]["is_active"] is True
    lignes = corps["lignes"]
    # Sens normal D : +1000 -> 600 -> 850. Calculé à la main, indépendamment du code testé.
    assert [ligne["solde_cumule"] for ligne in lignes] == [1000, 600, 850]


def test_grand_livre_signale_un_compte_desactive(client: TestClient, db: Session) -> None:
    """Le compte désactivé reste consultable (historique), et l'écran doit pouvoir le savoir
    même sans repasser par le sélecteur — is_active porté par la réponse elle-même."""
    compte = _compte(db, "6T931")
    _mouvement(db, compte, "D", 100, date(2026, 7, 1))
    compte.is_active = False
    db.flush()
    comptable = _entete(db, "COMPTABLE")

    reponse = client.get(
        "/comptabilite/grand-livre", params={"compte_id": str(compte.id)}, headers=comptable
    )
    assert reponse.status_code == 200
    assert reponse.json()["compte"]["is_active"] is False


# --- Grand livre — LE POINT SENSIBLE : solde cumulé stable à travers la pagination -----------


def test_grand_livre_solde_cumule_stable_a_travers_la_pagination(db: Session) -> None:
    """Un compte avec 3 pages de mouvements (taille de page volontairement petite : 2 lignes).

    Le solde en TÊTE de la page 2 et de la page 3 doit être EXACTEMENT celui qu'on obtiendrait
    en demandant tout d'un coup, sans pagination — pas une valeur "proche" ou "presque juste".
    Une erreur d'un cran à la frontière entre deux pages ne se voit pas à l'œil sur une seule
    page mais fausse tout le rapport dès qu'on tourne la page. Les valeurs attendues ne sont PAS
    recalculées par le code testé : elles viennent d'un appel séparé, non paginé (taille=100),
    qui sert de référence indépendante de l'implémentation de la pagination elle-même.
    """
    compte = _compte(db, "6T940")  # sens D
    montants_et_sens = [
        ("D", 1000, date(2026, 7, 1)),
        ("C", 500, date(2026, 7, 2)),
        ("D", 300, date(2026, 7, 3)),
        ("C", 700, date(2026, 7, 4)),
        ("D", 200, date(2026, 7, 5)),
        ("C", 900, date(2026, 7, 6)),
    ]
    for side, amount, jour in montants_et_sens:
        _mouvement(db, compte, side, amount, jour)

    # Référence : tout d'un coup, une seule page large.
    reference = rapports.grand_livre(db, compte, date_debut=None, date_fin=None, page=1, taille=100)
    assert len(reference.lignes) == 6
    soldes_reference = [ligne.solde_cumule for ligne in reference.lignes]

    # Paginé : 3 pages de 2 lignes.
    page1 = rapports.grand_livre(db, compte, date_debut=None, date_fin=None, page=1, taille=2)
    page2 = rapports.grand_livre(db, compte, date_debut=None, date_fin=None, page=2, taille=2)
    page3 = rapports.grand_livre(db, compte, date_debut=None, date_fin=None, page=3, taille=2)

    for page in (page1, page2, page3):
        assert page.total == 6
        assert len(page.lignes) == 2

    soldes_pagines = [ligne.solde_cumule for page in (page1, page2, page3) for ligne in page.lignes]
    assert soldes_pagines == soldes_reference

    # La vérification explicite demandée : la TÊTE de la page 2 (3e ligne globale) et la TÊTE
    # de la page 3 (5e ligne globale) sont exactement les valeurs de référence.
    assert page2.lignes[0].solde_cumule == soldes_reference[2]
    assert page3.lignes[0].solde_cumule == soldes_reference[4]


def test_grand_livre_solde_ouverture_respecte_date_debut(db: Session) -> None:
    compte = _compte(db, "6T941")  # sens D
    _mouvement(db, compte, "D", 1000, date(2026, 7, 1))
    _mouvement(db, compte, "C", 300, date(2026, 7, 2))
    _mouvement(db, compte, "D", 500, date(2026, 7, 10))

    resultat = rapports.grand_livre(
        db, compte, date_debut=date(2026, 7, 5), date_fin=None, page=1, taille=100
    )
    # Solde d'ouverture = mouvements strictement avant le 5 juillet : +1000 -300 = 700.
    assert resultat.solde_ouverture == 700
    assert len(resultat.lignes) == 1
    assert resultat.lignes[0].solde_cumule == 1200  # 700 + 500


# --- Balance ---------------------------------------------------------------------------------


def test_balance_equilibree_et_soldes(client: TestClient, db: Session) -> None:
    compte = _compte(db, "6T950")  # sens D
    _mouvement(db, compte, "D", 1000, date(2026, 7, 15))
    _mouvement(db, compte, "C", 300, date(2026, 7, 16))
    comptable = _entete(db, "COMPTABLE")

    reponse = client.get(
        "/comptabilite/balance",
        params={"date_debut": "2026-07-15", "date_fin": "2026-07-16"},
        headers=comptable,
    )
    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["equilibree"] is True
    assert corps["total_debit"] == corps["total_credit"]

    ligne = next(l for l in corps["lignes"] if l["account_number"] == "6T950")
    assert ligne["solde_ouverture"] == 0
    assert ligne["total_debit"] == 1000
    assert ligne["total_credit"] == 300
    assert ligne["solde_cloture"] == 700


def test_balance_exclut_puis_inclut_les_comptes_sans_mouvement(
    client: TestClient, db: Session
) -> None:
    _compte(db, "6T960")  # jamais mouvementé
    comptable = _entete(db, "COMPTABLE")

    defaut = client.get(
        "/comptabilite/balance", params={"date_debut": "2026-01-01"}, headers=comptable
    )
    assert not any(l["account_number"] == "6T960" for l in defaut.json()["lignes"])

    avec_tous = client.get(
        "/comptabilite/balance",
        params={"date_debut": "2026-01-01", "inclure_sans_mouvement": True},
        headers=comptable,
    )
    ligne = next(l for l in avec_tous.json()["lignes"] if l["account_number"] == "6T960")
    assert ligne["total_debit"] == 0
    assert ligne["total_credit"] == 0
    assert ligne["solde_cloture"] == 0
