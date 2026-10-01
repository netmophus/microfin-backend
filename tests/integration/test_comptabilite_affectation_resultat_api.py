"""API — Affectation du résultat d'un exercice clos (chantier P1, lot b2a).

  - ventilation À LA MAIN (5521/5522/5523/58), la somme doit égaler EXACTEMENT le résultat ;
  - un déficit ne se ventile qu'en report à nouveau (58), jamais en réserves ;
  - POINT CRITIQUE : le montant à affecter est lu depuis LA PIÈCE DE CLÔTURE de CET exercice,
    jamais le solde courant (global, non scopé) du compte 591 — testé explicitement ci-dessous
    avec deux exercices clos dont les résultats s'accumuleraient sur 591 si on se trompait ;
  - 592 n'est JAMAIS utilisé (décision actée) — vérifié directement en base ;
  - garde anti-double affectation (`resultat_affecte_at`) ;
  - préalable : l'exercice doit être 'clos' (lot b1) ;
  - permissions : compta.exercice.manage (déjà attribuée à COMPTABLE), 403 sinon.

Exercices de test TOUJOURS synthétiques (2098/2099), jamais le "2026" réel de la base de dev
partagée pour la CLÔTURE elle-même — voir blast-radius-comptes-partages. L'écriture
d'AFFECTATION, elle, est nécessairement datée au jour courant (indépendance b2a/b2b, décision
actée) : elle atterrit donc dans l'exercice réellement ouvert aujourd'hui sur cette base de dev
("2026"), en utilisant les VRAIS comptes RCSFD 591/5521/5522/5523/58 — inévitable (comptes
imposés par le référentiel) mais sans risque : tout est annulé par le rollback de la fixture
`db` (SAVEPOINT), et les assertions portent sur l'écriture précise créée, jamais sur un solde
agrégé de ces comptes partagés.
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
    normal_side = "D" if numero.startswith("6") else "C"
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


def _exercice_excedent(
    client: TestClient, db: Session, headers: dict[str, str], *, code: str, annee: int, montant: int
) -> Exercice:
    """Un exercice synthétique clos avec un excédent net de `montant`. Produit sans contrepartie
    charge (couplé à un compte de trésorerie, classe 5, hors 6/7) : le résultat de clôture
    (Σ crédit - débit sur 6/7) vaut alors exactement `montant`, sans compensation possible."""
    produit = _compte(db, f"7T{annee}0")
    tresorerie = _compte(db, f"5T{annee}0", account_class=5, normal_side="D")
    exercice = _exercice(db, code, date(annee, 1, 1), date(annee, 12, 31))
    _valider_od(
        db,
        [LigneSaisie(tresorerie.id, "D", montant), LigneSaisie(produit.id, "C", montant)],
        date(annee, 6, 1),
    )
    db.commit()
    _cloturer(client, headers, str(exercice.id))
    return exercice


# --- Aperçu ----------------------------------------------------------------------------------


def test_apercu_sur_exercice_encore_ouvert_non_affectable(client: TestClient, db: Session) -> None:
    exercice = _exercice(db, "B2A-OUV-1", date(2097, 1, 1), date(2097, 12, 31))
    db.commit()
    comptable = _entete(db, "COMPTABLE")

    reponse = client.get(
        f"/comptabilite/exercices/{exercice.id}/previsualisation-affectation", headers=comptable
    )

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["montant"] is None
    assert corps["deja_affecte"] is False
    assert corps["affectable"] is False


def test_apercu_excedent(client: TestClient, db: Session) -> None:
    comptable = _entete(db, "COMPTABLE")
    exercice = _exercice_excedent(
        client, db, comptable, code="B2A-EXC-1", annee=2096, montant=40000
    )

    reponse = client.get(
        f"/comptabilite/exercices/{exercice.id}/previsualisation-affectation", headers=comptable
    )

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["montant"] == 40000
    assert corps["deja_affecte"] is False
    assert corps["affectable"] is True


def test_apercu_deficit(client: TestClient, db: Session) -> None:
    comptable = _entete(db, "COMPTABLE")
    charge = _compte(db, "6T20950")
    tresorerie = _compte(db, "5T20950", account_class=5, normal_side="D")
    exercice = _exercice(db, "B2A-DEF-1", date(2095, 1, 1), date(2095, 12, 31))
    _valider_od(
        db, [LigneSaisie(charge.id, "D", 8000), LigneSaisie(tresorerie.id, "C", 8000)],
        date(2095, 6, 1),
    )
    db.commit()
    _cloturer(client, comptable, str(exercice.id))

    reponse = client.get(
        f"/comptabilite/exercices/{exercice.id}/previsualisation-affectation", headers=comptable
    )

    assert reponse.status_code == 200
    assert reponse.json()["montant"] == -8000


# --- Point critique : base de calcul = la pièce de clôture, pas le solde courant de 591 -------


def test_montant_base_sur_la_piece_de_cloture_pas_le_solde_courant_591(
    client: TestClient, db: Session
) -> None:
    """Deux exercices clos, 591 cumule donc les deux résultats (10 000 + 25 000 = 35 000) — mais
    affecter l'exercice A ne doit porter QUE sur son propre résultat (10 000), jamais sur le
    solde agrégé du compte."""
    comptable = _entete(db, "COMPTABLE")
    exercice_a = _exercice_excedent(
        client, db, comptable, code="B2A-CUMUL-A", annee=2093, montant=10000
    )
    exercice_b = _exercice_excedent(
        client, db, comptable, code="B2A-CUMUL-B", annee=2094, montant=25000
    )

    # Le solde agrégé de 591 (toutes écritures validées confondues) vaut bien la somme des deux.
    solde_591 = db.execute(
        text(
            "SELECT COALESCE(SUM(CASE WHEN jl.side='C' THEN jl.amount ELSE -jl.amount END), 0) "
            "FROM comptabilite.journal_lines jl "
            "JOIN comptabilite.accounts a ON a.id = jl.account_id "
            "JOIN comptabilite.journal_entries je ON je.id = jl.entry_id "
            "WHERE a.account_number = '591' AND je.status = 'validee'"
        )
    ).scalar_one()
    assert solde_591 >= 35000  # >= : la base de dev peut déjà porter d'autres écritures passées

    apercu_a = client.get(
        f"/comptabilite/exercices/{exercice_a.id}/previsualisation-affectation", headers=comptable
    ).json()
    apercu_b = client.get(
        f"/comptabilite/exercices/{exercice_b.id}/previsualisation-affectation", headers=comptable
    ).json()

    assert apercu_a["montant"] == 10000
    assert apercu_b["montant"] == 25000

    reponse = client.post(
        f"/comptabilite/exercices/{exercice_a.id}/affectation",
        json={"report_a_nouveau": 10000},
        headers=comptable,
    )
    assert reponse.status_code == 200
    assert reponse.json()["montant"] == 10000  # jamais 35000

    # B reste affectable, intact, pour son propre montant.
    apercu_b_apres = client.get(
        f"/comptabilite/exercices/{exercice_b.id}/previsualisation-affectation", headers=comptable
    ).json()
    assert apercu_b_apres["montant"] == 25000
    assert apercu_b_apres["deja_affecte"] is False


# --- Affectation réussie -----------------------------------------------------------------


def test_affecte_un_excedent_ventile_sur_plusieurs_comptes(
    client: TestClient, db: Session
) -> None:
    comptable = _entete(db, "COMPTABLE")
    exercice = _exercice_excedent(
        client, db, comptable, code="B2A-VENT-1", annee=2092, montant=100000
    )

    reponse = client.post(
        f"/comptabilite/exercices/{exercice.id}/affectation",
        json={
            "reserve_generale": 60000,
            "reserves_facultatives": 15000,
            "autres_reserves": 5000,
            "report_a_nouveau": 20000,
        },
        headers=comptable,
    )

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["montant"] == 100000
    numero = corps["entry_number"]

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
    assert par_compte["591"] == ("D", 100000)
    assert par_compte["5521"] == ("C", 60000)
    assert par_compte["5522"] == ("C", 15000)
    assert par_compte["5523"] == ("C", 5000)
    assert par_compte["58"] == ("C", 20000)
    # Décision actée : 592 n'est JAMAIS utilisé.
    assert "592" not in par_compte

    # Marqueur posé.
    marque = db.execute(
        text(
            "SELECT resultat_affecte_at IS NOT NULL FROM comptabilite.exercices WHERE id = :id"
        ),
        {"id": exercice.id},
    ).scalar_one()
    assert marque is True


def test_affecte_un_deficit_uniquement_en_report_a_nouveau(
    client: TestClient, db: Session
) -> None:
    comptable = _entete(db, "COMPTABLE")
    charge = _compte(db, "6T20910")
    tresorerie = _compte(db, "5T20910", account_class=5, normal_side="D")
    exercice = _exercice(db, "B2A-DEF-2", date(2091, 1, 1), date(2091, 12, 31))
    _valider_od(
        db, [LigneSaisie(charge.id, "D", 6000), LigneSaisie(tresorerie.id, "C", 6000)],
        date(2091, 6, 1),
    )
    db.commit()
    _cloturer(client, comptable, str(exercice.id))

    reponse = client.post(
        f"/comptabilite/exercices/{exercice.id}/affectation",
        json={"report_a_nouveau": 6000},
        headers=comptable,
    )

    assert reponse.status_code == 200
    numero = reponse.json()["entry_number"]
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
    assert par_compte["58"] == ("D", 6000)
    assert par_compte["591"] == ("C", 6000)
    assert len(par_compte) == 2


# --- Refus ---------------------------------------------------------------------------------


def test_refuse_une_ventilation_dont_la_somme_ne_correspond_pas(
    client: TestClient, db: Session
) -> None:
    comptable = _entete(db, "COMPTABLE")
    exercice = _exercice_excedent(
        client, db, comptable, code="B2A-SOMME-1", annee=2090, montant=50000
    )

    reponse = client.post(
        f"/comptabilite/exercices/{exercice.id}/affectation",
        json={"report_a_nouveau": 49999},
        headers=comptable,
    )

    assert reponse.status_code == 422
    assert "50000" in reponse.json()["detail"] or "49999" in reponse.json()["detail"]


def test_refuse_des_reserves_sur_un_deficit(client: TestClient, db: Session) -> None:
    comptable = _entete(db, "COMPTABLE")
    charge = _compte(db, "6T20890")
    tresorerie = _compte(db, "5T20890", account_class=5, normal_side="D")
    exercice = _exercice(db, "B2A-DEF-RES", date(2089, 1, 1), date(2089, 12, 31))
    _valider_od(
        db, [LigneSaisie(charge.id, "D", 3000), LigneSaisie(tresorerie.id, "C", 3000)],
        date(2089, 6, 1),
    )
    db.commit()
    _cloturer(client, comptable, str(exercice.id))

    reponse = client.post(
        f"/comptabilite/exercices/{exercice.id}/affectation",
        json={"reserve_generale": 3000},
        headers=comptable,
    )

    assert reponse.status_code == 422
    assert "réserve" in reponse.json()["detail"].lower()


def test_refuse_daffecter_un_exercice_encore_ouvert(client: TestClient, db: Session) -> None:
    exercice = _exercice(db, "B2A-OUV-2", date(2088, 1, 1), date(2088, 12, 31))
    db.commit()
    comptable = _entete(db, "COMPTABLE")

    reponse = client.post(
        f"/comptabilite/exercices/{exercice.id}/affectation",
        json={"report_a_nouveau": 100},
        headers=comptable,
    )

    assert reponse.status_code == 422
    assert "clos" in reponse.json()["detail"].lower()


def test_refuse_daffecter_deux_fois(client: TestClient, db: Session) -> None:
    comptable = _entete(db, "COMPTABLE")
    exercice = _exercice_excedent(
        client, db, comptable, code="B2A-DOUBLE-1", annee=2087, montant=7000
    )
    premiere = client.post(
        f"/comptabilite/exercices/{exercice.id}/affectation",
        json={"report_a_nouveau": 7000},
        headers=comptable,
    )
    assert premiere.status_code == 200

    reponse = client.post(
        f"/comptabilite/exercices/{exercice.id}/affectation",
        json={"report_a_nouveau": 7000},
        headers=comptable,
    )

    assert reponse.status_code == 422
    assert "déjà" in reponse.json()["detail"].lower()


def test_refuse_si_resultat_nul_a_la_cloture(client: TestClient, db: Session) -> None:
    """Un exercice clos dont le résultat était exactement nul : b1 n'a posté aucune ligne 591,
    donc rien à affecter — pas une pièce de clôture introuvable, un résultat nul."""
    comptable = _entete(db, "COMPTABLE")
    charge = _compte(db, "6T20860")
    produit = _compte(db, "7T20860")
    exercice = _exercice(db, "B2A-NUL-1", date(2086, 1, 1), date(2086, 12, 31))
    _valider_od(
        db, [LigneSaisie(charge.id, "D", 1000), LigneSaisie(produit.id, "C", 1000)],
        date(2086, 6, 1),
    )
    db.commit()
    _cloturer(client, comptable, str(exercice.id))

    reponse = client.post(
        f"/comptabilite/exercices/{exercice.id}/affectation",
        json={},
        headers=comptable,
    )

    assert reponse.status_code == 422
    assert "nul" in reponse.json()["detail"].lower()


def test_champ_inattendu_refuse_extra_forbid(client: TestClient, db: Session) -> None:
    comptable = _entete(db, "COMPTABLE")
    exercice = _exercice_excedent(
        client, db, comptable, code="B2A-FORBID-1", annee=2085, montant=1000
    )

    reponse = client.post(
        f"/comptabilite/exercices/{exercice.id}/affectation",
        json={"report_a_nouveau": 1000, "compte_arbitraire": "591"},
        headers=comptable,
    )

    assert reponse.status_code == 422


def test_affectation_exercice_introuvable_404(client: TestClient, db: Session) -> None:
    comptable = _entete(db, "COMPTABLE")

    reponse = client.post(
        f"/comptabilite/exercices/{uuid.uuid4()}/affectation",
        json={"report_a_nouveau": 100},
        headers=comptable,
    )

    assert reponse.status_code == 404


def test_affectation_sans_permission_refuse_403(client: TestClient, db: Session) -> None:
    exercice = _exercice(db, "B2A-403-1", date(2084, 1, 1), date(2084, 12, 31))
    db.commit()
    caissier = _entete(db, "CAISSIER")

    reponse = client.post(
        f"/comptabilite/exercices/{exercice.id}/affectation",
        json={"report_a_nouveau": 100},
        headers=caissier,
    )

    assert reponse.status_code == 403


def test_liste_exercices_expose_resultat_affecte(client: TestClient, db: Session) -> None:
    comptable = _entete(db, "COMPTABLE")
    exercice = _exercice_excedent(
        client, db, comptable, code="B2A-BADGE-1", annee=2083, montant=500
    )
    client.post(
        f"/comptabilite/exercices/{exercice.id}/affectation",
        json={"report_a_nouveau": 500},
        headers=comptable,
    )

    liste = client.get("/comptabilite/exercices", headers=comptable).json()
    ligne = next(e for e in liste if e["id"] == str(exercice.id))
    assert ligne["resultat_affecte"] is True
