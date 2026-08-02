"""API import/export CSV du plan de comptes (Bloc 2 du paramétrage comptable).

  - aperçu : lit et valide SANS écrire ; anomalies -> import bloqué (liste complète) ; fichier
    propre -> diff compte par compte (créations, modifications avec avant/après) + empreinte ;
  - confirmation : exige la MÊME empreinte que l'aperçu (fichier substitué -> refus explicite),
    un motif tracé, tout ou rien comme l'import CLI ; lever_provisoire est un choix explicite,
    jamais par défaut ;
  - export : mêmes colonnes que l'import (ré-importable telle quelle) ;
  - permissions : compta.plan.manage pour aperçu/confirmer, compta.plan.read pour l'export.
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.comptabilite.models import Account
from app.modules.comptabilite.plan import empreinte as calculer_empreinte
from app.modules.security.jwt import creer_access_token
from app.modules.security.models import Role, User, UserRole
from app.modules.security.password import hasher_mot_de_passe

pytestmark = pytest.mark.integration

ENTETE = (
    "account_number;name;short_name;class;parent_number;normal_side;"
    "is_posting;is_system;notes"
)


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


def _csv(*lignes: str) -> bytes:
    corps = "\r\n".join([ENTETE, *lignes]) + "\r\n"
    return ("\N{ZERO WIDTH NO-BREAK SPACE}" + corps).encode("utf-8")


def _fichier(contenu: bytes) -> dict[str, tuple[str, bytes, str]]:
    return {"fichier": ("plan.csv", contenu, "text/csv")}


# --- Aperçu ----------------------------------------------------------------------------


def test_apercu_fichier_propre_distingue_creations_et_modifications(
    client: TestClient, db: Session
) -> None:
    _compte(db, "6T600", name="Ancien libellé")
    comptable = _entete_auth(db, "COMPTABLE")
    contenu = _csv(
        "6T600;Nouveau libellé;;6;;D;TRUE;FALSE;",  # modifié : le libellé change
        "6T601;Compte neuf;;6;;D;TRUE;FALSE;",  # créé
    )

    reponse = client.post(
        "/comptabilite/comptes/import/apercu", files=_fichier(contenu), headers=comptable
    )

    assert reponse.status_code == 200
    donnees = reponse.json()
    assert donnees["anomalies"] == []
    assert donnees["empreinte"]
    assert [c["account_number"] for c in donnees["a_creer"]] == ["6T601"]
    modifie = next(c for c in donnees["a_modifier"] if c["account_number"] == "6T600")
    diff_name = next(d for d in modifie["diffs"] if d["champ"] == "name")
    assert diff_name["avant"] == "Ancien libellé"
    assert diff_name["apres"] == "Nouveau libellé"


def test_apercu_ligne_identique_nest_ni_creee_ni_modifiee(
    client: TestClient, db: Session
) -> None:
    _compte(db, "6T610", name="Compte stable", short_name=None, notes=None)
    comptable = _entete_auth(db, "COMPTABLE")
    contenu = _csv("6T610;Compte stable;;6;;D;TRUE;FALSE;")

    reponse = client.post(
        "/comptabilite/comptes/import/apercu", files=_fichier(contenu), headers=comptable
    )

    donnees = reponse.json()
    assert donnees["a_creer"] == []
    assert donnees["a_modifier"] == []
    assert donnees["inchanges"] == 1


def test_apercu_anomalies_bloquent_sans_empreinte_ni_diff(
    client: TestClient, db: Session
) -> None:
    comptable = _entete_auth(db, "COMPTABLE")
    contenu = _csv("6T620;;;6;;D;TRUE;FALSE;")  # libellé vide

    reponse = client.post(
        "/comptabilite/comptes/import/apercu", files=_fichier(contenu), headers=comptable
    )

    assert reponse.status_code == 200
    donnees = reponse.json()
    assert len(donnees["anomalies"]) == 1
    assert "libellé" in donnees["anomalies"][0]
    assert donnees["empreinte"] is None


def test_apercu_sans_permission_403(client: TestClient, db: Session) -> None:
    caissier = _entete_auth(db, "CAISSIER")
    contenu = _csv("6T630;Test;;6;;D;TRUE;FALSE;")

    reponse = client.post(
        "/comptabilite/comptes/import/apercu", files=_fichier(contenu), headers=caissier
    )
    assert reponse.status_code == 403


# --- Confirmation ------------------------------------------------------------------------


def test_confirmer_ecrit_et_trace_le_motif(client: TestClient, db: Session) -> None:
    comptable = _entete_auth(db, "COMPTABLE")
    contenu = _csv("6T700;Nouveau compte;;6;;D;TRUE;FALSE;")
    empreinte = client.post(
        "/comptabilite/comptes/import/apercu", files=_fichier(contenu), headers=comptable
    ).json()["empreinte"]

    reponse = client.post(
        "/comptabilite/comptes/import/confirmer",
        files=_fichier(contenu),
        data={"empreinte": empreinte, "motif": "Import du plan corrigé"},
        headers=comptable,
    )

    assert reponse.status_code == 200
    donnees = reponse.json()
    assert donnees["crees"] == 1
    assert donnees["provisoire_leve"] is False

    cree = db.execute(
        select(Account).where(Account.account_number == "6T700")
    ).scalar_one()
    assert cree.is_provisional is True  # créé par import CSV -> provisoire par défaut

    ligne = db.execute(
        text(
            "SELECT new_values FROM audit.audit_logs WHERE action = 'compta.plan.imported' "
            "ORDER BY occurred_at DESC LIMIT 1"
        )
    ).one()
    assert ligne.new_values["motif"] == "Import du plan corrigé"
    assert ligne.new_values["crees"] == 1


def test_confirmer_leve_le_provisoire_si_demande(client: TestClient, db: Session) -> None:
    comptable = _entete_auth(db, "COMPTABLE")
    contenu = _csv("6T710;Compte validé par l'expert;;6;;D;TRUE;FALSE;")
    empreinte = client.post(
        "/comptabilite/comptes/import/apercu", files=_fichier(contenu), headers=comptable
    ).json()["empreinte"]

    reponse = client.post(
        "/comptabilite/comptes/import/confirmer",
        files=_fichier(contenu),
        data={"empreinte": empreinte, "motif": "Validation experte", "lever_provisoire": "true"},
        headers=comptable,
    )

    assert reponse.status_code == 200
    assert reponse.json()["provisoire_leve"] is True
    compte = db.execute(
        select(Account).where(Account.account_number == "6T710")
    ).scalar_one()
    assert compte.is_provisional is False


def test_confirmer_fichier_different_refuse(client: TestClient, db: Session) -> None:
    comptable = _entete_auth(db, "COMPTABLE")
    original = _csv("6T720;Compte;;6;;D;TRUE;FALSE;")
    autre = _csv("6T721;Autre compte;;6;;D;TRUE;FALSE;")
    empreinte = client.post(
        "/comptabilite/comptes/import/apercu", files=_fichier(original), headers=comptable
    ).json()["empreinte"]

    reponse = client.post(
        "/comptabilite/comptes/import/confirmer",
        files=_fichier(autre),
        data={"empreinte": empreinte, "motif": "Tentative"},
        headers=comptable,
    )

    assert reponse.status_code == 422
    assert "changé" in reponse.json()["detail"]
    assert (
        db.execute(
            select(Account).where(Account.account_number == "6T721")
        ).scalar_one_or_none()
        is None
    )


def test_confirmer_motif_absent_refuse(client: TestClient, db: Session) -> None:
    comptable = _entete_auth(db, "COMPTABLE")
    contenu = _csv("6T730;Compte;;6;;D;TRUE;FALSE;")
    empreinte = client.post(
        "/comptabilite/comptes/import/apercu", files=_fichier(contenu), headers=comptable
    ).json()["empreinte"]

    reponse = client.post(
        "/comptabilite/comptes/import/confirmer",
        files=_fichier(contenu),
        data={"empreinte": empreinte, "motif": ""},
        headers=comptable,
    )
    assert reponse.status_code == 422


def test_confirmer_anomalies_naboutit_a_rien(client: TestClient, db: Session) -> None:
    # Empreinte du fichier lui-même donnée (le rare cas où le fichier n'a pas bougé depuis un
    # aperçu propre mais où on force quand même une anomalie côté confirmation) : le vrai filet,
    # c'est que même une empreinte qui matche n'ouvre pas la porte à un fichier fautif.
    comptable = _entete_auth(db, "COMPTABLE")
    contenu = _csv("6T740;;;6;;D;TRUE;FALSE;")  # libellé vide

    reponse = client.post(
        "/comptabilite/comptes/import/confirmer",
        files=_fichier(contenu),
        data={"empreinte": calculer_empreinte(contenu), "motif": "Tentative"},
        headers=comptable,
    )

    assert reponse.status_code == 422
    assert "libellé" in reponse.json()["detail"]
    assert (
        db.execute(
            select(Account).where(Account.account_number == "6T740")
        ).scalar_one_or_none()
        is None
    )


def test_confirmer_sans_permission_403(client: TestClient, db: Session) -> None:
    caissier = _entete_auth(db, "CAISSIER")
    contenu = _csv("6T750;Compte;;6;;D;TRUE;FALSE;")

    reponse = client.post(
        "/comptabilite/comptes/import/confirmer",
        files=_fichier(contenu),
        data={"empreinte": "x", "motif": "Tentative"},
        headers=caissier,
    )
    assert reponse.status_code == 403


# --- Export ----------------------------------------------------------------------------


def test_export_contient_les_comptes_et_les_colonnes_de_reimport(
    client: TestClient, db: Session
) -> None:
    _compte(db, "6T800", name="Compte exporté")
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.get("/comptabilite/comptes/export", headers=comptable)

    assert reponse.status_code == 200
    assert reponse.headers["content-type"].startswith("text/csv")
    texte = reponse.text.lstrip("﻿")
    premiere_ligne = texte.splitlines()[0]
    for colonne in ENTETE.split(";"):
        assert colonne in premiere_ligne
    assert "6T800;Compte exporté" in texte


def test_export_exclut_les_inactifs_si_demande(client: TestClient, db: Session) -> None:
    _compte(db, "6T810", is_active=False)
    comptable = _entete_auth(db, "COMPTABLE")

    avec_inactifs = client.get("/comptabilite/comptes/export", headers=comptable)
    assert "6T810" in avec_inactifs.text

    sans_inactifs = client.get(
        "/comptabilite/comptes/export",
        params={"inclure_inactifs": False},
        headers=comptable,
    )
    assert "6T810" not in sans_inactifs.text


def test_export_sans_permission_403(client: TestClient, db: Session) -> None:
    _compte(db, "6T820")
    caissier = _entete_auth(db, "CAISSIER")  # ne détient pas compta.plan.read
    reponse = client.get("/comptabilite/comptes/export", headers=caissier)
    assert reponse.status_code == 403
