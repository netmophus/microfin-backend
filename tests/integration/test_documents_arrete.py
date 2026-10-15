"""Arrêté de caisse : généré et archivé à CHAQUE fermeture de session, une seule fois par session.

Tout passe par l'API de fermeture : c'est le routeur qui archive, après le commit de la fermeture.
"""

import re
import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.caisse.models import CaisseSession
from app.modules.caisse.router import _vers_schema
from app.modules.caisse.service import calculer_mouvements, calculer_solde_theorique
from app.modules.comptabilite import journee
from app.modules.documents import arretes, service
from app.modules.documents.models import Archive, Numerotation
from tests.integration.test_documents_recus import Cadre, _pdf, _texte, _utilisateur

pytestmark = pytest.mark.integration

MOTIF = "Erreur de comptage"
FORMAT_NUMERO = re.compile(r"^AC-(\d{4})-(\d{6})$")


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


def _session_de(db: Session, c: Cadre) -> CaisseSession:
    return db.execute(
        select(CaisseSession).where(CaisseSession.caissier_id == c.caissier.id)
    ).scalar_one()


def _fermer(
    client: TestClient, c: Cadre, session: CaisseSession, montant: int, motif: str | None = None
) -> dict[str, object]:
    corps: dict[str, object] = {"montant_reel": montant}
    if motif is not None:
        corps["motif"] = motif
    reponse = client.post(f"/caisse/sessions/{session.id}/fermeture", json=corps, headers=c.entete)
    assert reponse.status_code == 200, reponse.text
    return dict(reponse.json())


def _arretes(db: Session) -> list[Archive]:
    return list(
        db.execute(select(Archive).where(Archive.type == arretes.TYPE_ARRETE)).scalars().all()
    )


def test_la_fermeture_sans_ecart_archive_l_arrete(db: Session, client: TestClient) -> None:
    c = Cadre(db, "AR1")
    c.deposer(client, 10_000)
    session = _session_de(db, c)
    theorique = session.fonds_initial + 10_000

    resultat = _fermer(client, c, session, theorique)

    archives = _arretes(db)
    assert len(archives) == 1
    archive = archives[0]
    assert (archive.module, archive.statut) == ("caisse", "ok")
    assert archive.objet_lie_id == session.id and archive.cle_naturelle == str(session.id)
    assert archive.acteur_id == c.caissier.id
    # Daté et classé par la JOURNÉE COMPTABLE de la session.
    assert archive.journee_comptable == session.journee_comptable == c.journee
    assert archive.chemin_relatif.startswith(
        f"AR1/{c.journee.year:04d}/{c.journee.month:02d}/{c.journee.day:02d}/caisse/"
    )
    correspondance = FORMAT_NUMERO.match(archive.numero_piece or "")
    assert correspondance is not None and int(correspondance.group(1)) == c.journee.year
    assert archive.sha256 is not None and archive.taille and archive.taille > 0
    # La réponse de fermeture porte la référence du document à télécharger.
    assert resultat["arrete"] == {
        "id": str(archive.id),
        "nom": service.nom_de_fichier(archive),
        "statut": "ok",
    }
    texte = _texte(archive)
    assert len(_pdf(archive).pages) == 1
    for attendu in (
        "ARRÊTÉ DE CAISSE",
        archive.numero_piece or "?",
        "AR1",
        "Souley Awa",
        "Ouverture de session (heure réelle)",
        "Fermeture de session (heure réelle)",
        # « T otal » : le crénage de la police coupe le T à l'extraction du texte du PDF.
        "otal des entrées",
        "otal des sorties",
        "Solde théorique",
        "Montant compté à la fermeture",
        "Écart (compté \u2212 théorique)",
        "Signature du caissier",
    ):
        assert attendu in texte, attendu
    assert "Motif de l'écart" not in texte  # écart nul : pas de motif à rappeler


def test_la_fermeture_avec_ecart_affiche_l_ecart_et_le_motif(
    db: Session, client: TestClient
) -> None:
    c = Cadre(db, "AR2")
    c.deposer(client, 10_000)
    session = _session_de(db, c)

    _fermer(client, c, session, session.fonds_initial + 4_000, MOTIF)

    archive = _arretes(db)[0]
    texte = _texte(archive)
    assert "Motif de l'écart" in texte and MOTIF in texte
    assert "-6 000 F" in texte  # compté moins théorique : un manquant est négatif
    assert archive.contexte["ecart"] == -6_000
    assert archive.contexte["motif_ecart"] == MOTIF


def test_les_chiffres_de_l_arrete_sont_ceux_de_l_ecran(db: Session, client: TestClient) -> None:
    c = Cadre(db, "AR3")
    c.deposer(client, 10_000)
    retrait = client.post(
        f"/epargne/comptes/{c.compte.id}/retrait", json={"montant": 3_000}, headers=c.entete
    )
    assert retrait.status_code == 200, retrait.text
    session = _session_de(db, c)

    resultat = _fermer(client, c, session, session.fonds_initial + 7_000)

    contexte = _arretes(db)[0].contexte
    assert contexte["entrees_affiche"].replace("\xa0", " ") == "10 000 F"
    assert contexte["sorties_affiche"].replace("\xa0", " ") == "3 000 F"
    # Aucune divergence avec l'écran : le solde théorique de l'arrêté est celui figé à la
    # fermeture, et il se retrouve par la même fonction de mouvements que l'écran.
    assert contexte["solde_theorique"] == resultat["solde_theorique_cloture"]
    entrees, sorties = calculer_mouvements(db, session, jusqu_a=session.closed_at)
    assert (entrees, sorties) == (10_000, 3_000)
    assert session.fonds_initial + entrees - sorties == contexte["solde_theorique"]
    assert (
        calculer_solde_theorique(db, session, jusqu_a=session.closed_at)
        == (contexte["solde_theorique"])
    )


def test_un_rejeu_ne_cree_ni_doublon_ni_nouveau_numero(db: Session, client: TestClient) -> None:
    c = Cadre(db, "AR4")
    session = _session_de(db, c)
    _fermer(client, c, session, session.fonds_initial)
    premier = _arretes(db)[0]
    rang = db.execute(select(func.max(Numerotation.last_value))).scalar_one()

    rejeu = arretes.archiver_arrete(db, _vers_schema(db, session))

    assert rejeu is not None and rejeu.id == premier.id
    assert len(_arretes(db)) == 1
    assert db.execute(select(func.max(Numerotation.last_value))).scalar_one() == rang


def test_chaque_session_a_son_numero_dans_l_ordre(db: Session, client: TestClient) -> None:
    premier, second = Cadre(db, "AR5"), Cadre(db, "AR6")
    for c in (premier, second):
        session = _session_de(db, c)
        _fermer(client, c, session, session.fonds_initial)

    numeros = sorted(a.numero_piece or "" for a in _arretes(db))
    rangs = [int(FORMAT_NUMERO.match(n).group(2)) for n in numeros]  # type: ignore[union-attr]
    assert rangs == [rangs[0], rangs[0] + 1]


def test_un_echec_de_rendu_ne_bloque_pas_la_fermeture_et_se_regenere_au_meme_numero(
    db: Session, client: TestClient, monkeypatch: MonkeyPatch
) -> None:
    c = Cadre(db, "AR7")
    session = _session_de(db, c)

    def _echec(*_: object, **__: object) -> bytes:
        raise RuntimeError("Pango indisponible")

    with monkeypatch.context() as m:
        m.setattr(service, "rendre_pdfa", _echec)
        resultat = _fermer(client, c, session, session.fonds_initial)  # la fermeture réussit

    assert db.get(CaisseSession, session.id).status == "fermee"  # type: ignore[union-attr]
    echec = _arretes(db)[0]
    assert echec.statut == "echec" and echec.numero_piece is not None
    assert resultat["arrete"]["statut"] == "echec"  # type: ignore[index]

    responsable = _utilisateur(db, c.agence, "RESPONSABLE_AGENCE")[1]
    reprise = client.post(f"/documents/{echec.id}/regenerer", headers=responsable)

    assert reprise.status_code == 200, reprise.text
    ok = db.get(Archive, uuid.UUID(reprise.json()["id"]))
    assert ok is not None and ok.statut == "ok"
    assert ok.numero_piece == echec.numero_piece  # même numéro : aucun n'a été consommé en plus
    assert db.execute(select(func.max(Numerotation.last_value))).scalar_one() == int(
        FORMAT_NUMERO.match(echec.numero_piece).group(2)  # type: ignore[union-attr]
    )


def test_le_caissier_voit_son_arrete_et_un_autre_agent_non(db: Session, client: TestClient) -> None:
    c = Cadre(db, "AR8")
    session = _session_de(db, c)
    resultat = _fermer(client, c, session, session.fonds_initial)
    document_id = resultat["arrete"]["id"]  # type: ignore[index]

    assert client.get(f"/documents/{document_id}/fichier", headers=c.entete).status_code == 200
    autre_agence = Cadre(db, "AR9")
    assert (
        client.get(f"/documents/{document_id}/fichier", headers=autre_agence.entete).status_code
        == 404
    )
    comptable = _utilisateur(db, c.agence, "RESPONSABLE_AGENCE")[1]
    assert client.get(f"/documents/{document_id}/fichier", headers=comptable).status_code == 200
