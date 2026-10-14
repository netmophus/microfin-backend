"""Lot 4 des documents : journal du jour et balance du jour.

  - à la CLÔTURE de la journée : générés, archivés et figés (après le commit) ;
  - à la DEMANDE pour la journée ouverte : brouillon rendu à la volée, jamais archivé ;
  - accès réservé à `compta.rapport.read` (la comptabilité est centralisée, pas par agence).
"""

from collections.abc import Generator
from datetime import timedelta
from io import BytesIO
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.caisse.models import CaisseSession
from app.modules.comptabilite import journee
from app.modules.documents import etats, service
from app.modules.documents.models import STATUT_ECHEC, STATUT_OK, Archive
from app.modules.security.autorisation import UtilisateurCourant
from tests.integration.test_documents_recus import Cadre, _texte, _utilisateur

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


MONTANT = 10_000


def _fermer_la_caisse(db: Session, client: TestClient, c: Cadre) -> None:
    session = db.execute(
        select(CaisseSession).where(CaisseSession.caissier_id == c.caissier.id)
    ).scalar_one()
    reponse = client.post(
        f"/caisse/sessions/{session.id}/fermeture", json={"montant_reel": MONTANT},
        headers=c.entete,
    )
    assert reponse.status_code == 200, reponse.text


def _cloturer(db: Session, client: TestClient, c: Cadre) -> dict[str, str]:
    """Une écriture (un dépôt), la caisse fermée, puis la clôture par un gestionnaire."""
    c.deposer(client, MONTANT)
    _fermer_la_caisse(db, client, c)
    gestionnaire = _utilisateur(db, c.agence, "ADMIN_FONCTIONNEL")[1]
    reponse = client.post("/comptabilite/journees/cloture", headers=gestionnaire)
    assert reponse.status_code == 200, reponse.text
    return gestionnaire


def _etats(db: Session) -> dict[str, Archive]:
    lignes = db.execute(
        select(Archive).where(Archive.type.in_(service.TYPES_ETATS_COMPTABLES))
    ).scalars().all()
    return {a.type: a for a in lignes}


def _courant(db: Session, c: Cadre) -> UtilisateurCourant:
    return UtilisateurCourant(
        user_id=c.caissier.id, roles=(), permissions=frozenset(),
        primary_agency_id=c.agence.id, agency_id=c.agence.id, voit_tout=True,
    )


# --- À la clôture : archivés et figés -----------------------------------------------------------


def test_la_cloture_archive_le_journal_et_la_balance_figes(
    db: Session, client: TestClient
) -> None:
    c = Cadre(db, "ET1")

    _cloturer(db, client, c)

    archives = _etats(db)
    assert set(archives) == {"journal_du_jour", "balance_du_jour"}
    for archive in archives.values():
        assert (archive.module, archive.statut) == ("comptabilite", STATUT_OK)
        assert archive.journee_comptable == c.journee
        assert archive.cle_naturelle == c.journee.isoformat()
        assert archive.chemin_relatif.startswith(
            f"ET1/{c.journee.year:04d}/{c.journee.month:02d}/{c.journee.day:02d}/comptabilite/"
        )
    journal = _texte(archives["journal_du_jour"])
    for attendu in ("Journal du jour", "10 000 F", "101111", "Écritures de la journée"):
        assert attendu in journal, attendu
    assert "BROUILLON" not in journal
    balance = _texte(archives["balance_du_jour"])
    for attendu in ("Balance générale arrêtée à la journée", "Soldes cumulés", "101111"):
        assert attendu in balance, attendu
    assert "BROUILLON" not in balance


def test_la_balance_porte_les_soldes_cumules_pas_seulement_les_mouvements_du_jour(
    db: Session, client: TestClient
) -> None:
    c = Cadre(db, "ET2")
    _cloturer(db, client, c)
    # Journée suivante, SANS aucune écriture : la balance arrêtée garde les soldes de la veille.
    suivante = journee.ouvrir_journee(db, c.journee + timedelta(days=1), None)
    journee.cloturer_journee(db, None)
    db.commit()

    archives = etats.archiver_etats_cloture(db, _courant(db, c), suivante.date_comptable)

    par_type = {a.type: a for a in archives}
    balance = _texte(par_type["balance_du_jour"])
    assert "101111" in balance and "10 000 F" in balance  # solde cumulé de la caisse
    assert "Aucune écriture validée" in _texte(par_type["journal_du_jour"])


def test_la_generation_est_idempotente(db: Session, client: TestClient) -> None:
    c = Cadre(db, "ET3")
    _cloturer(db, client, c)
    avant = _etats(db)

    etats.archiver_etats_cloture(db, _courant(db, c), c.journee)

    assert {t: a.id for t, a in _etats(db).items()} == {t: a.id for t, a in avant.items()}
    assert len(db.execute(select(Archive.id).where(Archive.type == "journal_du_jour")).all()) == 1


def test_un_echec_de_generation_ne_bloque_pas_la_cloture_et_se_regenere(
    db: Session, client: TestClient
) -> None:
    c = Cadre(db, "ET4")
    c.deposer(client, MONTANT)
    _fermer_la_caisse(db, client, c)
    gestionnaire = _utilisateur(db, c.agence, "ADMIN_FONCTIONNEL")[1]

    with patch.object(service, "_rendre", side_effect=RuntimeError("pango absent")):
        reponse = client.post("/comptabilite/journees/cloture", headers=gestionnaire)

    assert reponse.status_code == 200, reponse.text  # la clôture est acquise
    assert reponse.json()["status"] == "cloturee"
    archives = _etats(db)
    assert {a.statut for a in archives.values()} == {STATUT_ECHEC}
    comptable = _utilisateur(db, c.agence, "COMPTABLE")[1]
    regeneration = client.post(
        f"/documents/{archives['journal_du_jour'].id}/regenerer", headers=comptable
    )
    assert regeneration.status_code == 200, regeneration.text
    assert regeneration.json()["statut"] == STATUT_OK


# --- Accès ---------------------------------------------------------------------------------------


def test_le_comptable_voit_et_telecharge_les_etats_les_autres_roles_non(
    db: Session, client: TestClient
) -> None:
    c = Cadre(db, "ET5")
    _cloturer(db, client, c)
    archive = _etats(db)["journal_du_jour"]
    comptable = _utilisateur(db, c.agence, "COMPTABLE")[1]
    # Un responsable DE LA MÊME agence (documents.read.agence) mais sans compta.rapport.read.
    responsable = _utilisateur(db, c.agence, "RESPONSABLE_AGENCE")[1]

    lecture = client.get(f"/documents/{archive.id}/fichier", headers=comptable)
    assert lecture.status_code == 200
    assert lecture.content.startswith(b"%PDF")
    types_vus = {
        ligne["type"] for ligne in client.get("/documents", headers=comptable).json()["lignes"]
    }
    assert {"journal_du_jour", "balance_du_jour"} <= types_vus

    refus = client.get(f"/documents/{archive.id}/fichier", headers=responsable)
    assert refus.status_code == 404  # hors périmètre : jamais 403
    types_responsable = {
        ligne["type"] for ligne in client.get("/documents", headers=responsable).json()["lignes"]
    }
    assert not types_responsable & set(service.TYPES_ETATS_COMPTABLES)
    caissier_refus = client.get(f"/documents/{archive.id}/fichier", headers=c.entete)
    assert caissier_refus.status_code == 404


# --- Brouillon de la journée ouverte --------------------------------------------------------------


def test_le_brouillon_n_est_pas_archive_et_se_dit_brouillon(
    db: Session, client: TestClient
) -> None:
    c = Cadre(db, "ET6")
    c.deposer(client, MONTANT)
    comptable = _utilisateur(db, c.agence, "COMPTABLE")[1]
    avant = db.execute(select(Archive.id)).all()

    reponse = client.get("/documents/etats/journal-du-jour/brouillon", headers=comptable)

    assert reponse.status_code == 200, reponse.text
    assert reponse.headers["content-type"] == "application/pdf"
    assert "brouillon_journal_du_jour" in reponse.headers["content-disposition"]
    assert reponse.content.startswith(b"%PDF")
    assert db.execute(select(Archive.id)).all() == avant  # un brouillon n'est pas une pièce
    audit = db.execute(
        text("SELECT count(*) FROM audit.audit_logs WHERE action = 'documents.brouillon.genere'")
    ).scalar_one()
    assert audit == 1  # consulter la situation comptable est un acte à tracer


def test_le_brouillon_de_la_balance_porte_le_marquage_brouillon(
    db: Session, client: TestClient
) -> None:
    c = Cadre(db, "ET7")
    c.deposer(client, MONTANT)
    comptable = _utilisateur(db, c.agence, "COMPTABLE")[1]

    reponse = client.get("/documents/etats/balance-du-jour/brouillon", headers=comptable)

    assert reponse.status_code == 200, reponse.text
    texte = " ".join(p.extract_text() for p in PdfReader(BytesIO(reponse.content)).pages)
    assert "BROUILLON" in texte and "10 000" in texte.replace("\xa0", " ")


def test_le_brouillon_refuse_une_autre_journee_et_un_role_sans_droit(
    db: Session, client: TestClient
) -> None:
    c = Cadre(db, "ET8")
    comptable = _utilisateur(db, c.agence, "COMPTABLE")[1]

    autre_journee = client.get(
        "/documents/etats/journal-du-jour/brouillon", params={"journee": "2001-01-01"},
        headers=comptable,
    )
    sans_droit = client.get("/documents/etats/journal-du-jour/brouillon", headers=c.entete)
    inconnu = client.get("/documents/etats/inconnu/brouillon", headers=comptable)

    assert autre_journee.status_code == 422
    assert "journée ouverte" in autre_journee.json()["detail"]
    assert sans_droit.status_code == 403
    assert inconnu.status_code == 404


def test_aucun_brouillon_sans_journee_ouverte(db: Session, client: TestClient) -> None:
    c = Cadre(db, "ET9")
    _fermer = _utilisateur(db, c.agence, "ADMIN_FONCTIONNEL")[1]
    comptable = _utilisateur(db, c.agence, "COMPTABLE")[1]
    session = db.execute(
        select(CaisseSession).where(CaisseSession.caissier_id == c.caissier.id)
    ).scalar_one()
    client.post(
        f"/caisse/sessions/{session.id}/fermeture", json={"montant_reel": 0}, headers=c.entete
    )
    assert client.post("/comptabilite/journees/cloture", headers=_fermer).status_code == 200

    reponse = client.get("/documents/etats/journal-du-jour/brouillon", headers=comptable)

    assert reponse.status_code == 422
    assert "Aucune journée comptable" in reponse.json()["detail"]
