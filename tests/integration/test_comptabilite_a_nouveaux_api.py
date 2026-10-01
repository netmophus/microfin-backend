"""API — Génération des à-nouveaux à l'ouverture de l'exercice suivant (chantier P1, lot b2b).

  - périmètre : UNIQUEMENT les comptes de bilan (classes 1 à 5) à solde de clôture non nul —
    les classes 6/7 sont exclues (déjà soldées par b1) même si, par construction, elles sont
    toujours nulles à ce stade ;
  - 591 n'est PAS un cas particulier : il se reporte comme n'importe quel compte de classe 5,
    que le résultat ait été affecté (b2a) ou non — indépendance b2a/b2b actée ;
  - l'exercice SUIVANT est celui qui commence EXACTEMENT le lendemain de la fin de la source —
    un trou dans le calendrier (exercice suivant qui existe mais plus loin) n'est PAS accepté ;
  - équilibre vérifié explicitement (Σ débit = Σ crédit sur les lignes construites) ;
  - garde anti-double génération portée par l'exercice SUIVANT (receveur), pas la source ;
  - préalables : source 'clos', suivant EXISTE et 'ouvert' ;
  - permissions : compta.exercice.manage (déjà attribuée à COMPTABLE), 403 sinon.

Exercices de test TOUJOURS synthétiques, et TOUJOURS dans le PASSÉ (1860-1882) — jamais le
"2026" réel de la base de dev partagée, et jamais une année future. Raison PRÉCISE, propre à ce
lot : `rapports.balance(date_debut=None, ...)` (voir a_nouveaux.py) cumule TOUTES les écritures
validées depuis l'origine jusqu'à `date_fin` — une année synthétique FUTURE hériterait donc de
tout l'historique réel de cette base (dont les classes 6/7 du "2026" réel, jamais closes pour de
vrai, qui casseraient l'invariant d'équilibre). Une année dans le passé, avant toute donnée
réelle (confirmé : la plus ancienne écriture validée de cette base date du 28/09/2026), exclut
structurellement ce risque. Les comptes RCSFD réels (591) ne sont touchés que par la clôture
(b1), déjà couverte par son propre fichier de tests.

NOTE DE CONCEPTION DES FIXTURES : chaque mouvement posé via `_valider_od` doit être ÉQUILIBRÉ
(Σ débit = Σ crédit) — le moteur (`ecritures.valider`) le refuse sinon. Pour obtenir un résultat
de clôture non nul sans ambiguïté, les tests créditent un compte de PRODUIT (classe 7) et
débitent une contrepartie de TRÉSORERIE (classe 1), jamais un montant asymétrique sur deux
comptes différents.
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
from app.modules.comptabilite import ecritures
from app.modules.comptabilite.ecritures import LigneSaisie
from app.modules.comptabilite.models import Account, Exercice, Journal
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
    normal_side = "D" if numero.startswith(("1", "6")) else "C"
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


def _exercice_clos_avec_resultat(
    client: TestClient, db: Session, headers: dict[str, str], *, code: str, annee: int, montant: int
) -> Exercice:
    """Exercice synthétique clos avec un excédent net de `montant` — produit crédité, trésorerie
    débitée du même montant (mouvement équilibré, isole proprement le résultat)."""
    produit = _compte(db, f"7T{annee}0")
    tresorerie = _compte(db, f"1T{annee}0", account_class=1)
    exercice = _exercice(db, code, date(annee, 1, 1), date(annee, 12, 31))
    _valider_od(
        db, [LigneSaisie(tresorerie.id, "D", montant), LigneSaisie(produit.id, "C", montant)],
        date(annee, 6, 1),
    )
    db.commit()
    _cloturer(client, headers, str(exercice.id))
    return exercice


def _exercice_clos_resultat_nul(
    db: Session, *, code: str, annee: int
) -> Exercice:
    """Exercice synthétique dont le résultat de clôture est NUL (b1 ne poste aucune ligne 591) :
    charge et produit du même montant, mouvement équilibré, résultat net = 0."""
    charge = _compte(db, f"6T{annee}0")
    produit = _compte(db, f"7T{annee}0")
    exercice = _exercice(db, code, date(annee, 1, 1), date(annee, 12, 31))
    _valider_od(
        db, [LigneSaisie(charge.id, "D", 1000), LigneSaisie(produit.id, "C", 1000)],
        date(annee, 6, 1),
    )
    db.commit()
    return exercice


# --- Aperçu ------------------------------------------------------------------------------


def test_apercu_exercice_source_non_clos(client: TestClient, db: Session) -> None:
    exercice = _exercice(db, "B2B-OUV-1", date(1882, 1, 1), date(1882, 12, 31))
    db.commit()
    comptable = _entete(db, "COMPTABLE")

    reponse = client.get(
        f"/comptabilite/exercices/{exercice.id}/previsualisation-a-nouveaux", headers=comptable
    )

    assert reponse.status_code == 200
    assert reponse.json()["generable"] is False


def test_apercu_exercice_suivant_absent(client: TestClient, db: Session) -> None:
    comptable = _entete(db, "COMPTABLE")
    exercice = _exercice_clos_avec_resultat(
        client, db, comptable, code="B2B-SANS-SUIVANT", annee=1881, montant=1000
    )

    reponse = client.get(
        f"/comptabilite/exercices/{exercice.id}/previsualisation-a-nouveaux", headers=comptable
    )

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["exercice_suivant"] is None
    assert corps["generable"] is False


def test_apercu_exercice_suivant_avec_un_trou_est_ignore(
    client: TestClient, db: Session
) -> None:
    """L'exercice suivant doit commencer EXACTEMENT le lendemain — un exercice existant mais
    plus loin dans le calendrier (trou) ne compte pas comme « suivant »."""
    comptable = _entete(db, "COMPTABLE")
    exercice = _exercice_clos_avec_resultat(
        client, db, comptable, code="B2B-TROU-SRC", annee=1880, montant=1000
    )
    # Un exercice existe bien après, mais avec un trou (1881-02-01, pas 1881-01-01).
    _exercice(db, "B2B-TROU-LOIN", date(1881, 2, 1), date(1881, 12, 31))
    db.commit()

    reponse = client.get(
        f"/comptabilite/exercices/{exercice.id}/previsualisation-a-nouveaux", headers=comptable
    )

    assert reponse.status_code == 200
    assert reponse.json()["exercice_suivant"] is None


def test_apercu_exercice_suivant_existe_mais_pas_ouvert(
    client: TestClient, db: Session
) -> None:
    comptable = _entete(db, "COMPTABLE")
    exercice = _exercice_clos_avec_resultat(
        client, db, comptable, code="B2B-SUIV-CLOS-SRC", annee=1879, montant=1000
    )
    suivant = _exercice_clos_avec_resultat(
        client, db, comptable, code="B2B-SUIV-CLOS", annee=1880, montant=500
    )

    reponse = client.get(
        f"/comptabilite/exercices/{exercice.id}/previsualisation-a-nouveaux", headers=comptable
    )

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["exercice_suivant"]["id"] == str(suivant.id)
    assert corps["exercice_suivant"]["status"] == "clos"
    assert corps["generable"] is False


def test_apercu_liste_les_comptes_de_bilan_classes_1_a_5_exclut_6_et_7(
    client: TestClient, db: Session
) -> None:
    comptable = _entete(db, "COMPTABLE")
    produit = _compte(db, "7T18780")
    tresorerie = _compte(db, "1T18780", account_class=1)
    capital = _compte(db, "5T18781", account_class=5, normal_side="C")
    exercice = _exercice(db, "B2B-DETAIL", date(1878, 1, 1), date(1878, 12, 31))
    # Résultat net de 5 000 (produit crédité, trésorerie débitée du même montant).
    _valider_od(
        db, [LigneSaisie(tresorerie.id, "D", 5000), LigneSaisie(produit.id, "C", 5000)],
        date(1878, 6, 1),
    )
    # Mouvement de bilan pur, sans rapport avec le résultat (trésorerie/capital, 20 000).
    _valider_od(
        db, [LigneSaisie(tresorerie.id, "D", 20000), LigneSaisie(capital.id, "C", 20000)],
        date(1878, 6, 2),
    )
    _exercice(db, "B2B-DETAIL-SUIV", date(1879, 1, 1), date(1879, 12, 31))
    db.commit()
    _cloturer(client, comptable, str(exercice.id))

    reponse = client.get(
        f"/comptabilite/exercices/{exercice.id}/previsualisation-a-nouveaux", headers=comptable
    )

    assert reponse.status_code == 200
    corps = reponse.json()
    comptes = {ligne["account_number"]: ligne for ligne in corps["lignes"]}
    assert "7T18780" not in comptes  # classe 7, exclue
    # Trésorerie cumule les deux mouvements : 5 000 + 20 000 = 25 000, toujours au débit.
    assert comptes["1T18780"] == {
        "account_number": "1T18780", "name": "Compte 1T18780", "account_class": 1,
        "side": "D", "amount": 25000,
    }
    assert comptes["5T18781"] == {
        "account_number": "5T18781", "name": "Compte 5T18781", "account_class": 5,
        "side": "C", "amount": 20000,
    }
    # Le résultat (5 000) a été posté en 591 par b1 — présent lui aussi, côté classe 5.
    assert comptes["591"]["side"] == "C"
    assert comptes["591"]["amount"] == 5000
    assert corps["equilibre"] is True
    assert corps["generable"] is True


def test_apercu_solde_anormal_inverse_le_sens(client: TestClient, db: Session) -> None:
    """Un compte classe 1 (normal_side D) qui termine CRÉDITEUR (solde anormal) doit être
    reporté en CRÉDIT, pas en débit — le sens suit le signe du solde, pas le sens normal."""
    comptable = _entete(db, "COMPTABLE")
    compte_1 = _compte(db, "1T18770", account_class=1)  # normal_side D
    contrepartie = _compte(db, "5T18771", account_class=5, normal_side="C")
    exercice = _exercice_clos_resultat_nul(db, code="B2B-ANORMAL", annee=1877)
    # compte_1 (D-normal) termine créditeur : crédité sans jamais être débité.
    _valider_od(
        db, [LigneSaisie(contrepartie.id, "D", 9000), LigneSaisie(compte_1.id, "C", 9000)],
        date(1877, 6, 2),
    )
    _exercice(db, "B2B-ANORMAL-SUIV", date(1878, 1, 1), date(1878, 12, 31))
    db.commit()
    _cloturer(client, comptable, str(exercice.id))

    reponse = client.get(
        f"/comptabilite/exercices/{exercice.id}/previsualisation-a-nouveaux", headers=comptable
    )

    corps = reponse.json()
    comptes = {ligne["account_number"]: ligne for ligne in corps["lignes"]}
    assert comptes["1T18770"]["side"] == "C"
    assert comptes["1T18770"]["amount"] == 9000
    assert comptes["5T18771"] == {
        "account_number": "5T18771", "name": "Compte 5T18771", "account_class": 5,
        "side": "D", "amount": 9000,
    }
    assert "591" not in comptes  # résultat nul : b1 n'a posté aucune ligne 591
    assert corps["equilibre"] is True


def test_apercu_rien_a_reporter_si_resultat_nul_et_aucun_mouvement_de_bilan(
    client: TestClient, db: Session
) -> None:
    """Résultat de clôture nul (b1 ne poste aucune ligne 591) et aucun autre mouvement de bilan :
    rien à reporter."""
    comptable = _entete(db, "COMPTABLE")
    exercice = _exercice_clos_resultat_nul(db, code="B2B-NUL", annee=1876)
    _exercice(db, "B2B-NUL-SUIV", date(1877, 1, 1), date(1877, 12, 31))
    db.commit()
    _cloturer(client, comptable, str(exercice.id))

    reponse = client.get(
        f"/comptabilite/exercices/{exercice.id}/previsualisation-a-nouveaux", headers=comptable
    )

    corps = reponse.json()
    assert corps["lignes"] == []
    assert corps["generable"] is False


def test_apercu_exercice_introuvable_404(client: TestClient, db: Session) -> None:
    comptable = _entete(db, "COMPTABLE")

    reponse = client.get(
        f"/comptabilite/exercices/{uuid.uuid4()}/previsualisation-a-nouveaux", headers=comptable
    )

    assert reponse.status_code == 404


def test_apercu_sans_permission_refuse_403(client: TestClient, db: Session) -> None:
    exercice = _exercice(db, "B2B-403-APERCU", date(1875, 1, 1), date(1875, 12, 31))
    db.commit()
    caissier = _entete(db, "CAISSIER")

    reponse = client.get(
        f"/comptabilite/exercices/{exercice.id}/previsualisation-a-nouveaux", headers=caissier
    )

    assert reponse.status_code == 403


# --- Génération ----------------------------------------------------------------------------


def test_genere_les_a_nouveaux_pose_lecriture_dans_an(
    client: TestClient, db: Session
) -> None:
    comptable = _entete(db, "COMPTABLE")
    produit = _compte(db, "7T18740")
    tresorerie = _compte(db, "1T18740", account_class=1)
    capital = _compte(db, "5T18741", account_class=5, normal_side="C")
    exercice = _exercice(db, "B2B-GEN-1", date(1874, 1, 1), date(1874, 12, 31))
    # Résultat net de 7 000.
    _valider_od(
        db, [LigneSaisie(tresorerie.id, "D", 7000), LigneSaisie(produit.id, "C", 7000)],
        date(1874, 6, 1),
    )
    # Mouvement de bilan pur (trésorerie/capital, 15 000), sans rapport avec le résultat.
    _valider_od(
        db, [LigneSaisie(tresorerie.id, "D", 15000), LigneSaisie(capital.id, "C", 15000)],
        date(1874, 6, 2),
    )
    suivant = _exercice(db, "B2B-GEN-1-SUIV", date(1875, 1, 1), date(1875, 12, 31))
    db.commit()
    _cloturer(client, comptable, str(exercice.id))

    reponse = client.post(
        f"/comptabilite/exercices/{exercice.id}/a-nouveaux", headers=comptable
    )

    assert reponse.status_code == 200, reponse.text
    corps = reponse.json()
    assert corps["total"] == 22000  # trésorerie cumulée : 7 000 + 15 000
    numero = corps["entry_number"]
    assert numero.startswith("AN-")

    journal_reel = db.execute(
        text(
            "SELECT j.code, je.entry_date FROM comptabilite.journal_entries je "
            "JOIN comptabilite.journals j ON j.id = je.journal_id "
            "WHERE je.entry_number = :n"
        ),
        {"n": numero},
    ).one()
    assert journal_reel.code == "AN"
    assert journal_reel.entry_date == date(1875, 1, 1)

    lignes = db.execute(
        text(
            "SELECT a.account_number, jl.side, jl.amount FROM comptabilite.journal_lines jl "
            "JOIN comptabilite.journal_entries je ON je.id = jl.entry_id "
            "JOIN comptabilite.accounts a ON a.id = jl.account_id "
            "WHERE je.entry_number = :n ORDER BY a.account_number"
        ),
        {"n": numero},
    ).all()
    par_compte = {ligne.account_number: (ligne.side, ligne.amount) for ligne in lignes}
    assert par_compte["1T18740"] == ("D", 22000)
    assert par_compte["5T18741"] == ("C", 15000)
    assert par_compte["591"] == ("C", 7000)

    marque = db.execute(
        text(
            "SELECT a_nouveaux_generes_at IS NOT NULL FROM comptabilite.exercices WHERE id = :id"
        ),
        {"id": suivant.id},
    ).scalar_one()
    assert marque is True
    # La source, elle, ne porte AUCUN marqueur (posé sur le receveur).
    marque_source = db.execute(
        text(
            "SELECT a_nouveaux_generes_at IS NOT NULL FROM comptabilite.exercices WHERE id = :id"
        ),
        {"id": exercice.id},
    ).scalar_one()
    assert marque_source is False


def test_genere_independamment_de_laffectation_du_resultat(
    client: TestClient, db: Session
) -> None:
    """591 non affecté (b2a jamais appelé) : se reporte quand même tel quel — indépendance
    b2a/b2b actée."""
    comptable = _entete(db, "COMPTABLE")
    exercice = _exercice_clos_avec_resultat(
        client, db, comptable, code="B2B-INDEP-1", annee=1873, montant=3000
    )
    _exercice(db, "B2B-INDEP-1-SUIV", date(1874, 1, 1), date(1874, 12, 31))
    db.commit()

    # Pas d'appel à /affectation ici — volontairement.
    reponse = client.post(
        f"/comptabilite/exercices/{exercice.id}/a-nouveaux", headers=comptable
    )

    assert reponse.status_code == 200
    assert reponse.json()["total"] == 3000


def test_refuse_si_exercice_source_pas_clos(client: TestClient, db: Session) -> None:
    exercice = _exercice(db, "B2B-OUV-2", date(1872, 1, 1), date(1872, 12, 31))
    _exercice(db, "B2B-OUV-2-SUIV", date(1873, 1, 1), date(1873, 12, 31))
    db.commit()
    comptable = _entete(db, "COMPTABLE")

    reponse = client.post(
        f"/comptabilite/exercices/{exercice.id}/a-nouveaux", headers=comptable
    )

    assert reponse.status_code == 422
    assert "clos" in reponse.json()["detail"].lower()


def test_refuse_si_exercice_suivant_absent(client: TestClient, db: Session) -> None:
    comptable = _entete(db, "COMPTABLE")
    exercice = _exercice_clos_avec_resultat(
        client, db, comptable, code="B2B-SANS-SUIV-GEN", annee=1871, montant=1000
    )

    reponse = client.post(
        f"/comptabilite/exercices/{exercice.id}/a-nouveaux", headers=comptable
    )

    assert reponse.status_code == 422
    assert "1872-01-01" in reponse.json()["detail"]


def test_refuse_si_exercice_suivant_pas_ouvert(client: TestClient, db: Session) -> None:
    comptable = _entete(db, "COMPTABLE")
    exercice = _exercice_clos_avec_resultat(
        client, db, comptable, code="B2B-SUIV-FERME-SRC", annee=1869, montant=1000
    )
    _exercice_clos_avec_resultat(
        client, db, comptable, code="B2B-SUIV-FERME", annee=1870, montant=400
    )

    reponse = client.post(
        f"/comptabilite/exercices/{exercice.id}/a-nouveaux", headers=comptable
    )

    assert reponse.status_code == 422
    assert "ouvert" in reponse.json()["detail"].lower()


def test_refuse_de_generer_deux_fois(client: TestClient, db: Session) -> None:
    comptable = _entete(db, "COMPTABLE")
    exercice = _exercice_clos_avec_resultat(
        client, db, comptable, code="B2B-DOUBLE-1", annee=1866, montant=2000
    )
    _exercice(db, "B2B-DOUBLE-1-SUIV", date(1867, 1, 1), date(1867, 12, 31))
    db.commit()
    premiere = client.post(
        f"/comptabilite/exercices/{exercice.id}/a-nouveaux", headers=comptable
    )
    assert premiere.status_code == 200

    reponse = client.post(
        f"/comptabilite/exercices/{exercice.id}/a-nouveaux", headers=comptable
    )

    assert reponse.status_code == 422
    assert "déjà" in reponse.json()["detail"].lower()


def test_refuse_si_rien_a_reporter(client: TestClient, db: Session) -> None:
    comptable = _entete(db, "COMPTABLE")
    exercice = _exercice_clos_resultat_nul(db, code="B2B-RIEN-1", annee=1865)
    _exercice(db, "B2B-RIEN-1-SUIV", date(1866, 1, 1), date(1866, 12, 31))
    db.commit()
    _cloturer(client, comptable, str(exercice.id))

    reponse = client.post(
        f"/comptabilite/exercices/{exercice.id}/a-nouveaux", headers=comptable
    )

    assert reponse.status_code == 422
    assert "rien à reporter" in reponse.json()["detail"].lower()


def test_a_nouveaux_exercice_introuvable_404(client: TestClient, db: Session) -> None:
    comptable = _entete(db, "COMPTABLE")

    reponse = client.post(
        f"/comptabilite/exercices/{uuid.uuid4()}/a-nouveaux", headers=comptable
    )

    assert reponse.status_code == 404


def test_a_nouveaux_sans_permission_refuse_403(client: TestClient, db: Session) -> None:
    exercice = _exercice(db, "B2B-403-GEN", date(1864, 1, 1), date(1864, 12, 31))
    db.commit()
    caissier = _entete(db, "CAISSIER")

    reponse = client.post(
        f"/comptabilite/exercices/{exercice.id}/a-nouveaux", headers=caissier
    )

    assert reponse.status_code == 403
