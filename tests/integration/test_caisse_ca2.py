"""Caisse CA2 — seuil de tolérance, motif obligatoire au-delà, validation a posteriori.

NE BLOQUE JAMAIS la fermeture (décision actée dès l'analyse initiale) : ces tests prouvent que
le motif est EXIGÉ au-delà du seuil, jamais que la fermeture est empêchée. Le statut « à
valider » n'existe dans AUCUNE colonne — il est DÉRIVÉ (`session_a_valider`), et ces tests le
prouvent en le faisant varier : un changement de seuil, une validation, doivent immédiatement
changer ce qui apparaît dans la liste, sans purge ni recalcul de rien.

La lettre de demande d'explication (manquant, sans seuil — voir `lister_sessions_manquantes`)
reste VOLONTAIREMENT indépendante du seuil CA2 : un test le prouve explicitement (régression).
"""

import uuid
from collections.abc import Generator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.caisse.models import CaisseParametres, CaisseSession, Poste, PosteAssignation
from app.modules.caisse.parametres import lire as lire_parametres
from app.modules.caisse.parametres import modifier as modifier_parametres
from app.modules.caisse.service import (
    EcartNonSignificatifError,
    MotifRequisError,
    SessionDejaValideeError,
    SessionIntrouvableError,
    fermer_session,
    lister_sessions_a_valider,
    lister_sessions_manquantes,
    ouvrir_session,
    session_a_valider,
    valider_ecart,
)
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
    compte_id = _cid(db, "101111")
    agence = Agency(code=code, name=f"Agence {code}", compte_caisse_id=compte_id)
    db.add(agence)
    db.flush()
    db.add(
        Poste(
            agency_id=agence.id, code="01", libelle="Caisse principale",
            compte_caisse_id=compte_id,
        )
    )
    db.flush()
    return agence


def _utilisateur(db: Session, agence: Agency, suffixe: str, role_code: str = "CAISSIER") -> User:
    role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
    user = User(
        matricule=f"MAT-CA2-{suffixe}", email=f"ca2{suffixe}@ex.com", username=f"ca2{suffixe}",
        password_hash=hasher_mot_de_passe("Motdepasse!123"),
        last_name="Test", first_name=suffixe,
        primary_agency_id=agence.id,
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.flush()
    return user


def _poste_principal(db: Session, agence: Agency) -> Poste:
    return db.execute(
        select(Poste).where(Poste.agency_id == agence.id, Poste.code == "01")
    ).scalar_one()


def _courant(user: User, agence: Agency, role_code: str = "CAISSIER") -> UtilisateurCourant:
    permissions = {
        "CAISSIER": frozenset(
            {"caisse.session.open", "caisse.session.close", "caisse.session.read"}
        ),
        "RESPONSABLE_AGENCE": frozenset(
            {"caisse.session.read.autres", "caisse.session.valider", "compta.plan.manage"}
        ),
    }[role_code]
    return UtilisateurCourant(
        user_id=user.id, roles=(role_code,), permissions=permissions,
        primary_agency_id=agence.id, agency_id=agence.id, voit_tout=False,
    )


def _entete(user: User, agence: Agency, role_code: str) -> dict[str, str]:
    jeton = creer_access_token(
        user_id=user.id, roles=[role_code], primary_agency_id=agence.id, agency_id=agence.id
    )
    return {"Authorization": f"Bearer {jeton}"}


def _ouvrir(db: Session, courant: UtilisateurCourant, agence: Agency, *, fonds_initial: int = 0):
    poste = _poste_principal(db, agence)
    deja = db.execute(
        select(PosteAssignation).where(
            PosteAssignation.poste_id == poste.id, PosteAssignation.user_id == courant.user_id
        )
    ).scalar_one_or_none()
    if deja is None:
        db.add(PosteAssignation(poste_id=poste.id, user_id=courant.user_id))
        db.flush()
    return ouvrir_session(db, courant, poste_id=poste.id, fonds_initial=fonds_initial)


def _config(db: Session) -> CaisseParametres:
    return lire_parametres(db)


# --- Motif : optionnel sous le seuil, obligatoire au-delà -----------------------------------


def test_fermeture_sans_motif_sous_le_seuil_reussit(db: Session) -> None:
    agence = _agence(db, "CA2A1")
    caissier = _courant(_utilisateur(db, agence, "1"), agence)
    session = _ouvrir(db, caissier, agence, fonds_initial=10_000)
    db.flush()

    # Écart de 100 F, largement sous le seuil par défaut (500 F) — aucun motif requis.
    resultat = fermer_session(db, caissier, session.id, montant_reel=10_100)

    assert resultat.session.status == "fermee"
    assert resultat.session.motif_ecart is None


def test_fermeture_sans_motif_au_dela_du_seuil_refuse(db: Session) -> None:
    agence = _agence(db, "CA2A2")
    caissier = _courant(_utilisateur(db, agence, "2"), agence)
    session = _ouvrir(db, caissier, agence, fonds_initial=10_000)
    db.flush()

    with pytest.raises(MotifRequisError, match=r"700 F.*seuil de tolérance de 500 F"):
        fermer_session(db, caissier, session.id, montant_reel=10_700)

    # Rien n'a bougé : la session reste ouverte (refus AVANT toute mutation).
    db.refresh(session)
    assert session.status == "ouverte"


def test_fermeture_avec_motif_au_dela_du_seuil_reussit_et_persiste(db: Session) -> None:
    agence = _agence(db, "CA2A3")
    caissier = _courant(_utilisateur(db, agence, "3"), agence)
    session = _ouvrir(db, caissier, agence, fonds_initial=10_000)
    db.flush()

    resultat = fermer_session(
        db, caissier, session.id, montant_reel=10_700, motif="Erreur de comptage au guichet"
    )

    assert resultat.session.status == "fermee"
    assert resultat.session.motif_ecart == "Erreur de comptage au guichet"


def test_motif_uniquement_des_espaces_traite_comme_absent(db: Session) -> None:
    """Un motif blanc n'en est pas un — même discipline que partout ailleurs dans le projet
    (`.strip()` avant tout contrôle de présence)."""
    agence = _agence(db, "CA2A4")
    caissier = _courant(_utilisateur(db, agence, "4"), agence)
    session = _ouvrir(db, caissier, agence, fonds_initial=10_000)
    db.flush()

    with pytest.raises(MotifRequisError):
        fermer_session(db, caissier, session.id, montant_reel=10_700, motif="   ")


def test_motif_exact_au_seuil_nest_pas_exige(db: Session) -> None:
    """`abs(ecart) > seuil`, PAS `>=` — un écart ÉGAL au seuil reste toléré sans motif."""
    agence = _agence(db, "CA2A5")
    caissier = _courant(_utilisateur(db, agence, "5"), agence)
    session = _ouvrir(db, caissier, agence, fonds_initial=10_000)
    db.flush()

    resultat = fermer_session(db, caissier, session.id, montant_reel=10_500)
    assert resultat.session.status == "fermee"


# --- session_a_valider : LE calcul dérivé, jamais stocké -------------------------------------


def test_session_a_valider_calcul_pur() -> None:
    base = CaisseSession(
        agency_id=uuid.uuid4(), caissier_id=uuid.uuid4(), poste_id=uuid.uuid4(),
        compte_caisse_id=uuid.uuid4(), fonds_initial=0, status="fermee", ecart=-700,
        valide_le=None,
    )
    assert session_a_valider(base, 500) is True  # manquant significatif, non validé

    excedent = CaisseSession(
        agency_id=uuid.uuid4(), caissier_id=uuid.uuid4(), poste_id=uuid.uuid4(),
        compte_caisse_id=uuid.uuid4(), fonds_initial=0, status="fermee", ecart=700,
        valide_le=None,
    )
    assert session_a_valider(excedent, 500) is True  # excédent : compte AUSSI

    sous_seuil = CaisseSession(
        agency_id=uuid.uuid4(), caissier_id=uuid.uuid4(), poste_id=uuid.uuid4(),
        compte_caisse_id=uuid.uuid4(), fonds_initial=0, status="fermee", ecart=100,
        valide_le=None,
    )
    assert session_a_valider(sous_seuil, 500) is False

    deja_validee = CaisseSession(
        agency_id=uuid.uuid4(), caissier_id=uuid.uuid4(), poste_id=uuid.uuid4(),
        compte_caisse_id=uuid.uuid4(), fonds_initial=0, status="fermee", ecart=-700,
        valide_le=datetime.now(UTC),
    )
    assert session_a_valider(deja_validee, 500) is False

    ouverte = CaisseSession(
        agency_id=uuid.uuid4(), caissier_id=uuid.uuid4(), poste_id=uuid.uuid4(),
        compte_caisse_id=uuid.uuid4(), fonds_initial=0, status="ouverte", ecart=None,
        valide_le=None,
    )
    assert session_a_valider(ouverte, 500) is False


def test_lister_sessions_a_valider_exclut_sous_seuil_et_deja_validees(db: Session) -> None:
    agence = _agence(db, "CA2B1")
    caissier = _courant(_utilisateur(db, agence, "10"), agence)
    responsable = _courant(
        _utilisateur(db, agence, "11", "RESPONSABLE_AGENCE"), agence, "RESPONSABLE_AGENCE"
    )

    petit = _ouvrir(db, caissier, agence, fonds_initial=0)
    db.flush()
    fermer_session(db, caissier, petit.id, montant_reel=100)  # sous le seuil

    resultat_avant = lister_sessions_a_valider(db, caissier)
    assert petit.id not in {ligne.id for ligne in resultat_avant.lignes}

    significatif = _ouvrir(db, caissier, agence, fonds_initial=0)
    db.flush()
    fermer_session(db, caissier, significatif.id, montant_reel=10_000, motif="Test")

    resultat = lister_sessions_a_valider(db, caissier)
    ids = {ligne.id for ligne in resultat.lignes}
    assert significatif.id in ids
    assert petit.id not in ids
    assert resultat.seuil_tolerance == 500

    # Validée : disparaît immédiatement de la liste (dérivé, pas de purge à faire).
    valider_ecart(db, responsable, significatif.id)
    apres_validation = lister_sessions_a_valider(db, caissier)
    assert significatif.id not in {ligne.id for ligne in apres_validation.lignes}


def test_lister_sessions_a_valider_perimetre(db: Session) -> None:
    agence_a = _agence(db, "CA2C1")
    agence_b = _agence(db, "CA2C2")
    caissier_a = _courant(_utilisateur(db, agence_a, "20"), agence_a)
    responsable_a = _courant(
        _utilisateur(db, agence_a, "21", "RESPONSABLE_AGENCE"), agence_a, "RESPONSABLE_AGENCE"
    )
    responsable_b = _courant(
        _utilisateur(db, agence_b, "22", "RESPONSABLE_AGENCE"), agence_b, "RESPONSABLE_AGENCE"
    )

    session_a = _ouvrir(db, caissier_a, agence_a, fonds_initial=0)
    db.flush()
    fermer_session(db, caissier_a, session_a.id, montant_reel=10_000, motif="Test")

    # Le caissier voit la SIENNE.
    assert session_a.id in {ligne.id for ligne in lister_sessions_a_valider(db, caissier_a).lignes}
    # Le responsable de SON agence la voit.
    assert session_a.id in {
        ligne.id for ligne in lister_sessions_a_valider(db, responsable_a).lignes
    }
    # Le responsable d'une AUTRE agence ne la voit pas.
    assert session_a.id not in {
        ligne.id for ligne in lister_sessions_a_valider(db, responsable_b).lignes
    }


# --- valider_ecart : trace, jamais un blocage, jamais deux fois ------------------------------


def test_valider_ecart_succes_pose_la_trace(db: Session) -> None:
    agence = _agence(db, "CA2D1")
    caissier = _courant(_utilisateur(db, agence, "30"), agence)
    responsable_user = _utilisateur(db, agence, "31", "RESPONSABLE_AGENCE")
    responsable = _courant(responsable_user, agence, "RESPONSABLE_AGENCE")

    session = _ouvrir(db, caissier, agence, fonds_initial=0)
    db.flush()
    fermer_session(db, caissier, session.id, montant_reel=10_000, motif="Test")

    resultat = valider_ecart(db, responsable, session.id)

    assert resultat.valide_le is not None
    assert resultat.valide_par == responsable_user.id


def test_valider_ecart_deux_fois_refuse(db: Session) -> None:
    agence = _agence(db, "CA2D2")
    caissier = _courant(_utilisateur(db, agence, "32"), agence)
    responsable = _courant(
        _utilisateur(db, agence, "33", "RESPONSABLE_AGENCE"), agence, "RESPONSABLE_AGENCE"
    )
    session = _ouvrir(db, caissier, agence, fonds_initial=0)
    db.flush()
    fermer_session(db, caissier, session.id, montant_reel=10_000, motif="Test")
    valider_ecart(db, responsable, session.id)

    with pytest.raises(SessionDejaValideeError):
        valider_ecart(db, responsable, session.id)


def test_valider_ecart_non_significatif_refuse(db: Session) -> None:
    agence = _agence(db, "CA2D3")
    caissier = _courant(_utilisateur(db, agence, "34"), agence)
    responsable = _courant(
        _utilisateur(db, agence, "35", "RESPONSABLE_AGENCE"), agence, "RESPONSABLE_AGENCE"
    )
    session = _ouvrir(db, caissier, agence, fonds_initial=0)
    db.flush()
    fermer_session(db, caissier, session.id, montant_reel=100)  # sous le seuil

    with pytest.raises(EcartNonSignificatifError):
        valider_ecart(db, responsable, session.id)


def test_valider_ecart_hors_perimetre_introuvable(db: Session) -> None:
    agence_a = _agence(db, "CA2D4")
    agence_b = _agence(db, "CA2D5")
    caissier = _courant(_utilisateur(db, agence_a, "36"), agence_a)
    responsable_b = _courant(
        _utilisateur(db, agence_b, "37", "RESPONSABLE_AGENCE"), agence_b, "RESPONSABLE_AGENCE"
    )
    session = _ouvrir(db, caissier, agence_a, fonds_initial=0)
    db.flush()
    fermer_session(db, caissier, session.id, montant_reel=10_000, motif="Test")

    with pytest.raises(SessionIntrouvableError):  # -> 404 côté router, jamais 403 (IDOR)
        valider_ecart(db, responsable_b, session.id)


# --- Régression : la lettre de manquant reste indépendante du seuil CA2 ----------------------


def test_lettre_manquants_ignore_le_seuil_ca2(db: Session) -> None:
    """Décision confirmée explicitement : la lettre se déclenche sur TOUT manquant, même
    minime — un régime distinct du contrôle de matérialité CA2, jamais aligné dessus."""
    agence = _agence(db, "CA2E1")
    caissier = _courant(_utilisateur(db, agence, "40"), agence)
    session = _ouvrir(db, caissier, agence, fonds_initial=10_000)
    db.flush()
    # Manquant de 50 F seulement — très en dessous du seuil de 500 F.
    fermer_session(db, caissier, session.id, montant_reel=9_950)

    manquants = lister_sessions_manquantes(db, caissier)
    assert session.id in {ligne.id for ligne in manquants.lignes}
    # ET absente de la file « à valider » (sous le seuil) — les deux mécanismes divergent bien.
    assert session.id not in {
        ligne.id for ligne in lister_sessions_a_valider(db, caissier).lignes
    }


# --- Paramètres (CA2) : lecture, modification, motif obligatoire ------------------------------


def test_parametres_lecture_valeur_par_defaut(db: Session) -> None:
    config = _config(db)
    assert config.seuil_tolerance == 500
    assert config.is_provisional is True


def test_parametres_modification_change_le_seuil_immediatement(db: Session) -> None:
    agence = _agence(db, "CA2F1")
    caissier = _courant(_utilisateur(db, agence, "50"), agence)
    config = _config(db)
    modifier_parametres(
        db, config, seuil_tolerance=1_000,
        compte_ecart_manquant_number=None, compte_ecart_excedent_number=None,
        motif="Ajustement test", par=None,
    )

    session = _ouvrir(db, caissier, agence, fonds_initial=10_000)
    db.flush()
    # 700 F : refusé sans motif sous l'ANCIEN seuil (500), toléré sous le NOUVEAU (1000).
    resultat = fermer_session(db, caissier, session.id, montant_reel=10_700)
    assert resultat.session.status == "fermee"
    assert resultat.session.motif_ecart is None


# --- API : permissions, messages, table des erreurs ------------------------------------------


def test_api_parametres_lecture_ouverte_au_caissier(client: TestClient, db: Session) -> None:
    """Le caissier doit pouvoir lire le seuil AVANT de fermer (savoir s'il devra motiver) —
    lecture élargie à caisse.session.close, contrairement à l'écriture (comptable seul)."""
    agence = _agence(db, "CA2G1")
    caissier_user = _utilisateur(db, agence, "60")
    reponse = client.get("/caisse/parametres", headers=_entete(caissier_user, agence, "CAISSIER"))
    assert reponse.status_code == 200
    assert reponse.json()["seuil_tolerance"] == 500


def test_api_parametres_modification_reservee_a_compta_plan_manage(
    client: TestClient, db: Session
) -> None:
    """Lire n'est pas écrire : un caissier ne peut PAS modifier le seuil, même s'il peut le
    consulter."""
    agence = _agence(db, "CA2G7")
    caissier_user = _utilisateur(db, agence, "66")
    reponse = client.put(
        "/caisse/parametres",
        json={"seuil_tolerance": 1000, "motif": "Tentative non autorisée"},
        headers=_entete(caissier_user, agence, "CAISSIER"),
    )
    assert reponse.status_code == 403


def test_api_fermeture_sans_motif_au_dela_du_seuil_422(client: TestClient, db: Session) -> None:
    agence = _agence(db, "CA2G2")
    caissier_user = _utilisateur(db, agence, "61")
    entete = _entete(caissier_user, agence, "CAISSIER")
    caissier = _courant(caissier_user, agence)
    session = _ouvrir(db, caissier, agence, fonds_initial=10_000)
    db.commit()

    reponse = client.post(
        f"/caisse/sessions/{session.id}/fermeture",
        json={"montant_reel": 10_700},
        headers=entete,
    )
    assert reponse.status_code == 422
    assert "motif" in reponse.json()["detail"].lower()


def test_api_fermeture_avec_motif_au_dela_du_seuil_reussit(client: TestClient, db: Session) -> None:
    agence = _agence(db, "CA2G3")
    caissier_user = _utilisateur(db, agence, "62")
    entete = _entete(caissier_user, agence, "CAISSIER")
    caissier = _courant(caissier_user, agence)
    session = _ouvrir(db, caissier, agence, fonds_initial=10_000)
    db.commit()

    reponse = client.post(
        f"/caisse/sessions/{session.id}/fermeture",
        json={"montant_reel": 10_700, "motif": "Erreur de comptage"},
        headers=entete,
    )
    assert reponse.status_code == 200
    assert reponse.json()["motif_ecart"] == "Erreur de comptage"
    assert reponse.json()["a_valider"] is True


def test_api_validation_ecart_refusee_a_un_simple_caissier(client: TestClient, db: Session) -> None:
    agence = _agence(db, "CA2G4")
    caissier_user = _utilisateur(db, agence, "63")
    caissier = _courant(caissier_user, agence)
    session = _ouvrir(db, caissier, agence, fonds_initial=10_000)
    db.flush()
    fermer_session(db, caissier, session.id, montant_reel=10_700, motif="Test")
    db.commit()

    reponse = client.post(
        f"/caisse/sessions/{session.id}/validation-ecart",
        headers=_entete(caissier_user, agence, "CAISSIER"),
    )
    assert reponse.status_code == 403


def test_api_validation_ecart_hors_perimetre_404(client: TestClient, db: Session) -> None:
    agence_a = _agence(db, "CA2G5")
    agence_b = _agence(db, "CA2G6")
    caissier_user = _utilisateur(db, agence_a, "64")
    caissier = _courant(caissier_user, agence_a)
    responsable_b_user = _utilisateur(db, agence_b, "65", "RESPONSABLE_AGENCE")
    session = _ouvrir(db, caissier, agence_a, fonds_initial=10_000)
    db.flush()
    fermer_session(db, caissier, session.id, montant_reel=10_700, motif="Test")
    db.commit()

    reponse = client.post(
        f"/caisse/sessions/{session.id}/validation-ecart",
        headers=_entete(responsable_b_user, agence_b, "RESPONSABLE_AGENCE"),
    )
    assert reponse.status_code == 404
