"""Lot 3 des documents : reçus de parts sociales (souscription au comptant, libération,
remboursement) et lettre de demande d'explication d'un manquant de caisse, en PDF serveur archivé.

Tout passe par l'API : ce sont les routeurs qui archivent, après le commit de l'opération.
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.caisse.models import CaisseSession
from app.modules.comptabilite import journee
from app.modules.documents.models import Archive
from tests.integration.test_documents_recus import (
    Cadre,
    _archive,
    _pdf,
    _texte,
    _utilisateur,
)
from tests.integration.test_parts_api import _cadre, _entete, _ouvrir_session_caisse

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


@pytest.fixture(autouse=True)
def _journee_ouverte(request: pytest.FixtureRequest) -> None:
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


# --- Reçus de parts --------------------------------------------------------------------------


def test_la_souscription_comptant_archive_son_recu(db: Session, client: TestClient) -> None:
    agence, tier_id = _cadre(db, "DP1")
    caissier = _entete(db, agence, "CAISSIER")
    _ouvrir_session_caisse(db, agence, caissier)

    reponse = client.post(
        f"/tiers/{tier_id}/parts/souscription-comptant", json={"shares_count": 10}, headers=caissier
    )

    assert reponse.status_code == 200, reponse.text
    corps = reponse.json()
    assert corps["document"]["statut"] == "ok"
    archive = _archive(db, corps["document"]["id"])
    jour = journee.date_comptable_courante(db)
    assert (archive.type, archive.module) == ("recu_parts_souscription", "tiers")
    assert archive.journee_comptable == jour and archive.numero_piece == corps["entry_number"]
    assert (archive.agency_id, archive.objet_lie_id) == (agence.id, tier_id)
    assert archive.chemin_relatif.startswith(
        f"{agence.code}/{jour.year:04d}/{jour.month:02d}/{jour.day:02d}/tiers/"
    )
    texte = _texte(archive)
    assert len(_pdf(archive).pages) == 1
    for attendu in (
        "Reçu de souscription de parts sociales",
        f"Reçu n° {corps['entry_number']}",
        "Nombre de parts",
        "5 000 F",
        "50 000 F",
        "cinquante mille francs CFA",
        "Signature du client (facultative)",
    ):
        assert attendu in texte, attendu


def test_la_liberation_archive_son_recu(db: Session, client: TestClient) -> None:
    agence, tier_id = _cadre(db, "DP2")
    charge = _entete(db, agence, "CHARGE_CLIENTELE")
    caissier = _entete(db, agence, "CAISSIER")
    _ouvrir_session_caisse(db, agence, caissier)
    client.post(f"/tiers/{tier_id}/parts/souscription", json={"shares_count": 3}, headers=charge)

    reponse = client.post(
        f"/tiers/{tier_id}/parts/liberation", json={"shares_count": 3}, headers=caissier
    )

    assert reponse.status_code == 200, reponse.text
    archive = _archive(db, reponse.json()["document"]["id"])
    assert archive.type == "recu_parts_liberation"
    texte = _texte(archive)
    assert "Reçu de libération de parts sociales" in texte
    assert "15 000 F" in texte and "quinze mille francs CFA" in texte


def test_la_souscription_sans_paiement_n_a_pas_de_recu(db: Session, client: TestClient) -> None:
    agence, tier_id = _cadre(db, "DP3")
    charge = _entete(db, agence, "CHARGE_CLIENTELE")
    avant = db.execute(select(Archive.id)).all()

    reponse = client.post(
        f"/tiers/{tier_id}/parts/souscription", json={"shares_count": 2}, headers=charge
    )

    assert reponse.status_code == 200
    assert reponse.json()["document"] is None  # aucun espèce encaissé : pas de reçu de caisse
    assert db.execute(select(Archive.id)).all() == avant


def test_le_remboursement_archive_son_recu_signature_obligatoire(
    db: Session, client: TestClient
) -> None:
    agence, tier_id = _cadre(db, "DP4")
    responsable = _entete(db, agence, "RESPONSABLE_AGENCE")
    _ouvrir_session_caisse(db, agence, responsable)
    client.post(
        f"/tiers/{tier_id}/parts/souscription-comptant",
        json={"shares_count": 10},
        headers=responsable,
    )

    reponse = client.post(
        f"/tiers/{tier_id}/parts/remboursement", json={"shares_count": 4}, headers=responsable
    )

    assert reponse.status_code == 200, reponse.text
    archive = _archive(db, reponse.json()["document"]["id"])
    assert archive.type == "recu_parts_remboursement"
    texte = _texte(archive)
    assert "Reçu de remboursement de parts sociales" in texte
    assert "Total remis au client" in texte and "20 000 F" in texte
    assert "vingt mille francs CFA" in texte
    assert "Signature du client (obligatoire)" in texte


def test_un_refus_de_parts_n_archive_rien(db: Session, client: TestClient) -> None:
    agence, tier_id = _cadre(db, "DP5")
    caissier = _entete(db, agence, "CAISSIER")  # pas de session ouverte : refus
    avant = db.execute(select(Archive.id)).all()

    reponse = client.post(
        f"/tiers/{tier_id}/parts/souscription-comptant", json={"shares_count": 1}, headers=caissier
    )

    assert reponse.status_code == 422
    assert db.execute(select(Archive.id)).all() == avant


# --- Lettre d'explication ----------------------------------------------------------------------


def _fermer_avec_manquant(db: Session, client: TestClient, c: Cadre) -> uuid.UUID:
    c.deposer(client, 10_000)
    session = db.execute(
        select(CaisseSession).where(CaisseSession.caissier_id == c.caissier.id)
    ).scalar_one()
    reponse = client.post(
        f"/caisse/sessions/{session.id}/fermeture",
        json={"montant_reel": 4_000, "motif": "Erreur de comptage"},
        headers=c.entete,
    )
    assert reponse.status_code == 200, reponse.text
    return session.id


def _responsable(db: Session, c: Cadre) -> dict[str, str]:
    return _utilisateur(db, c.agence, "RESPONSABLE_AGENCE")[1]


def test_la_lettre_est_archivee_une_seule_fois(db: Session, client: TestClient) -> None:
    c = Cadre(db, "LT1")
    session_id = _fermer_avec_manquant(db, client, c)
    responsable = _responsable(db, c)

    premiere = client.post(f"/caisse/sessions/{session_id}/lettre-explication", headers=responsable)
    seconde = client.post(f"/caisse/sessions/{session_id}/lettre-explication", headers=responsable)

    assert premiere.status_code == 200, premiere.text
    assert seconde.json() == premiere.json()  # la 2e demande rend l'archive, ne régénère pas
    archives = (
        db.execute(select(Archive).where(Archive.type == "lettre_explication_ecart"))
        .scalars()
        .all()
    )
    assert len(archives) == 1
    archive = archives[0]
    assert (archive.module, archive.statut) == ("caisse", "ok")
    assert archive.journee_comptable == c.journee and archive.objet_lie_id == session_id
    assert archive.acteur_id == c.caissier.id
    assert archive.chemin_relatif.startswith(
        f"LT1/{c.journee.year:04d}/{c.journee.month:02d}/{c.journee.day:02d}/caisse/"
    )
    texte = _texte(archive)
    assert len(_pdf(archive).pages) == 1
    for attendu in (
        "Lettre de demande d'explication",
        "Souley Awa",
        "10 000 F",
        "4 000 F",
        "6 000 F",
        "Écart (manquant)",
        "48 heures",
        "RÉPONSE DU CAISSIER",
        "Signature et cachet du responsable",
    ):
        assert attendu in texte, attendu


def test_pas_de_lettre_pour_une_session_sans_manquant(db: Session, client: TestClient) -> None:
    c = Cadre(db, "LT2")
    c.deposer(client, 10_000)
    session = db.execute(
        select(CaisseSession).where(CaisseSession.caissier_id == c.caissier.id)
    ).scalar_one()
    client.post(
        f"/caisse/sessions/{session.id}/fermeture",
        json={"montant_reel": 10_000},
        headers=c.entete,
    )

    reponse = client.post(
        f"/caisse/sessions/{session.id}/lettre-explication", headers=_responsable(db, c)
    )

    assert reponse.status_code == 422
    assert "n'a pas de manquant" in reponse.json()["detail"]


def test_la_lettre_est_cloisonnee_par_agence(db: Session, client: TestClient) -> None:
    c = Cadre(db, "LT3")
    session_id = _fermer_avec_manquant(db, client, c)
    autre = Cadre(db, "LT4")
    responsable_autre = _responsable(db, autre)

    reponse = client.post(
        f"/caisse/sessions/{session_id}/lettre-explication", headers=responsable_autre
    )

    assert reponse.status_code == 404  # hors périmètre : jamais 403, jamais d'archive créée
    assert not db.execute(
        select(Archive.id).where(Archive.type == "lettre_explication_ecart")
    ).all()


def test_la_lettre_exige_la_lecture_des_documents(db: Session, client: TestClient) -> None:
    c = Cadre(db, "LT5")
    session_id = _fermer_avec_manquant(db, client, c)
    # Un rôle qui lit les sessions des autres mais sans documents.read : refus net.
    sans_documents = _utilisateur(db, c.agence, "CHARGE_CLIENTELE")[1]

    reponse = client.post(
        f"/caisse/sessions/{session_id}/lettre-explication", headers=sans_documents
    )

    assert reponse.status_code == 403
