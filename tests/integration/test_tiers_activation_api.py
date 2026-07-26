"""Gate d'activation réel (T3c) — le jalon : une fiche passe de prospect à ACTIF pour de vrai.

  1. fiche complète → activation réussie (prospect → actif) + lien vers l'évaluation KYC ;
  2. fiche incomplète → 412 listant TOUT ce qui manque d'un bloc ;
  3. quatre-yeux : bloqué par défaut (activateur == vérificateur), toléré ET tracé si l'agence
     est assouplie ;
  4. routage risque élevé → LBC/FT (le responsable d'agence est refusé) ;
  5. saisie KYC (PATCH) qui complète une fiche et recalcule le risque.
"""

import uuid
from collections.abc import Generator
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.parameters.models import Agency
from app.modules.security.jwt import creer_access_token
from app.modules.security.models import Role, User, UserRole
from app.modules.security.password import hasher_mot_de_passe
from app.modules.tiers.models import Contact, IdentityDocument, IndividualProfile

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


def _agence(db: Session, nom: str = "Agence Centre") -> Agency:
    agence = Agency(code=f"AG-{uuid.uuid4().hex[:6]}", name=nom)
    db.add(agence)
    db.flush()
    return agence


def _ref(db: Session, sql: str, code: str) -> uuid.UUID:
    return db.execute(text(sql), {"c": code}).scalar_one()


def _pays(db: Session, code: str) -> uuid.UUID:
    return _ref(db, "SELECT id FROM parameters.countries WHERE code = :c", code)


def _secteur(db: Session, code: str) -> uuid.UUID:
    return _ref(db, "SELECT id FROM parameters.secteurs_activite WHERE code = :c", code)


def _type_piece(db: Session, code: str) -> uuid.UUID:
    return _ref(db, "SELECT id FROM parameters.identity_document_types WHERE code = :c", code)


def _utilisateur(db: Session, role_code: str, agence: Agency) -> User:
    role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
    s = uuid.uuid4().hex[:8]
    user = User(
        matricule=f"MAT-{s}",
        email=f"{s}@example.com",
        username=f"u{s}",
        password_hash=hasher_mot_de_passe("Motdepasse!123"),
        last_name="Test",
        first_name="Agent",
        primary_agency_id=agence.id,
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.flush()
    return user


def _entete(user: User, role_code: str) -> dict[str, str]:
    roles = [role_code]
    jeton = creer_access_token(
        user_id=user.id, roles=roles, primary_agency_id=user.primary_agency_id
    )
    return {"Authorization": f"Bearer {jeton}"}


def _prospect_nu(db: Session, agence: Agency) -> IndividualProfile:
    """Prospect SANS pièce ni contact ni KYC — sert au cas « incomplet »."""
    tier = IndividualProfile(
        tier_number=f"M-2999-{uuid.uuid4().int % 10_000_000:07d}",
        primary_agency_id=agence.id,
        last_name="Diallo",
        first_name="Amadou",
        birth_date=date(1990, 5, 12),
        gender="M",
        nationality_id=_pays(db, "SN"),
    )
    db.add(tier)
    db.flush()
    return tier


def _completer(
    db: Session,
    tier: IndividualProfile,
    verificateur: User,
    *,
    secteur_code: str = "AGRICULTURE",
    ppe: bool = False,
) -> None:
    """Rend une fiche activable : pièce vérifiée valide, téléphone, adresse, données KYC."""
    tier.secteur_activite_id = _secteur(db, secteur_code)
    tier.origine_fonds = "Salaire"
    tier.ppe_status = ppe
    if ppe:
        tier.ppe_relation = "direct"
        tier.ppe_fonction = "Maire"
    tier.mode_entree_relation = "presentiel"
    db.add(
        IdentityDocument(
            tier_id=tier.id,
            document_type_id=_type_piece(db, "CNI"),
            document_number="NER-100",
            document_number_normalized="NER-100",
            expiry_date=date(2035, 1, 1),  # valide
            is_primary=True,
            is_verified=True,
            verified_at=datetime.now(UTC),
            verified_by=verificateur.id,
        )
    )
    db.add(
        Contact(
            tier_id=tier.id, contact_type="phone", phone_number="+22790123456", is_primary=True
        )
    )
    db.add(Contact(tier_id=tier.id, contact_type="address", landmark="Derrière la mosquée"))
    db.flush()


# --- cas 1 : le jalon --------------------------------------------------------------------


def test_fiche_complete_prospect_devient_actif(db: Session, client: TestClient) -> None:
    agence = _agence(db)
    verif = _utilisateur(db, "RESPONSABLE_AGENCE", agence)
    activateur = _utilisateur(db, "RESPONSABLE_AGENCE", agence)  # ≠ vérificateur (quatre-yeux OK)
    tier = _prospect_nu(db, agence)
    _completer(db, tier, verif)

    reponse = client.post(
        f"/tiers/{tier.id}/activate", headers=_entete(activateur, "RESPONSABLE_AGENCE")
    )
    assert reponse.status_code == 200, reponse.text
    assert reponse.json()["status"] == "actif"  # LE JALON

    # Traçabilité : la fiche pointe l'évaluation KYC EXACTE sur laquelle l'activation s'est appuyée.
    ligne = db.execute(
        text(
            "SELECT status, activated_by, activation_assessment_id "
            "FROM tiers.tiers WHERE id = :t"
        ),
        {"t": tier.id},
    ).one()
    assert ligne.status == "actif"
    assert ligne.activated_by == activateur.id
    assert ligne.activation_assessment_id is not None
    ev = db.execute(
        text("SELECT trigger_event FROM tiers.risk_assessments WHERE id = :a"),
        {"a": ligne.activation_assessment_id},
    ).one()
    assert ev.trigger_event == "activation"


# --- cas 2 : fiche incomplète → tout ce qui manque d'un coup ----------------------------


def test_fiche_incomplete_refuse_en_listant_tout(db: Session, client: TestClient) -> None:
    agence = _agence(db)
    resp = _utilisateur(db, "RESPONSABLE_AGENCE", agence)
    tier = _prospect_nu(db, agence)  # rien de rempli

    reponse = client.post(f"/tiers/{tier.id}/activate", headers=_entete(resp, "RESPONSABLE_AGENCE"))
    assert reponse.status_code == 412, reponse.text
    codes = {c["code"] for c in reponse.json()["detail"]["conditions_manquantes"]}
    # TOUT est remonté d'un bloc, pas une condition à la fois.
    assert {
        "PIECE_PRINCIPALE_MANQUANTE",
        "TELEPHONE_MANQUANT",
        "ADRESSE_MANQUANTE",
        "ORIGINE_FONDS_MANQUANTE",
        "SECTEUR_MANQUANT",
        "MODE_ENTREE_MANQUANT",
    } <= codes


# --- cas 3 : quatre-yeux -----------------------------------------------------------------


def test_quatre_yeux_bloque_l_auto_validation_par_defaut(db: Session, client: TestClient) -> None:
    agence = _agence(db)  # double_validation_kyc = TRUE par défaut
    resp = _utilisateur(db, "RESPONSABLE_AGENCE", agence)
    tier = _prospect_nu(db, agence)
    _completer(db, tier, resp)  # le vérificateur EST l'activateur

    reponse = client.post(f"/tiers/{tier.id}/activate", headers=_entete(resp, "RESPONSABLE_AGENCE"))
    assert reponse.status_code == 412, reponse.text
    codes = {c["code"] for c in reponse.json()["detail"]["conditions_manquantes"]}
    assert "AUTO_VALIDATION_INTERDITE" in codes


def test_agence_assouplie_tolere_et_trace_auto_validation(db: Session, client: TestClient) -> None:
    agence = _agence(db)
    agence.double_validation_kyc = False  # assouplie (petite agence à agent unique)
    resp = _utilisateur(db, "RESPONSABLE_AGENCE", agence)
    tier = _prospect_nu(db, agence)
    _completer(db, tier, resp)  # même personne vérifie et active
    db.flush()

    reponse = client.post(f"/tiers/{tier.id}/activate", headers=_entete(resp, "RESPONSABLE_AGENCE"))
    assert reponse.status_code == 200, reponse.text

    # L'auto-validation est TRACÉE dans l'audit — un contrôleur la voit.
    audit = db.execute(
        text(
            "SELECT new_values FROM audit.audit_logs "
            "WHERE resource_id = :t AND action = 'tier.activated'"
        ),
        {"t": tier.id},
    ).one()
    assert audit.new_values["auto_validation"] is True


# --- cas 4 : routage risque élevé → LBC/FT ---------------------------------------------


def test_risque_eleve_refuse_au_responsable_accepte_au_lbcft(
    db: Session, client: TestClient
) -> None:
    agence = _agence(db)
    verif = _utilisateur(db, "RESPONSABLE_AGENCE", agence)
    resp = _utilisateur(db, "RESPONSABLE_AGENCE", agence)
    lbc = _utilisateur(db, "RESPONSABLE_LBC_FT", agence)
    tier = _prospect_nu(db, agence)
    _completer(db, tier, verif, secteur_code="METAUX_PRECIEUX", ppe=True)  # → risque élevé

    # Le responsable d'agence ne peut pas valider un profil élevé.
    r1 = client.post(f"/tiers/{tier.id}/activate", headers=_entete(resp, "RESPONSABLE_AGENCE"))
    assert r1.status_code == 412, r1.text
    codes = {c["code"] for c in r1.json()["detail"]["conditions_manquantes"]}
    assert "VALIDEUR_INSUFFISANT" in codes

    # Le LBC/FT, oui.
    r2 = client.post(f"/tiers/{tier.id}/activate", headers=_entete(lbc, "RESPONSABLE_LBC_FT"))
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "actif"


# --- le bandeau : conditions de dossier, avant le clic « Activer » ---------------------


def test_conditions_activation_liste_ce_qui_reste_a_completer(
    db: Session, client: TestClient
) -> None:
    agence = _agence(db)
    resp = _utilisateur(db, "RESPONSABLE_AGENCE", agence)
    tier = _prospect_nu(db, agence)  # rien de rempli

    r = client.get(
        f"/tiers/{tier.id}/activation-conditions", headers=_entete(resp, "RESPONSABLE_AGENCE")
    )
    assert r.status_code == 200, r.text
    corps = r.json()
    assert corps["activable"] is False
    codes = {c["code"] for c in corps["conditions"]}
    assert "PIECE_PRINCIPALE_MANQUANTE" in codes
    assert "ORIGINE_FONDS_MANQUANTE" in codes


def test_conditions_activation_activable_quand_dossier_complet(
    db: Session, client: TestClient
) -> None:
    agence = _agence(db)
    verif = _utilisateur(db, "RESPONSABLE_AGENCE", agence)
    resp = _utilisateur(db, "RESPONSABLE_AGENCE", agence)
    tier = _prospect_nu(db, agence)
    _completer(db, tier, verif)

    r = client.get(
        f"/tiers/{tier.id}/activation-conditions", headers=_entete(resp, "RESPONSABLE_AGENCE")
    )
    assert r.json()["activable"] is True
    assert r.json()["conditions"] == []


# --- cas 5 : saisie KYC + recalcul -----------------------------------------------------


def test_maj_kyc_renseigne_et_recalcule(db: Session, client: TestClient) -> None:
    agence = _agence(db)
    user = _utilisateur(db, "CHARGE_CLIENTELE", agence)
    tier = _prospect_nu(db, agence)

    reponse = client.patch(
        f"/tiers/{tier.id}/kyc",
        json={
            "origine_fonds": "Commerce",
            "secteur_activite_id": str(_secteur(db, "METAUX_PRECIEUX")),
            "ppe_status": True,
            "ppe_relation": "direct",
            "ppe_fonction": "Député",
            "mode_entree_relation": "presentiel",
        },
        headers=_entete(user, "CHARGE_CLIENTELE"),
    )
    assert reponse.status_code == 200, reponse.text

    # Le risque a été recalculé et archivé (declencheur maj_kyc), reflet à jour.
    ev = db.execute(
        text(
            "SELECT niveau_effectif, trigger_event FROM tiers.risk_assessments "
            "WHERE tier_id = :t ORDER BY assessed_at DESC LIMIT 1"
        ),
        {"t": tier.id},
    ).one()
    assert ev.trigger_event == "maj_kyc"
    assert ev.niveau_effectif == "eleve"  # secteur à risque (30 → moyen) + plancher PPE
    db.refresh(tier)
    assert tier.risk_level == "eleve"
