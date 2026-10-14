"""Lot 2 des documents : les reçus de caisse / épargne / dépôt à terme et le bon DAT sont générés
automatiquement APRÈS le commit de chaque opération, archivés et indexés ; le résultat de
l'opération porte la référence du document ; l'accès est restreint (un caissier ne voit que SES
documents, un rôle `documents.read.agence` voit son périmètre).

Tout passe par l'API de guichet : c'est elle qui archive (les services ne génèrent rien).
"""

import hashlib
import io
import uuid
from collections.abc import Generator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import engine, get_db
from app.main import app
from app.modules.caisse.models import Poste, PosteAssignation
from app.modules.caisse.service import ouvrir_session
from app.modules.comptabilite import journee
from app.modules.documents import recus, service
from app.modules.documents.models import STATUT_ECHEC, STATUT_OK, Archive
from app.modules.epargne import service as epargne_service
from app.modules.epargne.guichet import ResultatDenouement
from app.modules.epargne.models import Product, SavingsAccount
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.security.jwt import creer_access_token, decoder_access_token
from app.modules.security.models import Role, User, UserRole
from app.modules.security.password import hasher_mot_de_passe

pytestmark = pytest.mark.integration

MONTANT_DAT = 1_000_000


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


def _cid(db: Session, numero: str) -> uuid.UUID:
    return db.execute(
        text("SELECT id FROM comptabilite.accounts WHERE account_number = :n"), {"n": numero}
    ).scalar_one()


def _agence(db: Session, code: str) -> Agency:
    agence = Agency(code=code, name=f"Agence {code}", compte_caisse_id=_cid(db, "101111"))
    db.add(agence)
    db.flush()
    return agence


def _membre(db: Session, agence: Agency) -> uuid.UUID:
    tier_id = db.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES (:n, 'individual', :a, 'actif') RETURNING id"
        ),
        {"n": f"M-DR-{uuid.uuid4().hex[:8]}", "a": agence.id},
    ).scalar_one()
    nationalite = db.execute(text("SELECT id FROM parameters.countries LIMIT 1")).scalar_one()
    db.execute(
        text(
            "INSERT INTO tiers.individual_profiles "
            "(tier_id, last_name, first_name, birth_date, gender, nationality_id) "
            "VALUES (:t, 'Traoré', 'Fatoumata', '1990-01-01', 'F', :n)"
        ),
        {"t": tier_id, "n": nationalite},
    )
    return tier_id


def _compte_a_vue(db: Session, agence: Agency) -> SavingsAccount:
    produit = Product(
        code=f"PR{uuid.uuid4().hex[:5]}", name="Épargne à vue", type="a_vue",
        compte_epargne_id=_cid(db, "251111"),
    )
    db.add(produit)
    db.flush()
    return epargne_service.ouvrir_compte(
        db, tier_id=_membre(db, agence), product_id=produit.id, agency_id=agence.id, par=None
    )


def _produit_dat(db: Session) -> Product:
    produit = Product(
        code=f"DT{uuid.uuid4().hex[:6]}", name="Dépôt à terme", type="terme",
        compte_epargne_id=_cid(db, "252111"), compte_charge_interet_id=_cid(db, "60252"),
        taux_bp=500, base_jours=365, regle_arrondi="plus_proche",
        methode_calcul_solde="fin_periode", duree_min_mois=3, duree_max_mois=24,
        penalite_anticipee_bp=5000,
    )
    db.add(produit)
    db.flush()
    return produit


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


def _session_caisse(db: Session, agence: Agency, entete: dict[str, str], code_poste: str) -> None:
    user_id = decoder_access_token(entete["Authorization"].removeprefix("Bearer ")).sub
    poste = Poste(
        agency_id=agence.id, code=code_poste, libelle="Caisse",
        compte_caisse_id=agence.compte_caisse_id,
    )
    db.add(poste)
    db.flush()
    db.add(PosteAssignation(poste_id=poste.id, user_id=user_id))
    db.flush()
    ouvrir_session(
        db,
        UtilisateurCourant(
            user_id=user_id, roles=(), permissions=frozenset(),
            primary_agency_id=agence.id, agency_id=agence.id, voit_tout=True,
        ),
        poste_id=poste.id, fonds_initial=0,
    )


class Cadre:
    """Une agence, un membre, un compte à vue, un caissier avec sa session de caisse."""

    def __init__(self, db: Session, code: str) -> None:
        self.db = db
        self.agence = _agence(db, code)
        self.compte = _compte_a_vue(db, self.agence)
        self.caissier, self.entete = _utilisateur(db, self.agence, "CAISSIER")
        _session_caisse(db, self.agence, self.entete, "01")
        jour = journee.date_comptable_courante(db)
        assert jour is not None
        self.journee: date = jour

    def deposer(self, client: TestClient, montant: int = 10_000) -> dict[str, object]:
        reponse = client.post(
            f"/epargne/comptes/{self.compte.id}/depot", json={"montant": montant},
            headers=self.entete,
        )
        assert reponse.status_code == 200, reponse.text
        return dict(reponse.json())


def _archive(db: Session, document_id: str) -> Archive:
    archive = db.get(Archive, uuid.UUID(document_id))
    assert archive is not None
    return archive


def _pdf(archive: Archive) -> PdfReader:
    return PdfReader(settings.repertoire_documents / archive.chemin_relatif)


def _texte(archive: Archive) -> str:
    lecteur = _pdf(archive)
    return " ".join(page.extract_text() for page in lecteur.pages).replace("\xa0", " ")


# --- Versement et retrait -----------------------------------------------------------------------


def test_le_versement_archive_son_recu_et_le_resultat_porte_la_reference(
    db: Session, client: TestClient
) -> None:
    c = Cadre(db, "RC1")

    corps = c.deposer(client)

    reference = corps["document"]
    assert isinstance(reference, dict) and reference["statut"] == "ok"
    archive = _archive(db, str(reference["id"]))
    piece = str(corps["entry_number"])
    # Index : type, module, pièce, journée comptable (jamais l'horloge), agence, acteur, empreinte.
    assert (archive.type, archive.module, archive.statut) == ("recu_versement", "caisse", STATUT_OK)
    assert archive.numero_piece == piece and archive.journee_comptable == c.journee
    assert (archive.agency_id, archive.acteur_id) == (c.agence.id, c.caissier.id)
    assert archive.objet_lie_id == c.compte.id
    attendu = (
        f"RC1/{c.journee.year:04d}/{c.journee.month:02d}/{c.journee.day:02d}/caisse/"
        f"recu_versement_{c.compte.account_number}_{piece}.pdf"
    )
    assert archive.chemin_relatif == attendu
    assert reference["nom"] == attendu.rsplit("/", 1)[1]
    fichier = settings.repertoire_documents / archive.chemin_relatif
    assert archive.sha256 == hashlib.sha256(fichier.read_bytes()).hexdigest()
    assert fichier.read_bytes().startswith(b"%PDF")
    # Contenu : les données de l'opération, en lettres comprises, sur UNE A4.
    texte = _texte(archive)
    assert len(_pdf(archive).pages) == 1
    for attendu_texte in (
        "Reçu de versement", f"Reçu n° {piece}", "Traoré Fatoumata", c.compte.account_number,
        "Souley Awa", "10 000 F", "dix mille francs CFA", "Signature du client (facultative)",
    ):
        assert attendu_texte in texte, attendu_texte


def test_le_retrait_archive_son_recu_signature_du_client_obligatoire(
    db: Session, client: TestClient
) -> None:
    c = Cadre(db, "RC2")
    c.deposer(client, 20_000)

    reponse = client.post(
        f"/epargne/comptes/{c.compte.id}/retrait", json={"montant": 5_000}, headers=c.entete
    )

    assert reponse.status_code == 200, reponse.text
    reference = reponse.json()["document"]
    archive = _archive(db, reference["id"])
    assert (archive.type, archive.module) == ("recu_retrait", "caisse")
    texte = _texte(archive)
    assert "Reçu de retrait" in texte and "Montant retiré" in texte
    assert "cinq mille francs CFA" in texte
    assert "15 000 F" in texte  # nouveau solde
    assert "Signature du client (obligatoire)" in texte
    assert len(_pdf(archive).pages) == 1


def test_un_refus_n_archive_aucun_recu(db: Session, client: TestClient) -> None:
    c = Cadre(db, "RC3")
    avant = db.execute(select(Archive.id)).all()

    reponse = client.post(
        f"/epargne/comptes/{c.compte.id}/retrait", json={"montant": 999_999}, headers=c.entete
    )

    assert reponse.status_code == 422
    assert db.execute(select(Archive.id)).all() == avant  # rien n'est archivé pour un refus


# --- Dépôt à terme : placement (reçu + bon), anticipé, dénouement -----------------------------


def _placer(db: Session, client: TestClient, code: str) -> tuple[dict[str, object], Cadre]:
    c = Cadre(db, code)
    # Le placement se fait au guichet par le CAISSIER (permission dédiée), avec SA session.
    produit = _produit_dat(db)
    reponse = client.post(
        f"/tiers/{c.compte.tier_id}/depots-a-terme",
        json={"product_id": str(produit.id), "term_months": 12, "montant": MONTANT_DAT},
        headers=c.entete,
    )
    assert reponse.status_code == 201, reponse.text
    return dict(reponse.json()), c


def test_le_placement_dat_produit_le_recu_et_le_bon(db: Session, client: TestClient) -> None:
    corps, c = _placer(db, client, "RC4")

    recu = _archive(db, corps["document"]["id"])  # type: ignore[index]
    bon = _archive(db, corps["bon"]["id"])  # type: ignore[index]
    numero_dat = corps["compte"]["account_number"]  # type: ignore[index]
    assert (recu.type, recu.module) == ("recu_placement_dat", "epargne")
    assert (bon.type, bon.module) == ("bon_dat", "epargne")
    assert bon.chemin_relatif.endswith(f"/epargne/bon_dat_{numero_dat}.pdf")
    assert recu.chemin_relatif.endswith(
        f"/epargne/recu_placement_dat_{numero_dat}_{corps['entry_number']}.pdf"
    )
    assert bon.journee_comptable == recu.journee_comptable == c.journee
    texte_recu, texte_bon = _texte(recu), _texte(bon)
    assert "Reçu de placement" in texte_recu and "Capital encaissé" in texte_recu
    assert "un million de francs CFA" in texte_recu
    assert "12 mois" in texte_recu
    assert "Signature du client (facultative)" in texte_recu
    # Le bon = le contrat : taux et pénalité FIGÉS au contrat, signature obligatoire.
    for attendu in ("Bon de dépôt à terme", f"Bon n° {numero_dat}", "annuel", "5 %",
                    "50 % des intérêts courus", "Capital placé",
                    "Signature du client (obligatoire)",
                    "le capital est toujours restitué en totalité"):
        assert attendu in texte_bon, attendu
    assert len(_pdf(recu).pages) == 1 and len(_pdf(bon).pages) == 1  # les deux exemplaires / A4


def test_le_retrait_anticipe_archive_son_recu_avec_le_detail_du_calcul(
    db: Session, client: TestClient
) -> None:
    corps, c = _placer(db, client, "RC5")
    responsable, entete_resp = _utilisateur(db, c.agence, "RESPONSABLE_AGENCE")
    _session_caisse(db, c.agence, entete_resp, "02")

    reponse = client.post(
        f"/epargne/comptes/{corps['compte']['id']}/retrait-anticipe",  # type: ignore[index]
        headers=entete_resp,
    )

    assert reponse.status_code == 200, reponse.text
    reference = reponse.json()["document"]
    archive = _archive(db, reference["id"])
    assert (archive.type, archive.module) == ("recu_retrait_anticipe_dat", "epargne")
    assert archive.acteur_id == responsable.id
    texte = _texte(archive)
    assert "Reçu de retrait anticipé" in texte and "Total remis au client" in texte
    assert "Intérêts courus" in texte and "Intérêts versés" in texte
    assert "Signature du client (obligatoire)" in texte
    assert len(_pdf(archive).pages) == 1


def test_le_recu_de_denouement_a_l_echeance(db: Session, client: TestClient) -> None:
    """Le dénouement exige d'atteindre l'échéance (journées successives, testé ailleurs) : ici on
    éprouve la CONSTRUCTION du reçu à partir d'un résultat de dénouement."""
    corps, c = _placer(db, client, "RC6")
    agence = c.agence
    compte_id = uuid.UUID(corps["compte"]["id"])  # type: ignore[index]
    resultat = ResultatDenouement(
        account_number=str(corps["compte"]["account_number"]),  # type: ignore[index]
        mode="echeance", capital=MONTANT_DAT, interets_courus=50_000, penalite=0,
        interets_nets=50_000, total_paye=1_050_000, entry_number="CA-2027-000900",
        message="Dénouement à l'échéance", date_comptable=date(2027, 10, 14),
    )
    courant = UtilisateurCourant(
        user_id=c.caissier.id, roles=(), permissions=frozenset(),
        primary_agency_id=agence.id, agency_id=agence.id, voit_tout=True,
    )

    archive = recus.archiver_solde_dat(db, courant, compte_id, resultat)

    assert archive is not None and archive.statut == STATUT_OK
    assert (archive.type, archive.numero_piece) == ("recu_denouement_dat", "CA-2027-000900")
    assert archive.journee_comptable == date(2027, 10, 14)  # la journée de la pièce
    assert "/2027/10/14/epargne/recu_denouement_dat_" in archive.chemin_relatif
    texte = _texte(archive)
    assert "Reçu de dénouement" in texte and "Capital" in texte and "Intérêts" in texte
    assert "1 050 000 F" in texte and "un million cinquante mille francs CFA" in texte
    assert "Pénalité" not in texte  # pas de pénalité à l'échéance
    assert "Signature du client (obligatoire)" in texte
    assert len(_pdf(archive).pages) == 1


# --- Accès : un caissier ne voit QUE ses documents ----------------------------------------------


def test_un_caissier_ne_voit_que_ses_propres_documents(db: Session, client: TestClient) -> None:
    c = Cadre(db, "RC7")
    mien = c.deposer(client)["document"]
    collegue, entete_collegue = _utilisateur(db, c.agence, "CAISSIER")  # même agence
    assert collegue.id != c.caissier.id

    chez_le_collegue = client.get("/documents", headers=entete_collegue).json()
    assert chez_le_collegue["total"] == 0
    identifiant = str(mien["id"])  # type: ignore[index]
    chez = entete_collegue
    assert client.get(f"/documents/{identifiant}/fichier", headers=chez).status_code == 404
    assert client.post(f"/documents/{identifiant}/regenerer", headers=chez).status_code == 404
    chez_moi = client.get("/documents", headers=c.entete).json()
    assert [ligne["id"] for ligne in chez_moi["lignes"]] == [mien["id"]]  # type: ignore[index]
    assert client.get(f"/documents/{mien['id']}/fichier", headers=c.entete).status_code == 200  # type: ignore[index]


def test_un_responsable_voit_les_documents_de_son_agence_pas_ceux_d_une_autre(
    db: Session, client: TestClient
) -> None:
    c = Cadre(db, "RC8A")
    mien = c.deposer(client)["document"]
    _, responsable = _utilisateur(db, c.agence, "RESPONSABLE_AGENCE")
    autre_agence = _agence(db, "RC8B")
    _, responsable_ailleurs = _utilisateur(db, autre_agence, "RESPONSABLE_AGENCE")

    chez_lui = client.get("/documents", headers=responsable).json()
    assert [ligne["id"] for ligne in chez_lui["lignes"]] == [mien["id"]]  # type: ignore[index]
    assert client.get(f"/documents/{mien['id']}/fichier", headers=responsable).status_code == 200  # type: ignore[index]
    assert client.get("/documents", headers=responsable_ailleurs).json()["total"] == 0
    ailleurs = f"/documents/{mien['id']}/fichier"  # type: ignore[index]
    assert client.get(ailleurs, headers=responsable_ailleurs).status_code == 404


def test_un_role_reseau_voit_toutes_les_agences(db: Session, client: TestClient) -> None:
    a, b = Cadre(db, "RC9A"), Cadre(db, "RC9B")
    a.deposer(client)
    b.deposer(client)
    _, direction = _utilisateur(db, a.agence, "DIRECTION_GENERALE")

    liste = client.get("/documents", headers=direction, params={"journee": a.journee.isoformat()})

    agences = {ligne["agency_id"] for ligne in liste.json()["lignes"]}
    assert {str(a.agence.id), str(b.agence.id)} <= agences


def test_le_telechargement_d_un_recu_est_audite(db: Session, client: TestClient) -> None:
    c = Cadre(db, "RC10")
    reference = c.deposer(client)["document"]

    reponse = client.get(f"/documents/{reference['id']}/fichier", headers=c.entete)  # type: ignore[index]

    assert reponse.status_code == 200 and reponse.headers["content-type"] == "application/pdf"
    audit = db.execute(
        text(
            "SELECT user_id FROM audit.audit_logs WHERE action = 'documents.telecharge' "
            "AND resource_id = :r"
        ),
        {"r": uuid.UUID(reference["id"])},  # type: ignore[index]
    ).all()
    assert [ligne[0] for ligne in audit] == [c.caissier.id]
    lecteur = PdfReader(io.BytesIO(reponse.content))
    assert len(lecteur.pages) == 1


# --- Robustesse : la génération n'est jamais un obstacle au guichet ---------------------------


def test_un_echec_de_generation_ne_bloque_pas_l_operation_et_se_regenere(
    db: Session, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = Cadre(db, "RC11")

    def casse(*_args: object, **_kwargs: object) -> bytes:
        raise OSError("bibliothèque native Pango introuvable")

    with monkeypatch.context() as panne:
        panne.setattr(service, "rendre_pdfa", casse)
        corps = c.deposer(client, 10_000)  # 200 : le dépôt est acquis malgré la panne

    assert corps["nouveau_solde"] == 10_000
    reference = corps["document"]
    assert isinstance(reference, dict) and reference["statut"] == "echec"  # régénérable
    assert db.get(SavingsAccount, c.compte.id) is not None
    db.refresh(c.compte)
    assert c.compte.balance == 10_000  # l'opération n'a pas été défaite

    reprise = client.post(f"/documents/{reference['id']}/regenerer", headers=c.entete)

    assert reprise.status_code == 200, reprise.text
    assert reprise.json()["statut"] == "ok"
    archive = _archive(db, reprise.json()["id"])
    assert archive.statut == STATUT_OK and archive.acteur_id == c.caissier.id  # propriétaire gardé
    assert client.get(f"/documents/{archive.id}/fichier", headers=c.entete).status_code == 200
    assert _archive(db, reference["id"]).statut == STATUT_ECHEC  # l'historique est conservé


def test_une_erreur_de_construction_rend_none_sans_lever(db: Session) -> None:
    c = Cadre(db, "RC12")
    courant = UtilisateurCourant(
        user_id=c.caissier.id, roles=(), permissions=frozenset(),
        primary_agency_id=c.agence.id, agency_id=c.agence.id, voit_tout=True,
    )

    from app.modules.epargne.guichet import ResultatOperation

    resultat = ResultatOperation("X", 0, "CA-1", date(2024, 3, 5))
    # Compte inexistant : aucune exception ne remonte au guichet, juste pas de document.
    assert recus.archiver_recu_operation(
        db, courant, uuid.uuid4(), resultat, versement=True, montant=1
    ) is None
    assert recus.reference_de(None) is None
