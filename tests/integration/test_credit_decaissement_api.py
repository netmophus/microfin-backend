"""API Crédit CR3 — décaissement : pièce comptable, échéancier persisté, ancrage du routage.

  - le double gate KYC (service + trigger) MORD réellement, comme CR1 : tiers suspendu entre
    l'approbation et le décaissement -> refus service (422) ET refus trigger (insertion brute) ;
  - le routage membre/client est résolu et FIGÉ dans compte_credit_id, jamais re-routé ;
  - la TRANSACTION UNIQUE : un échéancier impossible après que la pièce a été posée ne laisse
    RIEN persister (ni écriture, ni échéance, ni statut décaissé) ;
  - permissions : RESPONSABLE_AGENCE décaisse, CHARGE_PRET (qui a monté le dossier) ne peut pas
    (séparation des tâches).
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.caisse.models import Poste, PosteAssignation
from app.modules.caisse.service import ouvrir_session
from app.modules.credit.demandes import creer_demande, decider
from app.modules.credit.models import Application, Installment, Product
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.security.jwt import creer_access_token, decoder_access_token
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


def _tier(
    db: Session, agence: Agency, statut: str = "actif", est_membre: bool = True
) -> uuid.UUID:
    tier_id = db.execute(
        text(
            "INSERT INTO tiers.tiers "
            "(tier_number, tier_type, primary_agency_id, status, is_member) "
            "VALUES (:n, 'individual', :a, :s, :m) RETURNING id"
        ),
        {"n": f"M-CRD-{uuid.uuid4().hex[:6]}", "a": agence.id, "s": statut, "m": est_membre},
    ).scalar_one()
    nat = db.execute(text("SELECT id FROM parameters.countries LIMIT 1")).scalar_one()
    db.execute(
        text(
            "INSERT INTO tiers.individual_profiles "
            "(tier_id, last_name, first_name, birth_date, gender, nationality_id) "
            "VALUES (:t, 'Kone', 'Fatou', '1985-01-01', 'F', :nat)"
        ),
        {"t": tier_id, "nat": nat},
    )
    return tier_id


def _produit(db: Session, **kwargs: object) -> Product:
    produit = Product(
        code=f"CRT{uuid.uuid4().hex[:5]}",
        name="Crédit test",
        compte_credit_membre_id=_cid(db, "202211"),
        compte_credit_client_id=_cid(db, "202221"),
        taux_bp=1200,
        **kwargs,
    )
    db.add(produit)
    db.flush()
    return produit


def _entete(db: Session, agence: Agency, role_code: str) -> dict[str, str]:
    role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
    suffixe = uuid.uuid4().hex[:8]
    user = User(
        matricule=f"MAT-{suffixe}", email=f"{suffixe}@ex.com", username=f"u{suffixe}",
        password_hash=hasher_mot_de_passe("Motdepasse!123"), last_name="T", first_name="A",
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


def _ouvrir_session_caisse(db: Session, agence: Agency, entete: dict[str, str]) -> None:
    """Poste + session de caisse OUVERTE pour l'acteur de `entete` (Bloc C5) : le décaissement
    en mode 'caisse' (défaut) exige désormais SA session, plus la seule agence."""
    jeton = entete["Authorization"].removeprefix("Bearer ")
    user_id = decoder_access_token(jeton).sub
    poste = Poste(
        agency_id=agence.id, code="01", libelle="Caisse principale",
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


def _demande_approuvee(
    db: Session,
    agence: Agency,
    tier_id: uuid.UUID,
    produit: Product,
    montant_demande: int = 500000,
    montant_decide: int | None = None,
    duree_echeances: int = 12,
) -> uuid.UUID:
    demande = creer_demande(
        db,
        tier_id=tier_id,
        agency_id=agence.id,
        product_id=produit.id,
        montant_demande=montant_demande,
        duree_echeances=duree_echeances,
        objet="Test décaissement",
        par=None,
    )
    decider(
        db,
        demande,
        decision="approuve",
        montant_decide=montant_decide or montant_demande,
        motif="Dossier complet",
        par=None,
    )
    db.commit()
    return demande.id


# --- Décaissement réussi : la pièce, l'échéancier, l'ancrage ---------------------------------


def test_decaissement_pose_la_piece_et_persiste_lecheancier(
    client: TestClient, db: Session
) -> None:
    agence = _agence(db, "DCA1")
    tier_id = _tier(db, agence, est_membre=True)
    produit = _produit(db)
    demande_id = _demande_approuvee(db, agence, tier_id, produit, montant_demande=300000)
    responsable = _entete(db, agence, "RESPONSABLE_AGENCE")
    _ouvrir_session_caisse(db, agence, responsable)

    reponse = client.post(f"/credit/demandes/{demande_id}/decaissement", headers=responsable)

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["status"] == "decaisse"
    assert corps["disbursed_at"] is not None
    assert corps["compte_credit_number"] == "202211"  # membre
    assert corps["nb_echeances"] == 12
    assert corps["premiere_echeance_le"] is not None
    assert corps["derniere_echeance_le"] is not None

    # La pièce comptable est réellement posée et équilibrée.
    total_d, total_c = db.execute(
        text(
            "SELECT "
            "COALESCE(SUM(amount) FILTER (WHERE side='D'), 0), "
            "COALESCE(SUM(amount) FILTER (WHERE side='C'), 0) "
            "FROM comptabilite.journal_lines jl "
            "JOIN comptabilite.journal_entries je ON je.id = jl.entry_id "
            "WHERE je.description = :d"
        ),
        {"d": f"Décaissement crédit {corps['application_number']}"},
    ).one()
    assert total_d == total_c == 300000

    # L'échéancier persisté reconstitue exactement le montant décaissé.
    echeances = db.execute(
        select(Installment).where(Installment.application_id == demande_id)
    ).scalars().all()
    assert len(echeances) == 12
    assert sum(e.capital for e in echeances) == 300000
    assert echeances[-1].capital_restant_du == 0

    # Endpoint de lecture de l'échéancier.
    lecture = client.get(f"/credit/demandes/{demande_id}/echeancier", headers=responsable)
    assert lecture.status_code == 200
    assert len(lecture.json()) == 12
    assert lecture.json()[0]["status"] == "a_echoir"


def test_routage_client_utilise_le_compte_client(client: TestClient, db: Session) -> None:
    agence = _agence(db, "DCA2")
    tier_id = _tier(db, agence, est_membre=False)  # CLIENT, pas membre
    produit = _produit(db)
    demande_id = _demande_approuvee(db, agence, tier_id, produit)
    responsable = _entete(db, agence, "RESPONSABLE_AGENCE")
    _ouvrir_session_caisse(db, agence, responsable)

    reponse = client.post(f"/credit/demandes/{demande_id}/decaissement", headers=responsable)

    assert reponse.status_code == 200
    assert reponse.json()["compte_credit_number"] == "202221"  # client


def test_le_routage_reste_fige_meme_si_le_statut_membre_change_ensuite(
    client: TestClient, db: Session
) -> None:
    """L'ancrage : compte_credit_id est résolu UNE FOIS au décaissement, jamais re-routé —
    même si is_member change sur le tiers après coup."""
    agence = _agence(db, "DCA3")
    tier_id = _tier(db, agence, est_membre=True)
    produit = _produit(db)
    demande_id = _demande_approuvee(db, agence, tier_id, produit)
    responsable = _entete(db, agence, "RESPONSABLE_AGENCE")
    _ouvrir_session_caisse(db, agence, responsable)

    reponse = client.post(f"/credit/demandes/{demande_id}/decaissement", headers=responsable)
    assert reponse.json()["compte_credit_number"] == "202211"

    # Le tiers "devient" client après coup (mutation directe, hors service — juste pour la preuve).
    db.execute(text("UPDATE tiers.tiers SET is_member = FALSE WHERE id = :t"), {"t": tier_id})
    db.commit()

    demande = db.get(Application, demande_id)
    compte_number = db.execute(
        text("SELECT account_number FROM comptabilite.accounts WHERE id = :i"),
        {"i": demande.compte_credit_id},
    ).scalar_one()
    assert compte_number == "202211"  # inchangé : l'ancrage ne se re-dérive jamais


# --- Le double gate KYC : service ET trigger --------------------------------------------------


def test_decaissement_refuse_si_tiers_suspendu_entre_lapprobation_et_le_decaissement(
    client: TestClient, db: Session
) -> None:
    agence = _agence(db, "DCA4")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande_id = _demande_approuvee(db, agence, tier_id, produit)

    db.execute(
        text("UPDATE tiers.tiers SET status = 'suspendu_temporaire' WHERE id = :t"), {"t": tier_id}
    )
    db.commit()

    responsable = _entete(db, agence, "RESPONSABLE_AGENCE")
    reponse = client.post(f"/credit/demandes/{demande_id}/decaissement", headers=responsable)

    assert reponse.status_code == 422
    assert "actif" in reponse.json()["detail"].lower()

    # Rien n'a bougé : la demande reste 'approuve', aucune pièce, aucune échéance.
    demande = db.get(Application, demande_id)
    assert demande.status == "approuve"
    assert db.execute(
        select(Installment).where(Installment.application_id == demande_id)
    ).first() is None


def test_trigger_decaissement_mord_reellement_pas_seulement_le_service(db: Session) -> None:
    """LE garde-fou en base (dernier rempart) refuse une transition brute vers 'decaisse',
    même en contournant le service — pas un mécanisme vert mais inerte."""
    agence = _agence(db, "DCA5")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande_id = _demande_approuvee(db, agence, tier_id, produit)

    db.execute(
        text("UPDATE tiers.tiers SET status = 'suspendu_temporaire' WHERE id = :t"), {"t": tier_id}
    )
    db.flush()

    with pytest.raises(DBAPIError):
        db.execute(
            text("UPDATE credit.applications SET status = 'decaisse' WHERE id = :a"),
            {"a": demande_id},
        )


# --- États refusés ------------------------------------------------------------------------


def test_decaissement_refuse_si_demande_pas_encore_approuvee(
    client: TestClient, db: Session
) -> None:
    agence = _agence(db, "DCA6")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande = creer_demande(
        db, tier_id=tier_id, agency_id=agence.id, product_id=produit.id,
        montant_demande=200000, duree_echeances=6, objet="Non décidée", par=None,
    )
    db.commit()
    responsable = _entete(db, agence, "RESPONSABLE_AGENCE")

    reponse = client.post(f"/credit/demandes/{demande.id}/decaissement", headers=responsable)

    assert reponse.status_code == 422
    assert "approuv" in reponse.json()["detail"].lower()


def test_decaissement_deux_fois_refuse(client: TestClient, db: Session) -> None:
    agence = _agence(db, "DCA7")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande_id = _demande_approuvee(db, agence, tier_id, produit)
    responsable = _entete(db, agence, "RESPONSABLE_AGENCE")
    _ouvrir_session_caisse(db, agence, responsable)

    premier = client.post(f"/credit/demandes/{demande_id}/decaissement", headers=responsable)
    assert premier.status_code == 200

    second = client.post(f"/credit/demandes/{demande_id}/decaissement", headers=responsable)
    assert second.status_code == 422
    assert "approuv" in second.json()["detail"].lower()


# --- Transaction unique : rien ne reste à moitié ------------------------------------------


def test_echeancier_impossible_ne_laisse_rien_persister(client: TestClient, db: Session) -> None:
    """Force un échec APRÈS que la pièce comptable a été posée (EcheancierImpossibleError de
    CR2, déclenchée par montant/durée pathologiques) : la transaction unique doit tout défaire
    — pas de pièce comptable orpheline, pas d'échéance, statut resté 'approuve'."""
    agence = _agence(db, "DCA8")
    tier_id = _tier(db, agence)
    produit = _produit(db, methode_amortissement="capital_constant", regle_arrondi="plus_proche")
    # montant=5, durée=10 : capital de base arrondit à 1 (plus proche), 1x9=9 > 5 -> impossible.
    demande_id = _demande_approuvee(
        db, agence, tier_id, produit, montant_demande=5, montant_decide=5, duree_echeances=10
    )
    responsable = _entete(db, agence, "RESPONSABLE_AGENCE")
    _ouvrir_session_caisse(db, agence, responsable)

    reponse = client.post(f"/credit/demandes/{demande_id}/decaissement", headers=responsable)

    assert reponse.status_code == 422
    detail = reponse.json()["detail"].lower()
    assert "impossible" in detail or "négatif" in detail or "capital" in detail

    demande = db.get(Application, demande_id)
    assert demande.status == "approuve"
    assert demande.disbursed_at is None
    assert demande.compte_credit_id is None

    aucune_echeance = db.execute(
        select(Installment).where(Installment.application_id == demande_id)
    ).first()
    assert aucune_echeance is None

    aucune_piece = db.execute(
        text(
            "SELECT 1 FROM comptabilite.journal_entries "
            "WHERE description LIKE :d"
        ),
        {"d": f"%{demande.application_number}%"},
    ).first()
    assert aucune_piece is None


# --- Permissions : séparation des tâches ---------------------------------------------------


def test_charge_pret_ne_peut_pas_decaisser(client: TestClient, db: Session) -> None:
    agence = _agence(db, "DCA9")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande_id = _demande_approuvee(db, agence, tier_id, produit)
    charge = _entete(db, agence, "CHARGE_PRET")  # a monté le dossier, ne décaisse pas

    reponse = client.post(f"/credit/demandes/{demande_id}/decaissement", headers=charge)

    assert reponse.status_code == 403


def test_responsable_agence_peut_decaisser_hors_perimetre_404(
    client: TestClient, db: Session
) -> None:
    agence = _agence(db, "DCA10")
    autre_agence = _agence(db, "DCA11")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande_id = _demande_approuvee(db, agence, tier_id, produit)
    responsable_autre = _entete(db, autre_agence, "RESPONSABLE_AGENCE")

    reponse = client.post(f"/credit/demandes/{demande_id}/decaissement", headers=responsable_autre)

    assert reponse.status_code == 404
