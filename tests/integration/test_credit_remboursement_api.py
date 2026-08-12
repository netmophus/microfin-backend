"""API Crédit CR4 — remboursement : D CAISSE / C CREDIT / C PRODUITS_INTERETS.

- la ligne PRODUITS_INTERETS est OMISE proprement quand interets=0 (produit à taux nul) —
  pas une ligne à montant zéro, une ligne ABSENTE ;
- un montant qui ne correspond pas exactement au total de l'échéance est refusé avec le
  montant attendu, lisible par un caissier ;
- la transaction unique : une panne injectée APRÈS que la pièce a été posée (même patron que
  test_guichet_concurrence.py::test_operation_interrompue_ne_laisse_rien) ne laisse rien à
  moitié — ni pièce orpheline, ni échéance marquée payée, ni registre de remboursement.
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.caisse.models import CaisseSession, Poste, PosteAssignation
from app.modules.caisse.service import ouvrir_session
from app.modules.credit import remboursement as remboursement_module
from app.modules.credit.decaissement import decaisser
from app.modules.credit.demandes import creer_demande, decider
from app.modules.credit.models import Application, Installment, Product, Repayment
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant
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


def _cid(db: Session, numero: str) -> uuid.UUID:
    return db.execute(
        text("SELECT id FROM comptabilite.accounts WHERE account_number = :n"), {"n": numero}
    ).scalar_one()


def _agence(db: Session, code: str) -> Agency:
    agence = Agency(code=code, name=f"Agence {code}", compte_caisse_id=_cid(db, "101111"))
    db.add(agence)
    db.flush()
    return agence


def _tier(db: Session, agence: Agency) -> uuid.UUID:
    tier_id = db.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES (:n, 'individual', :a, 'actif') RETURNING id"
        ),
        {"n": f"M-CRR-{uuid.uuid4().hex[:6]}", "a": agence.id},
    ).scalar_one()
    nat = db.execute(text("SELECT id FROM parameters.countries LIMIT 1")).scalar_one()
    db.execute(
        text(
            "INSERT INTO tiers.individual_profiles "
            "(tier_id, last_name, first_name, birth_date, gender, nationality_id) "
            "VALUES (:t, 'Traore', 'Awa', '1985-01-01', 'F', :nat)"
        ),
        {"t": tier_id, "nat": nat},
    )
    return tier_id


def _produit(db: Session, *, taux_bp: int = 1200, avec_compte_interets: bool = True) -> Product:
    produit = Product(
        code=f"CRR{uuid.uuid4().hex[:5]}",
        name="Crédit test remboursement",
        compte_credit_membre_id=_cid(db, "202211"),
        compte_credit_client_id=_cid(db, "202221"),
        compte_produits_interets_id=_cid(db, "7021") if avec_compte_interets else None,
        taux_bp=taux_bp,
        methode_amortissement="capital_constant",
    )
    db.add(produit)
    db.flush()
    return produit


def _entete(db: Session, agence: Agency, role_code: str) -> dict[str, str]:
    role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
    suffixe = uuid.uuid4().hex[:8]
    user = User(
        matricule=f"MAT-{suffixe}",
        email=f"{suffixe}@ex.com",
        username=f"u{suffixe}",
        password_hash=hasher_mot_de_passe("Motdepasse!123"),
        last_name="T",
        first_name="A",
        primary_agency_id=agence.id,
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.flush()
    jeton = creer_access_token(
        user_id=user.id, roles=[role_code], primary_agency_id=agence.id, agency_id=agence.id
    )
    return {"Authorization": f"Bearer {jeton}"}


def _ouvrir_session_caisse(db: Session, agence: Agency) -> uuid.UUID:
    """Poste + session de caisse OUVERTE pour un caissier fictif (Bloc C5) : le décaissement en
    mode 'caisse' (défaut) exige désormais SA session. Idempotent (même uid réutilisé par
    `security.users LIMIT 1`)."""
    uid = db.execute(text("SELECT id FROM security.users LIMIT 1")).scalar_one()
    deja_ouverte = db.execute(
        select(CaisseSession.id).where(
            CaisseSession.caissier_id == uid, CaisseSession.status == "ouverte"
        )
    ).first()
    if deja_ouverte is None:
        poste = Poste(
            agency_id=agence.id, code="01", libelle="Caisse principale",
            compte_caisse_id=agence.compte_caisse_id,
        )
        db.add(poste)
        db.flush()
        db.add(PosteAssignation(poste_id=poste.id, user_id=uid))
        db.flush()
        ouvrir_session(
            db,
            UtilisateurCourant(
                user_id=uid, roles=(), permissions=frozenset(),
                primary_agency_id=agence.id, agency_id=agence.id, voit_tout=True,
            ),
            poste_id=poste.id, fonds_initial=0,
        )
    return uid


def _demande_decaissee(
    db: Session,
    agence: Agency,
    tier_id: uuid.UUID,
    produit: Product,
    montant: int = 300000,
    duree_echeances: int = 6,
) -> Application:
    demande = creer_demande(
        db,
        tier_id=tier_id,
        agency_id=agence.id,
        product_id=produit.id,
        montant_demande=montant,
        duree_echeances=duree_echeances,
        objet="Test",
        par=None,
    )
    decider(db, demande, decision="approuve", montant_decide=montant, motif="OK", par=None)
    par = _ouvrir_session_caisse(db, agence)
    decaisser(db, demande, par=par)
    db.commit()
    return demande


# --- La ligne PRODUITS_INTERETS : présente si intérêts, omise sinon ---------------------------


def test_remboursement_pose_trois_lignes_quand_linteret_est_non_nul(
    client: TestClient, db: Session
) -> None:
    agence = _agence(db, "RMA1")
    tier_id = _tier(db, agence)
    produit = _produit(db, taux_bp=1200)
    demande = _demande_decaissee(db, agence, tier_id, produit)
    caissier = _entete(db, agence, "CAISSIER")

    premiere = db.execute(
        select(Installment)
        .where(Installment.application_id == demande.id)
        .order_by(Installment.numero)
        .limit(1)
    ).scalar_one()
    assert premiere.interets > 0  # capital_constant à taux non nul : la 1ère porte des intérêts

    reponse = client.post(
        f"/credit/demandes/{demande.id}/remboursement",
        json={"montant": premiere.total},
        headers=caissier,
    )

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["numero"] == 1
    assert corps["echeances_restantes"] == 5

    lignes = db.execute(
        text(
            "SELECT a.account_number, jl.side, jl.amount "
            "FROM comptabilite.journal_lines jl "
            "JOIN comptabilite.journal_entries je ON je.id = jl.entry_id "
            "JOIN comptabilite.accounts a ON a.id = jl.account_id "
            "WHERE je.description = :d ORDER BY jl.line_number"
        ),
        {"d": f"Remboursement crédit {demande.application_number} #1"},
    ).all()
    assert len(lignes) == 3
    caisse, credit_l, interets_l = lignes
    assert caisse.side == "D" and caisse.amount == premiere.total
    assert credit_l.account_number == "202221" and credit_l.side == "C"  # client (défaut _tier)
    assert credit_l.amount == premiere.capital
    assert interets_l.account_number == "7021" and interets_l.side == "C"
    assert interets_l.amount == premiere.interets

    echeance = db.get(Installment, premiere.id)
    assert echeance.status == "paye"
    assert echeance.paid_at is not None

    remboursement = db.execute(
        select(Repayment).where(Repayment.installment_id == premiere.id)
    ).scalar_one()
    assert remboursement.montant_capital == premiere.capital
    assert remboursement.montant_interets == premiere.interets
    assert remboursement.montant_total == premiere.total


def test_ligne_interets_omise_quand_le_taux_est_nul(client: TestClient, db: Session) -> None:
    agence = _agence(db, "RMA2")
    tier_id = _tier(db, agence)
    # Taux nul ET aucun compte de produits d'intérêts rattaché : si la ligne n'était pas omise,
    # ça lèverait RattachementManquantError. Le succès EST la preuve que la ligne est absente.
    produit = _produit(db, taux_bp=0, avec_compte_interets=False)
    demande = _demande_decaissee(db, agence, tier_id, produit)
    caissier = _entete(db, agence, "CAISSIER")

    premiere = db.execute(
        select(Installment)
        .where(Installment.application_id == demande.id)
        .order_by(Installment.numero)
        .limit(1)
    ).scalar_one()
    assert premiere.interets == 0

    reponse = client.post(
        f"/credit/demandes/{demande.id}/remboursement",
        json={"montant": premiere.total},
        headers=caissier,
    )

    assert reponse.status_code == 200
    lignes = db.execute(
        text(
            "SELECT jl.side FROM comptabilite.journal_lines jl "
            "JOIN comptabilite.journal_entries je ON je.id = jl.entry_id "
            "WHERE je.description = :d"
        ),
        {"d": f"Remboursement crédit {demande.application_number} #1"},
    ).all()
    assert len(lignes) == 2  # D CAISSE / C CREDIT — aucune ligne d'intérêts


# --- CR5d : les paramètres compte_source_id/journal_code ne changent RIEN au guichet -------


def test_rembourser_guichet_defaut_reste_caisse_journal_ca(client: TestClient, db: Session) -> None:
    """Non-régression EXPLICITE (voir credit/remboursement.py, section CR5d) : appelé SANS
    compte_source_id/journal_code (le seul chemin qu'emprunte le guichet CR6d, via l'endpoint
    HTTP), rembourser() pose TOUJOURS D le compte de caisse de l'agence, sur le journal CA —
    comme avant l'ajout de ces paramètres pour le prélèvement automatique (CR5d)."""
    agence = _agence(db, "RMA13")
    tier_id = _tier(db, agence)
    produit = _produit(db, taux_bp=1200)
    demande = _demande_decaissee(db, agence, tier_id, produit)
    caissier = _entete(db, agence, "CAISSIER")

    premiere = db.execute(
        select(Installment)
        .where(Installment.application_id == demande.id)
        .order_by(Installment.numero)
        .limit(1)
    ).scalar_one()

    reponse = client.post(
        f"/credit/demandes/{demande.id}/remboursement",
        json={"montant": premiere.total},
        headers=caissier,
    )
    assert reponse.status_code == 200

    ligne = db.execute(
        text(
            "SELECT j.code, a.account_number "
            "FROM comptabilite.journal_lines jl "
            "JOIN comptabilite.journal_entries je ON je.id = jl.entry_id "
            "JOIN comptabilite.journals j ON j.id = je.journal_id "
            "JOIN comptabilite.accounts a ON a.id = jl.account_id "
            "WHERE je.description = :d AND jl.side = 'D'"
        ),
        {"d": f"Remboursement crédit {demande.application_number} #1"},
    ).one()
    assert ligne.code == "CA"
    assert (
        ligne.account_number == "101111"
    )  # le compte de caisse de l'agence, pas un compte epargne


# --- Montant incorrect : message actionnable ---------------------------------------------


def test_montant_incorrect_indique_le_montant_attendu(client: TestClient, db: Session) -> None:
    """CR5b : un montant INFÉRIEUR au solde est désormais un versement partiel légitime (voir
    plus bas) — seul un DÉPASSEMENT du solde reste un refus, avec le solde nommé."""
    agence = _agence(db, "RMA3")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande = _demande_decaissee(db, agence, tier_id, produit)
    caissier = _entete(db, agence, "CAISSIER")

    premiere = db.execute(
        select(Installment)
        .where(Installment.application_id == demande.id)
        .order_by(Installment.numero)
        .limit(1)
    ).scalar_one()

    reponse = client.post(
        f"/credit/demandes/{demande.id}/remboursement",
        json={"montant": premiere.total + 500},
        headers=caissier,
    )

    assert reponse.status_code == 422
    assert reponse.json()["detail"] == (
        f"Le solde restant de cette échéance est de {premiere.total} F, "
        f"vous avez saisi {premiere.total + 500} F."
    )

    # Rien n'a bougé.
    echeance = db.get(Installment, premiere.id)
    assert echeance.status == "a_echoir"


def test_aucune_echeance_a_regler_si_pas_decaisse(client: TestClient, db: Session) -> None:
    agence = _agence(db, "RMA4")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande = creer_demande(
        db,
        tier_id=tier_id,
        agency_id=agence.id,
        product_id=produit.id,
        montant_demande=100000,
        duree_echeances=4,
        objet="Non décaissée",
        par=None,
    )
    db.commit()
    caissier = _entete(db, agence, "CAISSIER")

    reponse = client.post(
        f"/credit/demandes/{demande.id}/remboursement", json={"montant": 1000}, headers=caissier
    )

    assert reponse.status_code == 422
    assert "aucune échéance" in reponse.json()["detail"].lower()


def test_aucune_echeance_a_regler_si_deja_solde(client: TestClient, db: Session) -> None:
    agence = _agence(db, "RMA5")
    tier_id = _tier(db, agence)
    produit = _produit(db, taux_bp=0)
    demande = _demande_decaissee(db, agence, tier_id, produit, montant=60000, duree_echeances=2)
    caissier = _entete(db, agence, "CAISSIER")

    for _ in range(2):
        echeance = db.execute(
            select(Installment)
            .where(Installment.application_id == demande.id, Installment.status == "a_echoir")
            .order_by(Installment.numero)
            .limit(1)
        ).scalar_one()
        reponse = client.post(
            f"/credit/demandes/{demande.id}/remboursement",
            json={"montant": echeance.total},
            headers=caissier,
        )
        assert reponse.status_code == 200

    solde = client.post(
        f"/credit/demandes/{demande.id}/remboursement", json={"montant": 1000}, headers=caissier
    )
    assert solde.status_code == 422
    assert "soldé" in solde.json()["detail"].lower()


# --- CR5b : paiement partiel — garde-fou (a) -----------------------------------------------


def test_remboursement_partiel_bascule_partiellement_paye(client: TestClient, db: Session) -> None:
    agence = _agence(db, "RMA8")
    tier_id = _tier(db, agence)
    produit = _produit(db, taux_bp=1200)
    demande = _demande_decaissee(db, agence, tier_id, produit)
    caissier = _entete(db, agence, "CAISSIER")

    premiere = db.execute(
        select(Installment)
        .where(Installment.application_id == demande.id)
        .order_by(Installment.numero)
        .limit(1)
    ).scalar_one()
    assert premiere.interets > 0
    moitie = premiere.total // 2

    reponse = client.post(
        f"/credit/demandes/{demande.id}/remboursement", json={"montant": moitie}, headers=caissier
    )

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["montant_total"] == moitie
    assert corps["echeance_soldee"] is False
    assert corps["solde_du"] == premiere.total - moitie
    # Echéance TOUJOURS présente dans les restantes (ni soldée, ni disparue).
    assert corps["echeances_restantes"] == 6

    echeance = db.get(Installment, premiere.id)
    assert echeance.status == "partiellement_paye"
    assert echeance.montant_paye == moitie
    assert echeance.paid_at is None  # pas encore soldée


def test_remboursement_complete_apres_partiel_bascule_paye(client: TestClient, db: Session) -> None:
    agence = _agence(db, "RMA9")
    tier_id = _tier(db, agence)
    produit = _produit(db, taux_bp=1200)
    demande = _demande_decaissee(db, agence, tier_id, produit)
    caissier = _entete(db, agence, "CAISSIER")

    premiere = db.execute(
        select(Installment)
        .where(Installment.application_id == demande.id)
        .order_by(Installment.numero)
        .limit(1)
    ).scalar_one()
    moitie = premiere.total // 2
    reste = premiere.total - moitie

    client.post(
        f"/credit/demandes/{demande.id}/remboursement", json={"montant": moitie}, headers=caissier
    )
    reponse = client.post(
        f"/credit/demandes/{demande.id}/remboursement", json={"montant": reste}, headers=caissier
    )

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["echeance_soldee"] is True
    assert corps["solde_du"] == 0
    assert corps["echeances_restantes"] == 5

    echeance = db.get(Installment, premiere.id)
    assert echeance.status == "paye"
    assert echeance.montant_paye == premiere.total
    assert echeance.paid_at is not None

    # DEUX paiements distincts pour LA MÊME échéance — la contrainte UNIQUE est bien tombée.
    paiements = (
        db.execute(
            select(Repayment)
            .where(Repayment.installment_id == premiere.id)
            .order_by(Repayment.created_at)
        )
        .scalars()
        .all()
    )
    assert len(paiements) == 2
    assert paiements[0].montant_total == moitie
    assert paiements[1].montant_total == reste


def test_remboursement_depasse_le_solde_du_refuse_meme_sous_le_total(
    client: TestClient, db: Session
) -> None:
    """Le plafond est le SOLDE restant, pas le total d'origine : après un premier versement
    partiel, un second versement supérieur à ce qui reste (même inférieur au total complet)
    doit être refusé, avec le SOLDE nommé dans le message — pas le total."""
    agence = _agence(db, "RMA10")
    tier_id = _tier(db, agence)
    produit = _produit(db, taux_bp=1200)
    demande = _demande_decaissee(db, agence, tier_id, produit)
    caissier = _entete(db, agence, "CAISSIER")

    premiere = db.execute(
        select(Installment)
        .where(Installment.application_id == demande.id)
        .order_by(Installment.numero)
        .limit(1)
    ).scalar_one()
    # Verse 80% du total — il ne reste que 20%.
    premier_versement = (premiere.total * 8) // 10
    solde_restant = premiere.total - premier_versement
    tentative = solde_restant + 1000  # dépasse le solde, reste < total d'origine

    client.post(
        f"/credit/demandes/{demande.id}/remboursement",
        json={"montant": premier_versement},
        headers=caissier,
    )
    reponse = client.post(
        f"/credit/demandes/{demande.id}/remboursement",
        json={"montant": tentative},
        headers=caissier,
    )

    assert reponse.status_code == 422
    assert reponse.json()["detail"] == (
        f"Le solde restant de cette échéance est de {solde_restant} F, "
        f"vous avez saisi {tentative} F."
    )
    # Rien n'a bougé depuis le premier versement.
    echeance = db.get(Installment, premiere.id)
    assert echeance.montant_paye == premier_versement
    assert echeance.status == "partiellement_paye"


def test_ventilation_interets_dabord_puis_capital(client: TestClient, db: Session) -> None:
    """Convention PROVISOIRE (à valider) : un versement partiel couvre d'abord les intérêts,
    puis le capital avec le reliquat — déduit de montant_paye, aucune colonne dédiée."""
    agence = _agence(db, "RMA11")
    tier_id = _tier(db, agence)
    produit = _produit(db, taux_bp=1200)
    demande = _demande_decaissee(db, agence, tier_id, produit)
    caissier = _entete(db, agence, "CAISSIER")

    premiere = db.execute(
        select(Installment)
        .where(Installment.application_id == demande.id)
        .order_by(Installment.numero)
        .limit(1)
    ).scalar_one()
    assert premiere.interets > 0 and premiere.capital > 0

    # Premier versement : moins que les intérêts seuls -> tout part en intérêts, rien au capital.
    petit_versement = max(1, premiere.interets - 1)
    reponse1 = client.post(
        f"/credit/demandes/{demande.id}/remboursement",
        json={"montant": petit_versement},
        headers=caissier,
    )
    assert reponse1.status_code == 200
    assert reponse1.json()["interets"] == petit_versement
    assert reponse1.json()["capital"] == 0

    # Deuxième versement : le reliquat d'intérêts, puis du capital.
    reste_interets = premiere.interets - petit_versement
    deuxieme_versement = reste_interets + 5000
    reponse2 = client.post(
        f"/credit/demandes/{demande.id}/remboursement",
        json={"montant": deuxieme_versement},
        headers=caissier,
    )
    assert reponse2.status_code == 200
    assert reponse2.json()["interets"] == reste_interets
    assert reponse2.json()["capital"] == 5000


def test_echeancier_expose_montant_paye_et_solde_du_apres_versement_partiel(
    client: TestClient, db: Session
) -> None:
    """CR5b : l'échéancier PERSISTÉ (/echeancier, consulté depuis la fiche dossier) doit,
    lui aussi, refléter un versement partiel — pas seulement le guichet de recherche. Le
    statut seul ('partiellement_paye') ne dit pas ce qui reste dû."""
    agence = _agence(db, "RMA12")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande = _demande_decaissee(db, agence, tier_id, produit)
    caissier = _entete(db, agence, "CAISSIER")
    responsable = _entete(db, agence, "RESPONSABLE_AGENCE")

    premiere = db.execute(
        select(Installment)
        .where(Installment.application_id == demande.id)
        .order_by(Installment.numero)
        .limit(1)
    ).scalar_one()
    versement = premiere.total - 1000

    reponse = client.post(
        f"/credit/demandes/{demande.id}/remboursement",
        json={"montant": versement},
        headers=caissier,
    )
    assert reponse.status_code == 200

    lecture = client.get(f"/credit/demandes/{demande.id}/echeancier", headers=responsable)
    assert lecture.status_code == 200
    premiere_ligne = lecture.json()[0]
    assert premiere_ligne["status"] == "partiellement_paye"
    assert premiere_ligne["montant_paye"] == versement
    assert premiere_ligne["solde_du"] == 1000


# --- Transaction unique : panne après la pièce, rien ne subsiste --------------------------


def test_transaction_interrompue_apres_la_piece_ne_laisse_rien(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Même patron que test_guichet_concurrence.py::test_operation_interrompue_ne_laisse_rien :
    panne injectée en toute fin (audit), APRÈS que la pièce comptable et le passage 'paye' ont
    été flush — la transaction unique doit tout défaire d'un bloc."""
    agence = _agence(db, "RMA6")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande = _demande_decaissee(db, agence, tier_id, produit)

    premiere = db.execute(
        select(Installment)
        .where(Installment.application_id == demande.id)
        .order_by(Installment.numero)
        .limit(1)
    ).scalar_one()

    def panne(*args: object, **kwargs: object) -> None:
        raise RuntimeError("panne au milieu du remboursement")

    monkeypatch.setattr(remboursement_module, "ecrire_audit", panne)

    entrees_avant = db.execute(
        text("SELECT count(*) FROM comptabilite.journal_entries")
    ).scalar_one()

    with pytest.raises(RuntimeError):
        remboursement_module.rembourser(db, demande, montant=premiere.total, par=None)
    db.rollback()

    entrees_apres = db.execute(
        text("SELECT count(*) FROM comptabilite.journal_entries")
    ).scalar_one()
    assert entrees_apres == entrees_avant

    echeance = db.get(Installment, premiere.id)
    assert echeance.status == "a_echoir"
    assert echeance.paid_at is None

    aucun_remboursement = db.execute(
        select(Repayment).where(Repayment.installment_id == premiere.id)
    ).first()
    assert aucun_remboursement is None


# --- Permissions -----------------------------------------------------------------------------


def test_charge_pret_ne_peut_pas_encaisser_un_remboursement(
    client: TestClient, db: Session
) -> None:
    agence = _agence(db, "RMA7")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande = _demande_decaissee(db, agence, tier_id, produit)
    charge = _entete(db, agence, "CHARGE_PRET")

    reponse = client.post(
        f"/credit/demandes/{demande.id}/remboursement", json={"montant": 1000}, headers=charge
    )

    assert reponse.status_code == 403
