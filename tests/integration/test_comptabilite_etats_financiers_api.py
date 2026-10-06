"""API — Bilan et compte de résultat RCSFD (chantier P1, dernier lot).

  - BILAN : `balance(date_debut=None, ...)` cumule TOUT l'historique — mêmes comptes et mêmes
    dates PASSÉES (1800-1820) que les tests à-nouveaux, pour la même raison exacte (éviter
    d'hériter les classes 6/7 jamais closes du "2026" réel de cette base partagée) ;
  - CONTRA_ACTIF vient en DÉDUCTION de l'actif, jamais au passif ;
  - comptes non mappés AVEC solde : signalés, jamais ignorés silencieusement ;
  - POINT NON ÉVIDENT, démontré explicitement : un bilan pris sur un exercice EN COURS (classes
    6/7 non soldées) ne s'équilibre PAS tant que le résultat courant n'a pas été clôturé (591) —
    ce n'est PAS un bug, c'est le résultat de la période qui n'est pas encore entré dans les
    capitaux propres. Après clôture (b1), le même bilan s'équilibre exactement ;
  - COMPTE DE RÉSULTAT : agrégation 6/7 sur la période si l'exercice est ouvert ; re-dérivé
    depuis la pièce de clôture (591) si l'exercice est clos, détail par poste alors vide ;
  - permissions : compta.rapport.read (bilan/résultat), compta.plan.manage (admin du mapping).
"""

import uuid
from collections.abc import Generator
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.cli.seed_financial_statement_mapping import executer_seed_mapping_etats
from app.core.database import engine, get_db
from app.main import app
from app.modules.comptabilite import ecritures
from app.modules.comptabilite.ecritures import LigneSaisie
from app.modules.comptabilite.models import (
    Account,
    Exercice,
    FinancialStatementMapping,
    Journal,
)
from app.modules.comptabilite.plan import importer
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


def _compte(db: Session, numero: str, *, normal_side: str, **overrides: object) -> Account:
    valeurs = {
        "account_number": numero,
        "name": f"Compte {numero}",
        "account_class": int(numero[0]),
        "normal_side": normal_side,
        "is_posting": True,
        "is_system": False,
        **overrides,
    }
    compte = Account(**valeurs)
    db.add(compte)
    db.flush()
    return compte


def _mapper(
    db: Session, compte: Account, *, etat: str, masse: str, poste: str, ordre: int = 10
) -> None:
    db.add(
        FinancialStatementMapping(
            account_id=compte.id, etat=etat, masse=masse, poste_libelle=poste, poste_ordre=ordre
        )
    )
    db.flush()


def _journal_id(db: Session, code: str) -> uuid.UUID:
    return db.execute(select(Journal.id).where(Journal.code == code)).scalar_one()


def _exercice(db: Session, code: str, debut: date, fin: date) -> Exercice:
    exercice = Exercice(code=code, label=f"Exercice {code}", date_debut=debut, date_fin=fin)
    db.add(exercice)
    db.flush()
    return exercice


def _valider_od(db: Session, lignes: list[LigneSaisie], entry_date: date) -> None:
    entry = ecritures.creer_brouillon(
        db, journal_id=_journal_id(db, "OD"), entry_date=entry_date,
        description="Mouvement de test", lignes=lignes, par=None,
    )
    ecritures.valider(db, entry, None)


def _cloturer(client: TestClient, headers: dict[str, str], exercice_id: str) -> dict:
    reponse = client.post(f"/comptabilite/exercices/{exercice_id}/cloture", headers=headers)
    assert reponse.status_code == 200, reponse.text
    return reponse.json()


# --- Bilan -----------------------------------------------------------------------------------


def test_bilan_equilibre_avec_comptes_de_bilan_purs(client: TestClient, db: Session) -> None:
    tresorerie = _compte(db, "1T18000", normal_side="D")
    capital = _compte(db, "5T18001", normal_side="C")
    _mapper(db, tresorerie, etat="BILAN", masse="ACTIF", poste="Tresorerie test")
    _mapper(db, capital, etat="BILAN", masse="PASSIF", poste="Capital test")
    _exercice(db, "ETATS-1800", date(1800, 1, 1), date(1800, 12, 31))
    _valider_od(
        db, [LigneSaisie(tresorerie.id, "D", 100000), LigneSaisie(capital.id, "C", 100000)],
        date(1800, 6, 1),
    )
    db.commit()
    comptable = _entete(db, "COMPTABLE")

    reponse = client.get(
        "/comptabilite/etats/bilan", params={"date": "1800-12-31"}, headers=comptable
    )

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["total_actif_net"] == 100000
    assert corps["total_passif"] == 100000
    assert corps["ecart"] == 0
    assert corps["equilibre"] is True
    assert corps["comptes_non_mappes"] == []


def test_bilan_contra_actif_deduit_de_lactif(client: TestClient, db: Session) -> None:
    credits_sains = _compte(db, "2T18010", normal_side="D")
    provision = _compte(db, "2T18011", normal_side="C")
    capital = _compte(db, "5T18012", normal_side="C")
    _mapper(db, credits_sains, etat="BILAN", masse="ACTIF", poste="Credits test")
    _mapper(db, provision, etat="BILAN", masse="CONTRA_ACTIF", poste="Provisions test")
    _mapper(db, capital, etat="BILAN", masse="PASSIF", poste="Capital test")
    _exercice(db, "ETATS-1801", date(1801, 1, 1), date(1801, 12, 31))
    # Crédit de 50 000 financé par capital, puis provisionné à hauteur de 8 000.
    _valider_od(
        db, [LigneSaisie(credits_sains.id, "D", 50000), LigneSaisie(capital.id, "C", 50000)],
        date(1801, 6, 1),
    )
    _valider_od(
        db, [LigneSaisie(capital.id, "D", 8000), LigneSaisie(provision.id, "C", 8000)],
        date(1801, 6, 2),
    )
    db.commit()
    comptable = _entete(db, "COMPTABLE")

    reponse = client.get(
        "/comptabilite/etats/bilan", params={"date": "1801-12-31"}, headers=comptable
    )

    corps = reponse.json()
    assert corps["total_actif_brut"] == 50000
    assert corps["total_contra_actif"] == 8000
    assert corps["total_actif_net"] == 42000
    assert corps["total_passif"] == 42000  # capital 50000 - 8000 débité
    assert corps["equilibre"] is True


def test_bilan_comptes_non_mappes_signales(client: TestClient, db: Session) -> None:
    tresorerie = _compte(db, "1T18020", normal_side="D")
    orphelin = _compte(db, "5T18021", normal_side="C")  # jamais mappé, volontairement
    _mapper(db, tresorerie, etat="BILAN", masse="ACTIF", poste="Tresorerie test")
    _exercice(db, "ETATS-1802", date(1802, 1, 1), date(1802, 12, 31))
    _valider_od(
        db, [LigneSaisie(tresorerie.id, "D", 3000), LigneSaisie(orphelin.id, "C", 3000)],
        date(1802, 6, 1),
    )
    db.commit()
    comptable = _entete(db, "COMPTABLE")

    reponse = client.get(
        "/comptabilite/etats/bilan", params={"date": "1802-12-31"}, headers=comptable
    )

    corps = reponse.json()
    non_mappes = {c["account_number"]: c for c in corps["comptes_non_mappes"]}
    assert "5T18021" in non_mappes
    assert non_mappes["5T18021"]["solde"] == 3000
    # Sans le compte orphelin en passif, l'actif seul (3000) ne s'équilibre plus à rien : signalé.
    assert corps["total_actif_net"] == 3000
    assert corps["total_passif"] == 0
    assert corps["equilibre"] is False


def test_bilan_desequilibre_avant_cloture_equilibre_apres(
    client: TestClient, db: Session
) -> None:
    """LE point non évident : un bilan pris EN COURS d'exercice ne s'équilibre pas tant que le
    résultat de la période n'est pas entré dans les capitaux propres via la clôture (591) — ce
    n'est pas un bug, c'est le résultat courant qui n'a pas encore sa place au bilan."""
    tresorerie = _compte(db, "1T18030", normal_side="D")
    capital = _compte(db, "5T18031", normal_side="C")
    produit = _compte(db, "7T18030", normal_side="C")
    tresorerie2 = _compte(db, "1T18032", normal_side="D")
    _mapper(db, tresorerie, etat="BILAN", masse="ACTIF", poste="Tresorerie test")
    _mapper(db, tresorerie2, etat="BILAN", masse="ACTIF", poste="Tresorerie test")
    _mapper(db, capital, etat="BILAN", masse="PASSIF", poste="Capital test")
    _mapper(db, produit, etat="RESULTAT", masse="PRODUIT", poste="Produit test")

    exercice = _exercice(db, "ETATS-1803", date(1803, 1, 1), date(1803, 12, 31))
    _valider_od(
        db, [LigneSaisie(tresorerie.id, "D", 100000), LigneSaisie(capital.id, "C", 100000)],
        date(1803, 2, 1),
    )
    _valider_od(
        db, [LigneSaisie(tresorerie2.id, "D", 5000), LigneSaisie(produit.id, "C", 5000)],
        date(1803, 6, 1),
    )
    db.commit()
    comptable = _entete(db, "COMPTABLE")

    avant = client.get(
        "/comptabilite/etats/bilan", params={"date": "1803-12-31"}, headers=comptable
    ).json()
    assert avant["total_actif_net"] == 105000
    assert avant["total_passif"] == 100000
    assert avant["ecart"] == 5000  # le résultat courant (produit non clôturé), pas une anomalie
    assert avant["equilibre"] is False

    _cloturer(client, comptable, str(exercice.id))

    apres = client.get(
        "/comptabilite/etats/bilan", params={"date": "1803-12-31"}, headers=comptable
    ).json()
    assert apres["total_actif_net"] == 105000
    assert apres["total_passif"] == 105000  # capital 100000 + 591 crédité de 5000
    assert apres["ecart"] == 0
    assert apres["equilibre"] is True


def test_bilan_sans_permission_refuse_403(client: TestClient, db: Session) -> None:
    caissier = _entete(db, "CAISSIER")

    reponse = client.get("/comptabilite/etats/bilan", headers=caissier)

    assert reponse.status_code == 403


# --- Compte de résultat ------------------------------------------------------------------------


def test_compte_resultat_exercice_en_cours(client: TestClient, db: Session) -> None:
    produit = _compte(db, "7T18040", normal_side="C")
    charge = _compte(db, "6T18040", normal_side="D")
    tresorerie = _compte(db, "1T18040", normal_side="D")
    _mapper(db, produit, etat="RESULTAT", masse="PRODUIT", poste="Produit test")
    _mapper(db, charge, etat="RESULTAT", masse="CHARGE", poste="Charge test")
    _mapper(db, tresorerie, etat="BILAN", masse="ACTIF", poste="Tresorerie test")

    exercice = _exercice(db, "ETATS-1804", date(1804, 1, 1), date(1804, 12, 31))
    _valider_od(
        db, [LigneSaisie(tresorerie.id, "D", 9000), LigneSaisie(produit.id, "C", 9000)],
        date(1804, 3, 1),
    )
    _valider_od(
        db, [LigneSaisie(charge.id, "D", 3000), LigneSaisie(tresorerie.id, "C", 3000)],
        date(1804, 4, 1),
    )
    db.commit()
    comptable = _entete(db, "COMPTABLE")

    reponse = client.get(
        "/comptabilite/etats/compte-resultat",
        params={"exercice_id": str(exercice.id)},
        headers=comptable,
    )

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["exercice_clos"] is False
    assert corps["source_resultat"] == "periode"
    assert corps["total_produits"] == 9000
    assert corps["total_charges"] == 3000
    assert corps["resultat_net"] == 6000
    assert len(corps["charges"]) == 1
    assert len(corps["produits"]) == 1


def test_compte_resultat_exercice_clos_redegrive_depuis_591(
    client: TestClient, db: Session
) -> None:
    produit = _compte(db, "7T18050", normal_side="C")
    tresorerie = _compte(db, "1T18050", normal_side="D")
    _mapper(db, produit, etat="RESULTAT", masse="PRODUIT", poste="Produit test")
    _mapper(db, tresorerie, etat="BILAN", masse="ACTIF", poste="Tresorerie test")

    exercice = _exercice(db, "ETATS-1805", date(1805, 1, 1), date(1805, 12, 31))
    _valider_od(
        db, [LigneSaisie(tresorerie.id, "D", 7000), LigneSaisie(produit.id, "C", 7000)],
        date(1805, 3, 1),
    )
    db.commit()
    comptable = _entete(db, "COMPTABLE")
    _cloturer(client, comptable, str(exercice.id))

    reponse = client.get(
        "/comptabilite/etats/compte-resultat",
        params={"exercice_id": str(exercice.id)},
        headers=comptable,
    )

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["exercice_clos"] is True
    assert corps["source_resultat"] == "cloture"
    assert corps["resultat_net"] == 7000
    assert corps["charges"] == []
    assert corps["produits"] == []  # détail indisponible post-clôture, signalé par source_resultat


def test_compte_resultat_exercice_introuvable_404(client: TestClient, db: Session) -> None:
    comptable = _entete(db, "COMPTABLE")

    reponse = client.get(
        "/comptabilite/etats/compte-resultat",
        params={"exercice_id": str(uuid.uuid4())},
        headers=comptable,
    )

    assert reponse.status_code == 404


# --- Administration du mapping -----------------------------------------------------------------


def test_lister_mapping_sans_permission_refuse_403(client: TestClient, db: Session) -> None:
    caissier = _entete(db, "CAISSIER")

    reponse = client.get("/comptabilite/etats/mapping", headers=caissier)

    assert reponse.status_code == 403


def test_modifier_mapping_verrouille_contre_le_seed(client: TestClient, db: Session) -> None:
    compte = _compte(db, "1T18060", normal_side="D")
    _mapper(db, compte, etat="BILAN", masse="ACTIF", poste="Ancien libelle", ordre=10)
    db.commit()
    responsable = _entete(db, "COMPTABLE")

    reponse = client.patch(
        f"/comptabilite/etats/mapping/{compte.id}",
        json={
            "etat": "BILAN", "masse": "ACTIF",
            "poste_libelle": "Nouveau libelle", "poste_ordre": 20,
        },
        headers=responsable,
    )

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["poste_libelle"] == "Nouveau libelle"
    assert corps["gere_manuellement"] is True

    verrou = db.execute(
        text(
            "SELECT gere_manuellement FROM comptabilite.financial_statement_mapping "
            "WHERE account_id = :id"
        ),
        {"id": compte.id},
    ).scalar_one()
    assert verrou is True


def test_modifier_mapping_introuvable_404(client: TestClient, db: Session) -> None:
    responsable = _entete(db, "COMPTABLE")

    reponse = client.patch(
        f"/comptabilite/etats/mapping/{uuid.uuid4()}",
        json={"etat": "BILAN", "masse": "ACTIF", "poste_libelle": "X", "poste_ordre": 1},
        headers=responsable,
    )

    assert reponse.status_code == 404


def test_modifier_mapping_champ_inattendu_refuse_extra_forbid(
    client: TestClient, db: Session
) -> None:
    compte = _compte(db, "1T18070", normal_side="D")
    _mapper(db, compte, etat="BILAN", masse="ACTIF", poste="Test")
    db.commit()
    responsable = _entete(db, "COMPTABLE")

    reponse = client.patch(
        f"/comptabilite/etats/mapping/{compte.id}",
        json={
            "etat": "BILAN", "masse": "ACTIF", "poste_libelle": "X", "poste_ordre": 1,
            "champ_en_trop": "non",
        },
        headers=responsable,
    )

    assert reponse.status_code == 422


# --- Provision 4319/4329 : se retranche de l'actif (P2.0-a) ---------------------------------


CSV_PLAN = (
    Path(__file__).resolve().parents[2] / "docs" / "reference" / "plan_comptable_import.csv"
)


def _scenario_provision_immos_en_cours(
    db: Session, annee: int, *, sens_provision: str | None = None
) -> tuple[Account, Account]:
    """Immobilisation en cours (4311, brute) de 50 000 financée par un capital de test, puis
    provision de 8 000 sur 4319 : crédit 4319 / débit capital. Comptes RÉELS du plan."""
    importer(db, str(CSV_PLAN))
    executer_seed_mapping_etats(db)
    brute = db.execute(select(Account).where(Account.account_number == "4311")).scalar_one()
    provision = db.execute(select(Account).where(Account.account_number == "4319")).scalar_one()
    if sens_provision is not None:  # simule une base seedée AVANT la correction
        provision.normal_side = sens_provision
        db.flush()
    capital = _compte(db, f"5T{annee}1", normal_side="C")
    _mapper(db, capital, etat="BILAN", masse="PASSIF", poste="Capital test")
    _exercice(db, f"ETATS-{annee}", date(annee, 1, 1), date(annee, 12, 31))
    _valider_od(
        db,
        [LigneSaisie(brute.id, "D", 50000), LigneSaisie(capital.id, "C", 50000)],
        date(annee, 6, 1),
    )
    _valider_od(
        db,
        [LigneSaisie(capital.id, "D", 8000), LigneSaisie(provision.id, "C", 8000)],
        date(annee, 6, 2),
    )
    db.commit()
    return brute, provision


def test_provision_4319_se_retranche_de_l_actif_au_bilan(
    client: TestClient, db: Session
) -> None:
    """Actif brut 50 000, provision 8 000 -> actif net 42 000. Raisonnement de signe : le montant
    retranché est le solde NORMALISÉ par `normal_side` (positif quand le solde est conforme au
    sens du compte). 4319 en C + mouvement au crédit = solde +8 000 = contra de 8 000, déduit."""
    _scenario_provision_immos_en_cours(db, 1803)
    comptable = _entete(db, "COMPTABLE")

    reponse = client.get(
        "/comptabilite/etats/bilan", params={"date": "1803-12-31"}, headers=comptable
    )

    corps = reponse.json()
    assert corps["total_actif_brut"] == 50000
    assert corps["total_contra_actif"] == 8000
    assert corps["total_actif_net"] == 42000
    assert corps["total_passif"] == 42000
    assert corps["equilibre"] is True
    assert corps["comptes_non_mappes"] == []


def test_ancien_sens_debiteur_faisait_augmenter_l_actif_net(
    client: TestClient, db: Session
) -> None:
    """Preuve du défaut corrigé : avec 4319 en D (ancien CSV), la même provision de 8 000 ressort
    à -8 000 et AUGMENTE l'actif net (58 000 au lieu de 42 000)."""
    _scenario_provision_immos_en_cours(db, 1804, sens_provision="D")
    comptable = _entete(db, "COMPTABLE")

    reponse = client.get(
        "/comptabilite/etats/bilan", params={"date": "1804-12-31"}, headers=comptable
    )

    corps = reponse.json()
    assert corps["total_contra_actif"] == -8000
    assert corps["total_actif_net"] == 58000


# --- P2.0-b1 : nouveaux comptes bruts, mapping complet, bilan équilibré -----------------------


def test_aucun_compte_de_saisie_du_plan_n_est_non_mappe(db: Session) -> None:
    """Après import du plan puis seed du mapping, TOUT compte de saisie du CSV a un poste d'état
    financier : le bilan ne peut afficher aucun « compte non mappé » par construction."""
    import csv

    importer(db, str(CSV_PLAN))
    executer_seed_mapping_etats(db)
    with open(CSV_PLAN, encoding="utf-8-sig", newline="") as f:
        saisie = [
            ligne["account_number"]
            for ligne in csv.DictReader(f, delimiter=";")
            if ligne["is_posting"] == "TRUE"
        ]

    non_mappes = db.execute(
        text(
            "SELECT a.account_number FROM comptabilite.accounts a "
            "LEFT JOIN comptabilite.financial_statement_mapping m ON m.account_id = a.id "
            "WHERE a.is_posting AND m.account_id IS NULL AND a.account_number = ANY(:n) "
            "ORDER BY 1"
        ),
        {"n": saisie},
    ).scalars().all()

    assert non_mappes == []


def test_bilan_equilibre_avec_les_nouveaux_comptes_bruts(client: TestClient, db: Session) -> None:
    """Participation brute 40 000 + incorporel brut 25 000 + corporel brut 15 000 = 80 000,
    financés par un écart de réévaluation (552400, capitaux propres) ; puis 6 000 d'amortissement
    (4418) imputés sur lui. Actif brut 80 000 moins contra 6 000 = 74 000 = passif 74 000."""
    importer(db, str(CSV_PLAN))
    executer_seed_mapping_etats(db)
    numeros = ("412100", "441100", "442100", "552400", "4418")
    comptes = {
        n: db.execute(select(Account).where(Account.account_number == n)).scalar_one()
        for n in numeros
    }
    _exercice(db, "ETATS-1805", date(1805, 1, 1), date(1805, 12, 31))
    _valider_od(
        db,
        [
            LigneSaisie(comptes["412100"].id, "D", 40000),
            LigneSaisie(comptes["441100"].id, "D", 25000),
            LigneSaisie(comptes["442100"].id, "D", 15000),
            LigneSaisie(comptes["552400"].id, "C", 80000),
        ],
        date(1805, 6, 1),
    )
    _valider_od(
        db,
        [LigneSaisie(comptes["552400"].id, "D", 6000), LigneSaisie(comptes["4418"].id, "C", 6000)],
        date(1805, 6, 2),
    )
    db.commit()
    comptable = _entete(db, "COMPTABLE")

    reponse = client.get(
        "/comptabilite/etats/bilan", params={"date": "1805-12-31"}, headers=comptable
    )

    corps = reponse.json()
    assert corps["comptes_non_mappes"] == []
    assert corps["total_actif_brut"] == 80000
    assert corps["total_contra_actif"] == 6000
    assert corps["total_actif_net"] == 74000
    assert corps["total_passif"] == 74000
    assert corps["equilibre"] is True
    postes = {p["poste_libelle"]: p["montant"] for p in corps["actif"] if p["masse"] == "ACTIF"}
    assert postes["Immobilisations financieres"] == 40000
    assert postes["Immobilisations d'exploitation"] == 40000  # 25 000 + 15 000
