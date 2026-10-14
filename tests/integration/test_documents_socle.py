"""Socle d'archivage des documents (lot 1) : rendu PDF/A, stockage daté par la journée comptable,
index immuable, génération après commit, API cloisonnée et auditée.

Prouvé avec un type de document d'ESSAI (`essai`) : les vrais documents viennent au lot suivant.
Chaque test écrit dans un dossier d'archive jetable (`tmp_path`), jamais dans le dossier de dev.
"""

import hashlib
import io
import os
import uuid
from collections.abc import Generator
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, settings
from app.core.database import engine, get_db
from app.main import app
from app.modules.documents import rendu, service, stockage
from app.modules.documents.models import STATUT_ECHEC, STATUT_OK, Archive
from app.modules.documents.service import DemandeDocument
from app.modules.parameters.models import Agency
from app.modules.security.jwt import creer_access_token
from app.modules.security.models import Role, User, UserRole
from app.modules.security.password import hasher_mot_de_passe

pytestmark = pytest.mark.integration

JOURNEE = date(2024, 3, 5)  # volontairement éloignée de la date du jour


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


@pytest.fixture(autouse=True)
def archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Dossier d'archive jetable : l'archive de dev n'est jamais touchée."""
    monkeypatch.setattr(settings, "DOCUMENTS_REPERTOIRE", str(tmp_path))
    return tmp_path


def _agence(db: Session, code: str) -> Agency:
    agence = Agency(code=code, name=f"Agence {code}")
    db.add(agence)
    db.flush()
    return agence


def _utilisateur(db: Session, agence: Agency, role_code: str) -> tuple[User, dict[str, str]]:
    role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
    suffixe = uuid.uuid4().hex[:8]
    user = User(
        matricule=f"MAT-{suffixe}", email=f"{suffixe}@ex.com", username=f"u{suffixe}",
        password_hash=hasher_mot_de_passe("Motdepasse!123"), last_name="Souley", first_name="Awa",
        primary_agency_id=agence.id,
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.flush()
    jeton = creer_access_token(
        user_id=user.id, roles=[role_code], primary_agency_id=agence.id, agency_id=agence.id
    )
    return user, {"Authorization": f"Bearer {jeton}"}


def _demande(
    agence: Agency, *, cle: str = "essai-1", acteur: uuid.UUID | None = None,
    journee: date = JOURNEE,
) -> DemandeDocument:
    return DemandeDocument(
        type="essai",
        module="essai",
        cle_naturelle=cle,
        journee=journee,
        agency_id=agence.id,
        nom=f"recu_essai_{cle}",
        numero_piece="CA-2024-000001",
        objet_lie_type="essai.objet",
        objet_lie_id=uuid.uuid4(),
        acteur_id=acteur,
        contexte={
            "titre": "Reçu de versement (essai)",
            "reference": "ESSAI-0001",
            "signature_client_requise": False,
            "lignes": [
                {"libelle": "Membre", "valeur": "Traoré Fatoumata"},
                {"libelle": "Compte", "valeur": "EP-2024-0000001"},
            ],
            "libelle_montant": "Montant versé",
            "montant": 2_000_000,
            "montant_affiche": "2 000 000 F",
        },
    )


def _sha(chemin: Path) -> str:
    return hashlib.sha256(chemin.read_bytes()).hexdigest()


# --- Rendu PDF/A ------------------------------------------------------------------------------


def test_le_pdf_est_valide_non_vide_et_pdfa(db: Session) -> None:
    agence = _agence(db, "DOC1")
    document = service.archiver(db, _demande(agence))

    contenu = (settings.repertoire_documents / document.chemin_relatif).read_bytes()

    assert contenu.startswith(b"%PDF") and len(contenu) > 1_000
    lecteur = PdfReader(io.BytesIO(contenu))
    assert len(lecteur.pages) >= 1
    racine = lecteur.trailer["/Root"]
    # PDF/A-2b : identification dans le XMP + profil colorimétrique de sortie embarqué.
    xmp = racine["/Metadata"].get_object().get_data().decode("utf-8", "replace")
    assert 'pdfaid:part="2"' in xmp and 'pdfaid:conformance="B"' in xmp
    assert racine["/OutputIntents"][0]["/S"] == "/GTS_PDFA1"
    texte = lecteur.pages[0].extract_text()
    assert "Traoré Fatoumata" in texte
    assert "deux millions de francs CFA" in texte  # montant en lettres porté en Python
    # (le style met les intitulés en capitales : l'extraction restitue ce que le papier montre)
    assert "EXEMPLAIRE CLIENT" in texte and "EXEMPLAIRE SOUCHE" in texte
    assert texte.count("découper ici") == 1  # un seul trait, entre les deux exemplaires
    assert len(lecteur.pages) == 1  # les deux exemplaires tiennent sur UNE A4


def test_aucune_horloge_dans_le_pdf_la_date_est_la_journee_comptable(db: Session) -> None:
    agence = _agence(db, "DOC2")
    document = service.archiver(db, _demande(agence))
    lecteur = PdfReader(settings.repertoire_documents / document.chemin_relatif)

    assert "05/03/2024" in lecteur.pages[0].extract_text()
    assert lecteur.metadata is not None
    assert lecteur.metadata["/CreationDate"] == "D:20240305"  # la journée, pas l'heure du poste
    # L'horloge de la base n'apparaît nulle part : ni sa date, ni son heure.
    aujourd_hui = db.execute(text("SELECT CURRENT_DATE")).scalar_one()
    assert aujourd_hui != JOURNEE
    assert f"{aujourd_hui.day:02d}/{aujourd_hui.month:02d}/{aujourd_hui.year}" not in (
        lecteur.pages[0].extract_text()
    )


def test_le_rendu_embarque_ses_polices_sans_police_generique(db: Session) -> None:
    """Polices LIBRES embarquées (DejaVu, chasse fixe comprise) : jamais une police du système.
    Le rendu n'est PAS garanti identique octet pour octet d'une exécution à l'autre sur Linux
    (étiquettes de sous-ensembles de polices) : l'idempotence repose sur l'index et le nom
    déterministe, jamais sur un second rendu identique."""
    agence = _agence(db, "DOC3")
    contexte = {**_demande(agence).contexte, "institution": service._institution(),
                "agence": {"code": agence.code, "nom": agence.name}}

    premier = rendu.rendre_pdfa("essai.html.j2", contexte, JOURNEE)

    lecteur = PdfReader(io.BytesIO(premier))
    polices = {
        str(police.get_object().get("/BaseFont"))
        for page in lecteur.pages
        for police in page["/Resources"].get("/Font", {}).values()
    }
    assert polices and all("DejaVu" in nom for nom in polices)  # embarquées, aucune générique


def test_le_bandeau_provisoire_suit_la_config_de_l_institution(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    agence = _agence(db, "DOC4")
    monkeypatch.setattr(settings, "IMF_NOM", "")
    sans_nom = service.archiver(db, _demande(agence, cle="sans-nom"))
    monkeypatch.setattr(settings, "IMF_NOM", "IMCEC Test")
    avec_nom = service.archiver(db, _demande(agence, cle="avec-nom"))

    base = settings.repertoire_documents
    texte_sans = PdfReader(base / sans_nom.chemin_relatif).pages[0].extract_text()
    texte_avec = PdfReader(base / avec_nom.chemin_relatif).pages[0].extract_text()
    assert "Document provisoire" in texte_sans
    assert "Document provisoire" not in texte_avec and "IMCEC Test" in texte_avec


# --- Stockage : chemin daté par la journée comptable, noms assainis -----------------------------


def test_le_chemin_suit_la_journee_comptable_pas_l_horloge(db: Session, archive: Path) -> None:
    agence = _agence(db, "DOC5")
    document = service.archiver(db, _demande(agence))

    assert document.chemin_relatif == "DOC5/2024/03/05/essai/recu_essai_essai-1.pdf"
    assert (archive / "DOC5" / "2024" / "03" / "05" / "essai" / "recu_essai_essai-1.pdf").is_file()
    aujourd_hui = db.execute(text("SELECT CURRENT_DATE")).scalar_one()
    assert aujourd_hui != JOURNEE and f"/{aujourd_hui.year:04d}/" not in document.chemin_relatif
    assert document.journee_comptable == JOURNEE


@pytest.mark.parametrize(
    ("brut", "attendu"),
    [
        ("EP-2026-0000001", "EP-2026-0000001"),
        ("reçu de l'été", "re_u_de_l__t_"),
        ("a b/c\\d", "a_b_c_d"),
        ("../../etc/passwd", "____etc_passwd"),
        ("..cache", "_cache"),
    ],
)
def test_les_composants_sont_assainis(brut: str, attendu: str) -> None:
    assert stockage.assainir(brut) == attendu


@pytest.mark.parametrize("vide", ["", "   ", "...", "///"])
def test_un_composant_vide_est_refuse(vide: str) -> None:
    with pytest.raises(stockage.ComposantInvalideError):
        stockage.assainir(vide)


def test_aucune_evasion_du_dossier_d_archive(archive: Path) -> None:
    with pytest.raises(stockage.ComposantInvalideError):
        stockage.chemin_absolu(archive, "../dehors.pdf")


# --- Index : écrit, fidèle, immuable ----------------------------------------------------------


def test_l_index_est_ecrit_et_son_empreinte_est_celle_du_fichier(
    db: Session, archive: Path
) -> None:
    agence = _agence(db, "DOC6")
    acteur, _ = _utilisateur(db, agence, "CAISSIER")

    document = service.archiver(db, _demande(agence, acteur=acteur.id))

    ligne = db.get(Archive, document.id)
    assert ligne is not None
    fichier = archive / ligne.chemin_relatif
    assert ligne.statut == STATUT_OK
    assert ligne.sha256 == _sha(fichier)
    assert ligne.taille == fichier.stat().st_size
    assert (ligne.type, ligne.module, ligne.numero_piece) == ("essai", "essai", "CA-2024-000001")
    assert (ligne.agency_id, ligne.acteur_id) == (agence.id, acteur.id)
    assert ligne.journee_comptable == JOURNEE
    assert ligne.contexte["montant"] == 2_000_000  # les données imprimées sont conservées
    assert ligne.cree_le is not None  # horodatage réel d'audit, distinct de la journée


def test_aucune_reecriture_un_second_appel_rend_le_meme_document(
    db: Session, archive: Path
) -> None:
    agence = _agence(db, "DOC7")
    premier = service.archiver(db, _demande(agence))
    fichier = archive / premier.chemin_relatif
    avant = (_sha(fichier), fichier.stat().st_mtime_ns)

    second = service.archiver(db, _demande(agence))
    db.flush()

    assert second.id == premier.id
    assert (_sha(fichier), fichier.stat().st_mtime_ns) == avant  # fichier intact
    nombre = db.execute(
        select(Archive).where(Archive.type == "essai", Archive.cle_naturelle == "essai-1")
    ).scalars().all()
    assert len(nombre) == 1


def test_l_ecriture_ne_remplace_jamais_un_fichier_existant(archive: Path) -> None:
    stockage.ecrire_sans_ecraser(archive, "A/2024/03/05/essai/x.pdf", b"original")

    with pytest.raises(stockage.FichierExistantError):
        stockage.ecrire_sans_ecraser(archive, "A/2024/03/05/essai/x.pdf", b"autre contenu")

    assert (archive / "A/2024/03/05/essai/x.pdf").read_bytes() == b"original"
    assert [p.name for p in (archive / "A/2024/03/05/essai").iterdir()] == ["x.pdf"]  # pas de .tmp


def test_un_fichier_orphelin_est_adopte_sans_etre_ecrase(db: Session, archive: Path) -> None:
    """Arrêt entre l'écriture et l'index : le fichier existe, pas la ligne. On l'indexe tel quel."""
    agence = _agence(db, "DOC8")
    stockage.ecrire_sans_ecraser(
        archive, "DOC8/2024/03/05/essai/recu_essai_essai-1.pdf", b"%PDF-orphelin"
    )

    document = service.archiver(db, _demande(agence))

    fichier = archive / document.chemin_relatif
    assert fichier.read_bytes() == b"%PDF-orphelin"
    assert document.sha256 == hashlib.sha256(b"%PDF-orphelin").hexdigest()


def test_l_index_refuse_update_et_delete(db: Session) -> None:
    agence = _agence(db, "DOC9")
    document = service.archiver(db, _demande(agence))
    db.flush()

    with pytest.raises(DBAPIError, match="immuable"), db.begin_nested():
        db.execute(
            text("UPDATE documents.archives SET module = 'autre' WHERE id = :i"),
            {"i": document.id},
        )
    with pytest.raises(DBAPIError, match="immuable"), db.begin_nested():
        db.execute(text("DELETE FROM documents.archives WHERE id = :i"), {"i": document.id})


def test_deux_documents_ok_pour_la_meme_cle_sont_refuses_par_la_base(db: Session) -> None:
    agence = _agence(db, "DOC10")
    premier = service.archiver(db, _demande(agence))
    db.flush()

    with pytest.raises(IntegrityError, match="uq_documents_archives_cle_ok"), db.begin_nested():
        db.execute(
            text(
                "INSERT INTO documents.archives (type, module, cle_naturelle, "
                "journee_comptable, chemin_relatif, sha256, taille, agency_id, contexte, "
                "statut) VALUES (:t, 'essai', :c, :j, 'autre/chemin.pdf', :h, 10, :a, "
                "CAST('{}' AS jsonb), 'ok')"
            ),
            {"t": premier.type, "c": premier.cle_naturelle, "j": JOURNEE,
             "h": "0" * 64, "a": agence.id},
        )


# --- Génération après commit : un échec ne défait rien ------------------------------------------


def _faire_echouer_le_rendu(monkeypatch: pytest.MonkeyPatch) -> None:
    def casse(*_args: object, **_kwargs: object) -> bytes:
        raise OSError("bibliothèque native Pango introuvable")

    monkeypatch.setattr(service, "rendre_pdfa", casse)


def test_un_echec_de_generation_ne_defait_pas_l_operation_et_la_regeneration_rattrape(
    db: Session, client: TestClient, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agence = _agence(db, "DOC11")
    _, entete = _utilisateur(db, agence, "CAISSIER")
    # L'OPÉRATION MÉTIER (ici : un compte créé) est validée AVANT la génération.
    operation = _agence(db, "DOC11-OPERATION")
    db.commit()

    with monkeypatch.context() as panne:
        _faire_echouer_le_rendu(panne)
        resultat = service.generer_apres_commit(db, _demande(agence, cle="panne"))  # ne lève pas

    assert resultat.statut == STATUT_ECHEC and "Pango" in (resultat.erreur or "")
    assert db.get(Agency, operation.id) is not None  # l'opération est intacte
    assert service.trouver_ok(db, "essai", "panne") is None
    assert not any(archive.rglob("*.pdf"))  # aucun fichier produit

    reprise = client.post(f"/documents/{resultat.id}/regenerer", headers=entete)

    assert reprise.status_code == 200, reprise.text
    corps = reprise.json()
    assert corps["statut"] == "ok" and corps["id"] != str(resultat.id)  # NOUVELLE ligne
    assert (archive / "DOC11/2024/03/05/essai/recu_essai_panne.pdf").is_file()
    # Idempotent : une seconde régénération rend le même document, sans en créer un autre.
    encore = client.post(f"/documents/{resultat.id}/regenerer", headers=entete)
    assert encore.json()["id"] == corps["id"]
    # L'échec reste dans l'historique (index immuable).
    assert db.get(Archive, resultat.id) is not None


def test_seul_un_document_en_echec_se_regenere(db: Session, client: TestClient) -> None:
    agence = _agence(db, "DOC12")
    _, entete = _utilisateur(db, agence, "CAISSIER")
    document = service.archiver(db, _demande(agence))
    db.flush()

    reponse = client.post(f"/documents/{document.id}/regenerer", headers=entete)

    assert reponse.status_code == 422
    assert "génération a échoué" in reponse.json()["detail"]


# --- API : permission, périmètre, audit --------------------------------------------------------


def test_sans_la_permission_documents_read_403(db: Session, client: TestClient) -> None:
    agence = _agence(db, "DOC13")
    document = service.archiver(db, _demande(agence))
    _, sans_droit = _utilisateur(db, agence, "CHARGE_CLIENTELE")  # pas documents.read

    assert client.get("/documents", headers=sans_droit).status_code == 403
    assert client.get(f"/documents/{document.id}/fichier", headers=sans_droit).status_code == 403
    assert client.post(f"/documents/{document.id}/regenerer", headers=sans_droit).status_code == 403


def test_un_document_hors_agence_est_introuvable_404(db: Session, client: TestClient) -> None:
    agence_a, agence_b = _agence(db, "DOC14A"), _agence(db, "DOC14B")
    document = service.archiver(db, _demande(agence_a))
    _, chez_b = _utilisateur(db, agence_b, "CAISSIER")  # a documents.read, mais AUTRE agence

    assert client.get(f"/documents/{document.id}/fichier", headers=chez_b).status_code == 404
    assert client.post(f"/documents/{document.id}/regenerer", headers=chez_b).status_code == 404
    liste = client.get("/documents", headers=chez_b).json()
    assert liste["total"] == 0  # le document de A n'apparaît même pas dans la liste de B


def test_le_telechargement_rend_le_fichier_archive_et_il_est_audite(
    db: Session, client: TestClient, archive: Path
) -> None:
    agence = _agence(db, "DOC15")
    utilisateur, entete = _utilisateur(db, agence, "CAISSIER")
    document = service.archiver(db, _demande(agence))
    db.flush()

    reponse = client.get(f"/documents/{document.id}/fichier", headers=entete)

    assert reponse.status_code == 200
    assert reponse.headers["content-type"] == "application/pdf"
    assert 'filename="recu_essai_essai-1.pdf"' in reponse.headers["content-disposition"]
    assert reponse.content == (archive / document.chemin_relatif).read_bytes()  # tel qu'écrit
    audit = db.execute(
        text(
            "SELECT user_id FROM audit.audit_logs WHERE action = 'documents.telecharge' "
            "AND resource_id = :r"
        ),
        {"r": document.id},
    ).all()
    assert [ligne[0] for ligne in audit] == [utilisateur.id]  # UNE ligne par téléchargement


def test_un_fichier_altere_est_detecte_et_refuse(
    db: Session, client: TestClient, archive: Path
) -> None:
    agence = _agence(db, "DOC16")
    _, entete = _utilisateur(db, agence, "CAISSIER")
    document = service.archiver(db, _demande(agence))
    db.flush()
    fichier = archive / document.chemin_relatif
    os.chmod(fichier, 0o666)
    fichier.write_bytes(fichier.read_bytes() + b"\n%altere")

    reponse = client.get(f"/documents/{document.id}/fichier", headers=entete)

    assert reponse.status_code == 409
    assert "empreinte" in reponse.json()["detail"]


def test_la_liste_se_filtre_et_se_pagine_cote_serveur(db: Session, client: TestClient) -> None:
    agence = _agence(db, "DOC17")
    _, entete = _utilisateur(db, agence, "CAISSIER")
    for rang in range(3):
        service.archiver(db, _demande(agence, cle=f"liste-{rang}"))
    service.archiver(db, _demande(agence, cle="autre-jour", journee=date(2024, 3, 6)))
    db.flush()

    toutes = client.get("/documents", headers=entete, params={"taille": 2, "page": 1}).json()
    assert (toutes["total"], len(toutes["lignes"]), toutes["taille"]) == (4, 2, 2)
    seconde = client.get("/documents", headers=entete, params={"taille": 2, "page": 2}).json()
    assert len(seconde["lignes"]) == 2
    un_jour = client.get(
        "/documents", headers=entete, params={"journee": "2024-03-05", "module": "essai"}
    ).json()
    assert un_jour["total"] == 3
    assert {ligne["nom_fichier"] for ligne in un_jour["lignes"]} == {
        "recu_essai_liste-0.pdf", "recu_essai_liste-1.pdf", "recu_essai_liste-2.pdf",
    }
    piece = client.get("/documents", headers=entete, params={"piece": "CA-2024-000001"}).json()
    assert piece["total"] == 4
    assert "chemin" not in piece["lignes"][0]  # jamais le chemin du serveur
    assert client.get("/documents", headers=entete, params={"taille": 500}).status_code == 422


def test_un_role_reseau_voit_les_documents_de_toutes_les_agences(
    db: Session, client: TestClient
) -> None:
    agence_a, agence_b = _agence(db, "DOC18A"), _agence(db, "DOC18B")
    service.archiver(db, _demande(agence_a, cle="a"))
    service.archiver(db, _demande(agence_b, cle="b"))
    _, auditeur = _utilisateur(db, agence_a, "AUDITEUR_INTERNE")  # périmètre réseau
    db.flush()

    liste = client.get("/documents", headers=auditeur, params={"journee": "2024-03-05"}).json()

    assert {ligne["agency_id"] for ligne in liste["lignes"]} == {
        str(agence_a.id), str(agence_b.id),
    }


# --- Prêt / configuration ------------------------------------------------------------------------


def test_health_pret_ok_quand_le_dossier_d_archive_existe(client: TestClient) -> None:
    reponse = client.get("/health/ready")

    assert reponse.status_code == 200
    assert reponse.json() == {"status": "ok", "documents": "ok"}
    assert client.get("/health").json() == {"status": "ok"}  # « vivant » inchangé


def test_health_pas_pret_quand_le_dossier_d_archive_est_absent(
    client: TestClient, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "DOCUMENTS_REPERTOIRE", str(archive / "inexistant"))

    reponse = client.get("/health/ready")

    assert reponse.status_code == 503
    assert "n'existe pas" in reponse.json()["documents"]
    assert str(archive) not in reponse.text  # jamais le chemin du serveur sur une route publique
    assert client.get("/health").status_code == 200  # vivant, mais pas prêt


def test_le_repertoire_est_obligatoire_hors_dev_et_absolu() -> None:
    base = {"DATABASE_URL": "postgresql+psycopg://x:x@localhost/x", "JWT_SECRET": "x" * 40}
    with pytest.raises(ValueError, match="DOCUMENTS_REPERTOIRE est obligatoire"):
        Settings(**base, ENV="production", DOCUMENTS_REPERTOIRE=None)
    with pytest.raises(ValueError, match="chemin absolu"):
        Settings(**base, ENV="production", DOCUMENTS_REPERTOIRE="archives/relatif")
    dev = Settings(**base, ENV="dev", DOCUMENTS_REPERTOIRE="")  # « .env.example » : valeur vide
    assert dev.DOCUMENTS_REPERTOIRE is None
    assert dev.repertoire_documents.name == "archives_documents"
    prod = Settings(**base, ENV="production", DOCUMENTS_REPERTOIRE=str(Path.cwd()))
    assert prod.repertoire_documents == Path.cwd()
