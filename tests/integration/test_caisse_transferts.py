"""Chantier coffre-fort/caisses, sous-chantier 2, Lot 1 — transferts de fonds entre niveaux de
caisse ADJACENTS (coffre/principale/secondaire), backend socle (`caisse/transferts.py`).

C'est ICI que de l'argent bouge vraiment (contrairement au sous-chantier 1) : ce que ces tests
protègent en priorité :
  - Un transfert SANS écart (coffre -> principale) : écriture d'envoi équilibrée, le compte de
    liaison encaisse le montant envoyé puis retombe exactement à zéro à la réception, la balance
    globale (Σ débit = Σ crédit) tient à CHAQUE étape.
  - Un transfert AVEC écart (montant compté != montant envoyé) : la 3e ligne part vers le compte
    d'écart de transfert DÉDIÉ (jamais celui de l'écart de caisse), la pièce reste équilibrée, le
    compte de liaison retombe quand même à zéro.
  - DOUBLE REGARD : receveur = envoyeur refusé par le service (message clair) ET par le CHECK
    base (dernier rempart, contourné exprès dans un test pour le prouver).
  - ADJACENCE : coffre -> secondaire direct refusé, RIEN écrit (aucune ligne, aucune pièce).
  - CONTRÔLE À L'OBJET (résolution du point de rupture identifié avant tout code) : côté
    secondaire, seul le caissier TITULAIRE de la session ouverte sur ce poste peut agir — un
    autre agent, même habilité, est refusé.
  - PARAMÉTRAGE INCOMPLET (compte de transit non rattaché) : refus propre AVANT toute écriture,
    aucun transfert créé.

Lot 2 (endpoints, section « API » en fin de fichier) : mêmes garde-fous, vus depuis HTTP —
succès, refus de permission (403), cloisonnement hors agence (404, IDOR), double regard (422),
contrôle à l'objet côté secondaire (422). Même fichier que le Lot 1 (précédent de
`test_caisse_niveaux.py`, qui mélange déjà service et endpoints pour ce module).
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.caisse.models import (
    CaisseParametres,
    CaisseSession,
    CaissierPrincipal,
    NiveauCaisse,
    Poste,
    Transfert,
)
from app.modules.caisse.parametres import modifier as modifier_parametres
from app.modules.caisse.transferts import (
    AdjacenceInvalideError,
    CaissierPrincipalNonDesigneError,
    CaissierPrincipalRequisError,
    CompteTransitNonParametreError,
    DoubleRegardError,
    ResponsableCoffreRequisError,
    SessionCaissierRequiseError,
    initier_transfert,
    receptionner_transfert,
)
from app.modules.comptabilite import journee
from app.modules.comptabilite.comptes import CompteInvalideRattachementError
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.security.jwt import creer_access_token
from app.modules.security.models import Role, User, UserRole
from app.modules.security.password import hasher_mot_de_passe

pytestmark = pytest.mark.integration

# Sous-chantier 3 : caisse.coffre.gerer ajoutée — la plupart des acteurs de test agissent côté
# coffre (responsabilité de rôle). Être caissier principal reste une IDENTITÉ (voir
# _designer_principal), jamais une permission — pas dans ce frozenset.
PERMISSIONS_TRANSFERT = frozenset(
    {"caisse.transfert.initier", "caisse.transfert.valider", "caisse.coffre.gerer"}
)


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
    """Chantier P1bis lot 3 : `transferts._jour` (envoi ET réception) exige désormais une
    journée comptable ouverte — ce fichier construit ses sessions de caisse par INSERT direct
    (`_session_ouverte`), pas via `caisse.service.ouvrir_session`, donc le lot 2 ne l'a jamais
    concerné ; le lot 3 l'atteint quand même via `_jour`. Même garde-fou anti-interblocage que
    les 19 fichiers du lot 2 : n'agit que si `db` est déjà dans la fermeture de fixtures du
    test."""
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


def _solde(db: Session, compte_id: uuid.UUID) -> int:
    return db.execute(
        text(
            "SELECT COALESCE(SUM(CASE WHEN side = 'D' THEN amount ELSE -amount END), 0) "
            "FROM comptabilite.journal_lines WHERE account_id = :c"
        ),
        {"c": compte_id},
    ).scalar_one()


def _lignes(db: Session, entry_id: uuid.UUID) -> list[tuple[str, int]]:
    return db.execute(
        text(
            "SELECT side, amount FROM comptabilite.journal_lines WHERE entry_id = :e "
            "ORDER BY line_number"
        ),
        {"e": entry_id},
    ).all()  # type: ignore[return-value]


@pytest.fixture
def agence(db: Session) -> Agency:
    agence = Agency(code=f"AG-{uuid.uuid4().hex[:6]}", name="Agence de test")
    db.add(agence)
    db.flush()
    return agence


@pytest.fixture
def poste(db: Session, agence: Agency) -> Poste:
    poste = Poste(
        agency_id=agence.id,
        code="01",
        libelle="Guichet 1",
        compte_caisse_id=_cid(db, "101112"),
        is_active=True,
    )
    db.add(poste)
    db.flush()
    return poste


@pytest.fixture
def niveaux(db: Session, agence: Agency) -> None:
    """Coffre/principale paramétrés pour cette agence (comptes réels sous 1011, déjà en base)."""
    db.add(NiveauCaisse(agency_id=agence.id, niveau="coffre", compte_caisse_id=_cid(db, "101115")))
    db.add(
        NiveauCaisse(agency_id=agence.id, niveau="principale", compte_caisse_id=_cid(db, "101114"))
    )
    db.flush()


@pytest.fixture
def compte_transit_pose(db: Session) -> CaisseParametres:
    """Pont comptable paramétré : compte de liaison + écarts de transfert dédiés (6099/7099,
    réutilisés — l'IMF peut choisir les mêmes que l'écart de caisse, décision actée)."""
    config = db.execute(select(CaisseParametres).limit(1)).scalar_one()
    config.compte_transit_id = _cid(db, "1141")
    config.compte_ecart_transfert_manquant_id = _cid(db, "6099")
    config.compte_ecart_transfert_excedent_id = _cid(db, "7099")
    db.flush()
    return config


def _utilisateur(db: Session, agence: Agency) -> User:
    suffixe = uuid.uuid4().hex[:8]
    user = User(
        matricule=f"MAT-{suffixe}",
        email=f"{suffixe}@example.com",
        username=f"u{suffixe}",
        password_hash="x",
        last_name="Test",
        first_name="U",
        primary_agency_id=agence.id,
    )
    db.add(user)
    db.flush()
    return user


def _courant(user: User, agence: Agency) -> UtilisateurCourant:
    return UtilisateurCourant(
        user_id=user.id,
        roles=(),
        permissions=PERMISSIONS_TRANSFERT,
        primary_agency_id=agence.id,
        agency_id=agence.id,
        voit_tout=False,
    )


def _session_ouverte(db: Session, poste: Poste, caissier: User) -> CaisseSession:
    session = CaisseSession(
        agency_id=poste.agency_id,
        caissier_id=caissier.id,
        poste_id=poste.id,
        compte_caisse_id=poste.compte_caisse_id,
        fonds_initial=0,
    )
    db.add(session)
    db.flush()
    return session


def _designer_principal(db: Session, agence: Agency, caissier: User) -> None:
    """Sous-chantier 3 : désigne LE caissier principal d'une agence — insertion directe (pas le
    service `caissiers_principaux.designer`, testé séparément), même discipline que
    `_session_ouverte`."""
    db.add(CaissierPrincipal(agency_id=agence.id, user_id=caissier.id))
    db.flush()


def _utilisateur_avec_role(db: Session, agence: Agency, role_code: str) -> User:
    """Pour les tests HTTP (Lot 2) : un vrai rôle système, résolu par l'authentification réelle
    — contrairement à `_utilisateur`/`_courant`, qui fabriquent un `UtilisateurCourant` à la main
    pour tester le service en direct, sans passer par la chaîne d'authentification."""
    role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
    suffixe = uuid.uuid4().hex[:8]
    user = User(
        matricule=f"MAT-{suffixe}",
        email=f"{suffixe}@example.com",
        username=f"u{suffixe}",
        password_hash=hasher_mot_de_passe("Motdepasse!123"),
        last_name="Test",
        first_name="U",
        primary_agency_id=agence.id,
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.flush()
    return user


def _entete(user: User, agence: Agency, role_code: str) -> dict[str, str]:
    jeton = creer_access_token(
        user_id=user.id, roles=[role_code], primary_agency_id=agence.id, agency_id=agence.id
    )
    return {"Authorization": f"Bearer {jeton}"}


# --- 1. Transfert complet, sans écart --------------------------------------------------------


def test_transfert_coffre_principale_complet(
    db: Session, agence: Agency, niveaux: None, compte_transit_pose: CaisseParametres
) -> None:
    # Deltas (avant/après), JAMAIS un solde absolu : le compte 101115/101114 sert aussi à la
    # vraie base de dev (paramétrage réel testé à l'écran) — un solde absolu casserait au
    # premier mouvement réel posté ailleurs. Voir dette-fuite-isolation-savepoint-like : robuste
    # à l'activité ambiante, jamais dépendant d'une base vierge.
    envoyeur = _utilisateur(db, agence)
    receveur = _utilisateur(db, agence)
    compte_coffre = _cid(db, "101115")
    compte_principale = _cid(db, "101114")
    compte_transit = _cid(db, "1141")
    avant_coffre = _solde(db, compte_coffre)
    avant_transit = _solde(db, compte_transit)
    avant_principale = _solde(db, compte_principale)

    transfert = initier_transfert(
        db,
        _courant(envoyeur, agence),
        niveau_source="coffre",
        niveau_destination="principale",
        poste_id=None,
        montant_envoye=100_000,
        motif="Alimentation de la principale",
    )

    assert transfert.statut == "en_transit"
    assert transfert.envoye_par == envoyeur.id
    assert transfert.journal_entry_envoi_id is not None

    lignes_envoi = _lignes(db, transfert.journal_entry_envoi_id)
    assert sorted(lignes_envoi) == [("C", 100_000), ("D", 100_000)]
    # « en transit », visible dans le grand livre :
    assert _solde(db, compte_transit) - avant_transit == 100_000
    assert _solde(db, compte_coffre) - avant_coffre == -100_000

    _designer_principal(db, agence, receveur)  # sous-chantier 3 : identité requise à la réception
    resultat = receptionner_transfert(
        db, _courant(receveur, agence), transfert.id, montant_compte=100_000
    )

    assert resultat.statut == "receptionne"
    assert resultat.montant_compte == 100_000
    assert resultat.receptionne_par == receveur.id
    assert resultat.journal_entry_reception_id is not None

    lignes_reception = _lignes(db, resultat.journal_entry_reception_id)
    assert sorted(lignes_reception) == [("C", 100_000), ("D", 100_000)]
    assert len(lignes_reception) == 2  # pas d'écart : pas de 3e ligne

    assert _solde(db, compte_transit) - avant_transit == 0  # retombe exactement à zéro
    assert _solde(db, compte_principale) - avant_principale == 100_000


# --- 2. Transfert avec écart -----------------------------------------------------------------


def test_transfert_avec_ecart_manquant(
    db: Session,
    agence: Agency,
    poste: Poste,
    niveaux: None,
    compte_transit_pose: CaisseParametres,
) -> None:
    envoyeur = _utilisateur(db, agence)  # agit côté « principale », pas de session requise
    receveur = _utilisateur(db, agence)  # caissier titulaire du poste (secondaire)
    _session_ouverte(db, poste, receveur)
    # Sous-chantier 3 : l'envoyeur agit côté principale -> doit être LE caissier principal désigné.
    _designer_principal(db, agence, envoyeur)
    compte_transit = _cid(db, "1141")
    compte_secondaire = _cid(db, "101112")
    compte_ecart_manquant = _cid(db, "6099")
    avant_transit = _solde(db, compte_transit)
    avant_secondaire = _solde(db, compte_secondaire)
    avant_ecart_manquant = _solde(db, compte_ecart_manquant)

    transfert = initier_transfert(
        db,
        _courant(envoyeur, agence),
        niveau_source="principale",
        niveau_destination="secondaire",
        poste_id=poste.id,
        montant_envoye=50_000,
        motif="Approvisionnement du guichet 1",
    )

    resultat = receptionner_transfert(
        db, _courant(receveur, agence), transfert.id, montant_compte=48_000
    )

    lignes = _lignes(db, resultat.journal_entry_reception_id)  # type: ignore[arg-type]
    assert sorted(lignes) == [("C", 50_000), ("D", 2_000), ("D", 48_000)]
    total_debit = sum(m for s, m in lignes if s == "D")
    total_credit = sum(m for s, m in lignes if s == "C")
    assert total_debit == total_credit  # pièce équilibrée

    assert _solde(db, compte_transit) - avant_transit == 0  # retombe à zéro malgré l'écart
    # le montant RÉELLEMENT compté :
    assert _solde(db, compte_secondaire) - avant_secondaire == 48_000
    # exactement l'écart, au bon compte :
    assert _solde(db, compte_ecart_manquant) - avant_ecart_manquant == 2_000


# --- 3. Double regard --------------------------------------------------------------------------


def test_double_regard_refuse_par_le_service(
    db: Session, agence: Agency, niveaux: None, compte_transit_pose: CaisseParametres
) -> None:
    envoyeur = _utilisateur(db, agence)
    transfert = initier_transfert(
        db,
        _courant(envoyeur, agence),
        niveau_source="coffre",
        niveau_destination="principale",
        poste_id=None,
        montant_envoye=10_000,
        motif="Test double regard",
    )

    with pytest.raises(DoubleRegardError):
        receptionner_transfert(
            db, _courant(envoyeur, agence), transfert.id, montant_compte=10_000
        )


def test_double_regard_refuse_par_le_check_base(
    db: Session, agence: Agency, niveaux: None, compte_transit_pose: CaisseParametres
) -> None:
    envoyeur = _utilisateur(db, agence)
    transfert = initier_transfert(
        db,
        _courant(envoyeur, agence),
        niveau_source="coffre",
        niveau_destination="principale",
        poste_id=None,
        montant_envoye=10_000,
        motif="Test double regard (base)",
    )
    # Contourne le service, tente d'écrire directement receptionne_par = envoye_par : le CHECK
    # `double_regard_envoyeur_receveur` (migration 0047) est le dernier rempart.
    with pytest.raises(IntegrityError, match="double_regard_envoyeur_receveur"):
        db.execute(
            text(
                "UPDATE caisse.transferts SET statut = 'receptionne', "
                "receptionne_par = envoye_par, receptionne_le = NOW(), "
                "montant_compte = montant_envoye, "
                "journal_entry_reception_id = journal_entry_envoi_id WHERE id = :id"
            ),
            {"id": transfert.id},
        )
        db.flush()


# --- 4. Adjacence -------------------------------------------------------------------------------


def test_adjacence_coffre_secondaire_direct_refuse(
    db: Session, agence: Agency, poste: Poste, niveaux: None, compte_transit_pose: CaisseParametres
) -> None:
    envoyeur = _utilisateur(db, agence)
    avant = db.execute(select(Transfert.id)).all()

    with pytest.raises(AdjacenceInvalideError):
        initier_transfert(
            db,
            _courant(envoyeur, agence),
            niveau_source="coffre",
            niveau_destination="secondaire",
            poste_id=poste.id,
            montant_envoye=10_000,
            motif="Tentative coffre -> secondaire",
        )

    apres = db.execute(select(Transfert.id)).all()
    assert apres == avant  # rien écrit


# --- 5. Contrôle à l'objet côté secondaire -------------------------------------------------------


def test_caissier_non_titulaire_du_poste_refuse(
    db: Session, agence: Agency, poste: Poste, niveaux: None, compte_transit_pose: CaisseParametres
) -> None:
    titulaire = _utilisateur(db, agence)
    # Habilité, mais SA session à lui n'est pas ouverte ici — la session du poste est au titulaire.
    autre_caissier = _utilisateur(db, agence)
    _session_ouverte(db, poste, titulaire)

    with pytest.raises(SessionCaissierRequiseError):
        initier_transfert(
            db,
            _courant(autre_caissier, agence),
            niveau_source="secondaire",
            niveau_destination="principale",
            poste_id=poste.id,
            montant_envoye=5_000,
            motif="Tentative par un non-titulaire",
        )


# --- 6. Paramétrage incomplet ---------------------------------------------------------------


def test_compte_transit_non_parametre_refuse(db: Session, agence: Agency, niveaux: None) -> None:
    # Vide EXPLICITEMENT le paramétrage réel (une vraie IMF peut déjà avoir rattaché son compte
    # de liaison, comme validé à l'écran) — ce test doit rester vrai quel que soit l'état de la
    # base, jamais dépendant d'un singleton « encore vierge » par coïncidence.
    db.execute(text("UPDATE caisse.parametres SET compte_transit_id = NULL"))
    envoyeur = _utilisateur(db, agence)
    avant = db.execute(select(Transfert.id)).all()

    with pytest.raises(CompteTransitNonParametreError):
        initier_transfert(
            db,
            _courant(envoyeur, agence),
            niveau_source="coffre",
            niveau_destination="principale",
            poste_id=None,
            montant_envoye=10_000,
            motif="Tentative sans compte de liaison",
        )

    apres = db.execute(select(Transfert.id)).all()
    assert apres == avant  # rien créé


# =============================================================================================
# --- API (Lot 2) : mêmes garde-fous, vus depuis HTTP ------------------------------------------
# =============================================================================================


def test_api_initier_transfert_ok(
    client: TestClient,
    db: Session,
    agence: Agency,
    niveaux: None,
    compte_transit_pose: CaisseParametres,
) -> None:
    envoyeur = _utilisateur_avec_role(db, agence, "RESPONSABLE_AGENCE")

    reponse = client.post(
        "/caisse/transferts",
        json={
            "niveau_source": "coffre",
            "niveau_destination": "principale",
            "montant_envoye": 75_000,
            "motif": "Alimentation de la principale (API)",
        },
        headers=_entete(envoyeur, agence, "RESPONSABLE_AGENCE"),
    )

    assert reponse.status_code == 201
    corps = reponse.json()
    assert corps["statut"] == "en_transit"
    assert corps["montant_envoye"] == 75_000
    assert corps["compte_source_number"] == "101115"
    assert corps["compte_destination_number"] == "101114"
    assert corps["envoye_par_nom"].strip() != ""
    assert corps["receptionne_par_nom"] is None


def test_api_initier_transfert_permission_absente_403(
    client: TestClient,
    db: Session,
    agence: Agency,
    niveaux: None,
    compte_transit_pose: CaisseParametres,
) -> None:
    # COMPTABLE n'a ni caisse.transfert.initier ni caisse.transfert.valider.
    comptable = _utilisateur_avec_role(db, agence, "COMPTABLE")

    reponse = client.post(
        "/caisse/transferts",
        json={
            "niveau_source": "coffre",
            "niveau_destination": "principale",
            "montant_envoye": 10_000,
            "motif": "Tentative sans permission",
        },
        headers=_entete(comptable, agence, "COMPTABLE"),
    )

    assert reponse.status_code == 403


def test_api_cycle_complet_initier_puis_receptionner(
    client: TestClient,
    db: Session,
    agence: Agency,
    niveaux: None,
    compte_transit_pose: CaisseParametres,
) -> None:
    envoyeur = _utilisateur_avec_role(db, agence, "RESPONSABLE_AGENCE")
    receveur = _utilisateur_avec_role(db, agence, "RESPONSABLE_AGENCE")
    _designer_principal(db, agence, receveur)

    creation = client.post(
        "/caisse/transferts",
        json={
            "niveau_source": "coffre",
            "niveau_destination": "principale",
            "montant_envoye": 60_000,
            "motif": "Cycle complet API",
        },
        headers=_entete(envoyeur, agence, "RESPONSABLE_AGENCE"),
    )
    transfert_id = creation.json()["id"]

    reponse = client.post(
        f"/caisse/transferts/{transfert_id}/reception",
        json={"montant_compte": 60_000},
        headers=_entete(receveur, agence, "RESPONSABLE_AGENCE"),
    )

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["statut"] == "receptionne"
    assert corps["montant_compte"] == 60_000
    assert corps["receptionne_par_nom"].strip() != ""


def test_api_reception_double_regard_422(
    client: TestClient,
    db: Session,
    agence: Agency,
    niveaux: None,
    compte_transit_pose: CaisseParametres,
) -> None:
    envoyeur = _utilisateur_avec_role(db, agence, "RESPONSABLE_AGENCE")

    creation = client.post(
        "/caisse/transferts",
        json={
            "niveau_source": "coffre",
            "niveau_destination": "principale",
            "montant_envoye": 5_000,
            "motif": "Tentative double regard API",
        },
        headers=_entete(envoyeur, agence, "RESPONSABLE_AGENCE"),
    )
    transfert_id = creation.json()["id"]

    reponse = client.post(
        f"/caisse/transferts/{transfert_id}/reception",
        json={"montant_compte": 5_000},
        headers=_entete(envoyeur, agence, "RESPONSABLE_AGENCE"),  # le même acteur
    )

    assert reponse.status_code == 422
    assert "vous-même envoyé" in reponse.json()["detail"]


def test_api_reception_cloisonnement_hors_agence_404(
    client: TestClient, db: Session, niveaux: None, compte_transit_pose: CaisseParametres
) -> None:
    agence_a = Agency(code=f"AG-{uuid.uuid4().hex[:6]}", name="Agence A")
    agence_b = Agency(code=f"AG-{uuid.uuid4().hex[:6]}", name="Agence B")
    db.add_all([agence_a, agence_b])
    db.flush()
    db.add(
        NiveauCaisse(
            agency_id=agence_a.id, niveau="coffre", compte_caisse_id=_cid(db, "101115")
        )
    )
    db.add(
        NiveauCaisse(
            agency_id=agence_a.id, niveau="principale", compte_caisse_id=_cid(db, "101114")
        )
    )
    db.flush()

    envoyeur = _utilisateur_avec_role(db, agence_a, "RESPONSABLE_AGENCE")
    intrus = _utilisateur_avec_role(db, agence_b, "RESPONSABLE_AGENCE")

    creation = client.post(
        "/caisse/transferts",
        json={
            "niveau_source": "coffre",
            "niveau_destination": "principale",
            "montant_envoye": 5_000,
            "motif": "Transfert de l'agence A",
        },
        headers=_entete(envoyeur, agence_a, "RESPONSABLE_AGENCE"),
    )
    transfert_id = creation.json()["id"]

    # Lecture hors agence -> 404 (IDOR, jamais 403 : on ne révèle pas qu'il existe).
    lecture = client.get(
        f"/caisse/transferts/{transfert_id}",
        headers=_entete(intrus, agence_b, "RESPONSABLE_AGENCE"),
    )
    assert lecture.status_code == 404

    # Réception hors agence -> 404, pas 422 : jamais atteindre le double regard sur un objet
    # qu'on ne devrait même pas savoir exister.
    reception = client.post(
        f"/caisse/transferts/{transfert_id}/reception",
        json={"montant_compte": 5_000},
        headers=_entete(intrus, agence_b, "RESPONSABLE_AGENCE"),
    )
    assert reception.status_code == 404


def test_api_controle_objet_caissier_non_titulaire_422(
    client: TestClient,
    db: Session,
    agence: Agency,
    poste: Poste,
    niveaux: None,
    compte_transit_pose: CaisseParametres,
) -> None:
    titulaire = _utilisateur_avec_role(db, agence, "CAISSIER")
    autre_caissier = _utilisateur_avec_role(db, agence, "CAISSIER")
    _session_ouverte(db, poste, titulaire)

    reponse = client.post(
        "/caisse/transferts",
        json={
            "niveau_source": "secondaire",
            "niveau_destination": "principale",
            "poste_id": str(poste.id),
            "montant_envoye": 5_000,
            "motif": "Tentative par un non-titulaire (API)",
        },
        headers=_entete(autre_caissier, agence, "CAISSIER"),
    )

    assert reponse.status_code == 422
    assert "session de caisse ouverte sur ce poste" in reponse.json()["detail"]


def test_api_lister_transferts_en_transit(
    client: TestClient,
    db: Session,
    agence: Agency,
    niveaux: None,
    compte_transit_pose: CaisseParametres,
) -> None:
    acteur = _utilisateur_avec_role(db, agence, "RESPONSABLE_AGENCE")
    client.post(
        "/caisse/transferts",
        json={
            "niveau_source": "coffre",
            "niveau_destination": "principale",
            "montant_envoye": 8_000,
            "motif": "Pour la liste",
        },
        headers=_entete(acteur, agence, "RESPONSABLE_AGENCE"),
    )

    reponse = client.get(
        "/caisse/transferts", headers=_entete(acteur, agence, "RESPONSABLE_AGENCE")
    )

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["total"] >= 1
    assert all(ligne["statut"] == "en_transit" for ligne in corps["lignes"])
    assert all(ligne["agency_id"] == str(agence.id) for ligne in corps["lignes"])


def test_api_lister_transferts_permission_absente_403(
    client: TestClient, db: Session, agence: Agency
) -> None:
    comptable = _utilisateur_avec_role(db, agence, "COMPTABLE")

    reponse = client.get("/caisse/transferts", headers=_entete(comptable, agence, "COMPTABLE"))

    assert reponse.status_code == 403


# =============================================================================================
# --- Historique (Lot 2c) : un transfert réceptionné reste consultable (besoin d'audit) --------
# =============================================================================================


def test_api_lister_transferts_defaut_exclut_les_receptionnes(
    client: TestClient,
    db: Session,
    agence: Agency,
    niveaux: None,
    compte_transit_pose: CaisseParametres,
) -> None:
    envoyeur = _utilisateur_avec_role(db, agence, "RESPONSABLE_AGENCE")
    receveur = _utilisateur_avec_role(db, agence, "RESPONSABLE_AGENCE")
    _designer_principal(db, agence, receveur)
    creation = client.post(
        "/caisse/transferts",
        json={
            "niveau_source": "coffre",
            "niveau_destination": "principale",
            "montant_envoye": 12_000,
            "motif": "Pour l'historique",
        },
        headers=_entete(envoyeur, agence, "RESPONSABLE_AGENCE"),
    )
    transfert_id = creation.json()["id"]
    reception = client.post(
        f"/caisse/transferts/{transfert_id}/reception",
        json={"montant_compte": 12_000},
        headers=_entete(receveur, agence, "RESPONSABLE_AGENCE"),
    )
    assert reception.status_code == 200

    # Sans paramètre statut, le défaut reste « en transit » — comportement du Lot 2 inchangé.
    reponse = client.get(
        "/caisse/transferts", headers=_entete(envoyeur, agence, "RESPONSABLE_AGENCE")
    )

    ids = {ligne["id"] for ligne in reponse.json()["lignes"]}
    assert transfert_id not in ids


def test_api_lister_transferts_statut_receptionne(
    client: TestClient,
    db: Session,
    agence: Agency,
    niveaux: None,
    compte_transit_pose: CaisseParametres,
) -> None:
    envoyeur = _utilisateur_avec_role(db, agence, "RESPONSABLE_AGENCE")
    receveur = _utilisateur_avec_role(db, agence, "RESPONSABLE_AGENCE")
    _designer_principal(db, agence, receveur)
    creation = client.post(
        "/caisse/transferts",
        json={
            "niveau_source": "coffre",
            "niveau_destination": "principale",
            "montant_envoye": 15_000,
            "motif": "Historique — écart",
        },
        headers=_entete(envoyeur, agence, "RESPONSABLE_AGENCE"),
    )
    transfert_id = creation.json()["id"]
    reception = client.post(
        f"/caisse/transferts/{transfert_id}/reception",
        json={"montant_compte": 14_000},  # manquant de 1 000
        headers=_entete(receveur, agence, "RESPONSABLE_AGENCE"),
    )
    assert reception.status_code == 200

    reponse = client.get(
        "/caisse/transferts",
        params={"statut": "receptionne"},
        headers=_entete(envoyeur, agence, "RESPONSABLE_AGENCE"),
    )

    assert reponse.status_code == 200
    lignes = {ligne["id"]: ligne for ligne in reponse.json()["lignes"]}
    assert transfert_id in lignes
    ligne = lignes[transfert_id]
    assert ligne["statut"] == "receptionne"
    assert ligne["montant_envoye"] == 15_000
    assert ligne["montant_compte"] == 14_000  # écart = compté - envoyé, calculable côté écran
    assert ligne["envoye_par_nom"].strip() != ""
    assert ligne["receptionne_par_nom"].strip() != ""
    assert ligne["receptionne_le"] is not None
    # Un transfert en transit (créé plus haut par d'autres tests) ne doit PAS apparaître ici.
    assert all(ligne["statut"] == "receptionne" for ligne in reponse.json()["lignes"])


def test_api_lister_transferts_statut_tous(
    client: TestClient,
    db: Session,
    agence: Agency,
    niveaux: None,
    compte_transit_pose: CaisseParametres,
) -> None:
    envoyeur = _utilisateur_avec_role(db, agence, "RESPONSABLE_AGENCE")
    receveur = _utilisateur_avec_role(db, agence, "RESPONSABLE_AGENCE")
    _designer_principal(db, agence, receveur)
    en_transit = client.post(
        "/caisse/transferts",
        json={
            "niveau_source": "coffre",
            "niveau_destination": "principale",
            "montant_envoye": 5_000,
            "motif": "Reste en transit",
        },
        headers=_entete(envoyeur, agence, "RESPONSABLE_AGENCE"),
    ).json()["id"]
    receptionne = client.post(
        "/caisse/transferts",
        json={
            "niveau_source": "coffre",
            "niveau_destination": "principale",
            "montant_envoye": 6_000,
            "motif": "Sera réceptionné",
        },
        headers=_entete(envoyeur, agence, "RESPONSABLE_AGENCE"),
    ).json()["id"]
    reception = client.post(
        f"/caisse/transferts/{receptionne}/reception",
        json={"montant_compte": 6_000},
        headers=_entete(receveur, agence, "RESPONSABLE_AGENCE"),
    )
    assert reception.status_code == 200

    reponse = client.get(
        "/caisse/transferts",
        params={"statut": "tous"},
        headers=_entete(envoyeur, agence, "RESPONSABLE_AGENCE"),
    )

    ids = {ligne["id"] for ligne in reponse.json()["lignes"]}
    assert {en_transit, receptionne} <= ids


def test_api_lister_transferts_historique_cloisonnement_inchange(
    client: TestClient, db: Session, niveaux: None, compte_transit_pose: CaisseParametres
) -> None:
    agence_a = Agency(code=f"AG-{uuid.uuid4().hex[:6]}", name="Agence A (historique)")
    agence_b = Agency(code=f"AG-{uuid.uuid4().hex[:6]}", name="Agence B (historique)")
    db.add_all([agence_a, agence_b])
    db.flush()
    db.add(
        NiveauCaisse(
            agency_id=agence_a.id, niveau="coffre", compte_caisse_id=_cid(db, "101115")
        )
    )
    db.add(
        NiveauCaisse(
            agency_id=agence_a.id, niveau="principale", compte_caisse_id=_cid(db, "101114")
        )
    )
    db.flush()

    envoyeur_a = _utilisateur_avec_role(db, agence_a, "RESPONSABLE_AGENCE")
    receveur_a = _utilisateur_avec_role(db, agence_a, "RESPONSABLE_AGENCE")
    intrus_b = _utilisateur_avec_role(db, agence_b, "RESPONSABLE_AGENCE")
    _designer_principal(db, agence_a, receveur_a)

    transfert_id = client.post(
        "/caisse/transferts",
        json={
            "niveau_source": "coffre",
            "niveau_destination": "principale",
            "montant_envoye": 3_000,
            "motif": "Historique agence A",
        },
        headers=_entete(envoyeur_a, agence_a, "RESPONSABLE_AGENCE"),
    ).json()["id"]
    reception = client.post(
        f"/caisse/transferts/{transfert_id}/reception",
        json={"montant_compte": 3_000},
        headers=_entete(receveur_a, agence_a, "RESPONSABLE_AGENCE"),
    )
    assert reception.status_code == 200

    reponse = client.get(
        "/caisse/transferts",
        params={"statut": "tous"},
        headers=_entete(intrus_b, agence_b, "RESPONSABLE_AGENCE"),
    )

    ids = {ligne["id"] for ligne in reponse.json()["lignes"]}
    assert transfert_id not in ids  # l'historique reste cloisonné à l'agence, comme le reste


# =============================================================================================
# --- Paramétrage (Lot 2b) : compte de liaison + écarts de transfert, sur l'écran existant -----
# « Seuil de tolérance de caisse » (GET/PUT /caisse/parametres). Garde-fou compte_saisie_actif
# (SANS contrainte de rubrique 1011) : vérifié explicitement avec un compte hors 1011 ET hors
# 6/7, pour prouver que ce n'est PAS compte_caisse_valide qui est utilisé ici.
# =============================================================================================


def test_modifier_parametres_rattache_les_3_comptes_transfert(db: Session) -> None:
    config = db.execute(select(CaisseParametres).limit(1)).scalar_one()

    modifier_parametres(
        db,
        config,
        seuil_tolerance=config.seuil_tolerance,
        compte_ecart_manquant_number=None,
        compte_ecart_excedent_number=None,
        # hors 1011 ET hors 6/7 : preuve qu'aucune rubrique n'est exigée.
        compte_transit_number="1141",
        compte_ecart_transfert_manquant_number="6099",
        compte_ecart_transfert_excedent_number="7099",
        motif="Paramétrage initial des transferts",
        par=None,
    )

    assert config.compte_transit_id == _cid(db, "1141")
    assert config.compte_ecart_transfert_manquant_id == _cid(db, "6099")
    assert config.compte_ecart_transfert_excedent_id == _cid(db, "7099")


def test_modifier_parametres_refuse_compte_transfert_invalide(db: Session) -> None:
    config = db.execute(select(CaisseParametres).limit(1)).scalar_one()

    with pytest.raises(CompteInvalideRattachementError):
        modifier_parametres(
            db,
            config,
            seuil_tolerance=config.seuil_tolerance,
            compte_ecart_manquant_number=None,
            compte_ecart_excedent_number=None,
            # 1011 est un compte de REGROUPEMENT (is_posting=False), pas un compte de saisie.
            compte_transit_number="1011",
            compte_ecart_transfert_manquant_number=None,
            compte_ecart_transfert_excedent_number=None,
            motif="Tentative avec un compte invalide",
            par=None,
        )


def test_api_parametres_expose_les_3_comptes_transfert_non_parametres(
    client: TestClient, db: Session, agence: Agency
) -> None:
    # Vide EXPLICITEMENT (voir test_compte_transit_non_parametre_refuse) : la vraie base peut
    # déjà avoir ces 3 comptes rattachés (paramétrage réel validé à l'écran).
    db.execute(
        text(
            "UPDATE caisse.parametres SET compte_transit_id = NULL, "
            "compte_ecart_transfert_manquant_id = NULL, compte_ecart_transfert_excedent_id = NULL"
        )
    )
    comptable = _utilisateur_avec_role(db, agence, "COMPTABLE")

    reponse = client.get("/caisse/parametres", headers=_entete(comptable, agence, "COMPTABLE"))

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["compte_transit"] is None
    assert corps["compte_ecart_transfert_manquant"] is None
    assert corps["compte_ecart_transfert_excedent"] is None


def test_api_modifier_parametres_rattache_les_3_comptes_transfert(
    client: TestClient, db: Session, agence: Agency
) -> None:
    comptable = _utilisateur_avec_role(db, agence, "COMPTABLE")

    reponse = client.put(
        "/caisse/parametres",
        json={
            "seuil_tolerance": 500,
            "compte_ecart_manquant": None,
            "compte_ecart_excedent": None,
            "compte_transit": "1141",
            "compte_ecart_transfert_manquant": "6099",
            "compte_ecart_transfert_excedent": "7099",
            "motif": "Paramétrage des transferts (API)",
        },
        headers=_entete(comptable, agence, "COMPTABLE"),
    )

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["compte_transit"]["account_number"] == "1141"
    assert corps["compte_ecart_transfert_manquant"]["account_number"] == "6099"
    assert corps["compte_ecart_transfert_excedent"]["account_number"] == "7099"


def test_api_modifier_parametres_compte_transfert_invalide_422(
    client: TestClient, db: Session, agence: Agency
) -> None:
    comptable = _utilisateur_avec_role(db, agence, "COMPTABLE")

    reponse = client.put(
        "/caisse/parametres",
        json={
            "seuil_tolerance": 500,
            "compte_ecart_manquant": None,
            "compte_ecart_excedent": None,
            "compte_transit": "1011",  # compte de regroupement, refusé
            "compte_ecart_transfert_manquant": None,
            "compte_ecart_transfert_excedent": None,
            "motif": "Tentative avec un compte invalide (API)",
        },
        headers=_entete(comptable, agence, "COMPTABLE"),
    )

    assert reponse.status_code == 422


# =============================================================================================
# --- Responsabilité des niveaux (sous-chantier 3, Lot A) — modèle B ---------------------------
# =============================================================================================


def test_coffre_autorise_pour_le_responsable_de_lagence(
    db: Session, agence: Agency, poste: Poste, niveaux: None, compte_transit_pose: CaisseParametres
) -> None:
    # Principale -> coffre (destination = coffre, vérifiée à la RÉCEPTION) : le responsable de
    # CETTE agence est autorisé.
    principal = _utilisateur(db, agence)
    responsable = _utilisateur(db, agence)
    _designer_principal(db, agence, principal)

    transfert = initier_transfert(
        db,
        _courant(principal, agence),
        niveau_source="principale",
        niveau_destination="coffre",
        poste_id=None,
        montant_envoye=10_000,
        motif="Reversement au coffre",
    )
    resultat = receptionner_transfert(
        db, _courant(responsable, agence), transfert.id, montant_compte=10_000
    )

    assert resultat.statut == "receptionne"


def test_coffre_refuse_pour_un_acteur_dune_autre_agence_meme_avec_role_reseau(
    db: Session, agence: Agency, niveaux: None, compte_transit_pose: CaisseParametres
) -> None:
    """Le point validé explicitement (§2) : ÉGALITÉ STRICTE d'agence, PAS condition_perimetre —
    un acteur `voit_tout=True` (rôle réseau type direction/audit) doit rester REFUSÉ sur le
    coffre d'une agence qu'il ne dirige pas. C'est la RÉCEPTION, pas l'initiation, qui rend ce
    cas testable : l'agence d'un transfert est TOUJOURS l'agence courante de qui l'initie
    (`_agence_courante`), donc l'égalité est triviale à l'initiation — seule la réception permet
    à un acteur d'une AUTRE agence d'atteindre le contrôle (condition_perimetre du chargement
    est déjà contourné par voit_tout, exprès, pour isoler CE contrôle-ci)."""
    autre_agence = Agency(code=f"AG-{uuid.uuid4().hex[:6]}", name="Autre agence")
    db.add(autre_agence)
    db.flush()
    principal = _utilisateur(db, agence)
    _designer_principal(db, agence, principal)

    transfert = initier_transfert(
        db,
        _courant(principal, agence),
        niveau_source="principale",
        niveau_destination="coffre",
        poste_id=None,
        montant_envoye=10_000,
        motif="Reversement au coffre",
    )

    intrus_reseau = _utilisateur(db, autre_agence)
    courant_intrus = UtilisateurCourant(
        user_id=intrus_reseau.id,
        roles=(),
        permissions=PERMISSIONS_TRANSFERT,  # détient bien caisse.coffre.gerer
        primary_agency_id=autre_agence.id,
        agency_id=autre_agence.id,  # PAS l'agence du transfert
        voit_tout=True,  # rôle réseau : contourne condition_perimetre, PAS ce contrôle-ci
    )

    with pytest.raises(ResponsableCoffreRequisError):
        receptionner_transfert(db, courant_intrus, transfert.id, montant_compte=10_000)


def test_principale_autorisee_pour_le_caissier_principal_designe(
    db: Session, agence: Agency, niveaux: None, compte_transit_pose: CaisseParametres
) -> None:
    responsable = _utilisateur(db, agence)
    principal = _utilisateur(db, agence)
    _designer_principal(db, agence, principal)

    transfert = initier_transfert(
        db,
        _courant(responsable, agence),
        niveau_source="coffre",
        niveau_destination="principale",
        poste_id=None,
        montant_envoye=10_000,
        motif="Alimentation de la principale",
    )
    resultat = receptionner_transfert(
        db, _courant(principal, agence), transfert.id, montant_compte=10_000
    )

    assert resultat.statut == "receptionne"


def test_principale_refusee_pour_un_autre_caissier(
    db: Session, agence: Agency, niveaux: None, compte_transit_pose: CaisseParametres
) -> None:
    responsable = _utilisateur(db, agence)
    principal_designe = _utilisateur(db, agence)
    autre_caissier = _utilisateur(db, agence)
    _designer_principal(db, agence, principal_designe)

    transfert = initier_transfert(
        db,
        _courant(responsable, agence),
        niveau_source="coffre",
        niveau_destination="principale",
        poste_id=None,
        montant_envoye=10_000,
        motif="Alimentation de la principale",
    )

    with pytest.raises(CaissierPrincipalRequisError):
        receptionner_transfert(
            db, _courant(autre_caissier, agence), transfert.id, montant_compte=10_000
        )


def test_principale_non_designee_refus_propre(
    db: Session, agence: Agency, niveaux: None, compte_transit_pose: CaisseParametres
) -> None:
    responsable = _utilisateur(db, agence)
    caissier = _utilisateur(db, agence)
    # AUCUNE désignation posée — état transitoire légitime.

    transfert = initier_transfert(
        db,
        _courant(responsable, agence),
        niveau_source="coffre",
        niveau_destination="principale",
        poste_id=None,
        montant_envoye=10_000,
        motif="Alimentation de la principale",
    )

    with pytest.raises(CaissierPrincipalNonDesigneError):
        receptionner_transfert(
            db, _courant(caissier, agence), transfert.id, montant_compte=10_000
        )


def test_api_principale_non_designee_422(
    client: TestClient,
    db: Session,
    agence: Agency,
    niveaux: None,
    compte_transit_pose: CaisseParametres,
) -> None:
    responsable = _utilisateur_avec_role(db, agence, "RESPONSABLE_AGENCE")
    caissier = _utilisateur_avec_role(db, agence, "CAISSIER")

    creation = client.post(
        "/caisse/transferts",
        json={
            "niveau_source": "coffre",
            "niveau_destination": "principale",
            "montant_envoye": 10_000,
            "motif": "Sans caissier principal désigné (API)",
        },
        headers=_entete(responsable, agence, "RESPONSABLE_AGENCE"),
    )
    transfert_id = creation.json()["id"]

    reponse = client.post(
        f"/caisse/transferts/{transfert_id}/reception",
        json={"montant_compte": 10_000},
        headers=_entete(caissier, agence, "CAISSIER"),
    )

    assert reponse.status_code == 422
    assert "aucun caissier principal" in reponse.json()["detail"]


def test_secondaire_non_affecte_par_la_responsabilite_des_niveaux(
    db: Session,
    agence: Agency,
    poste: Poste,
    niveaux: None,
    compte_transit_pose: CaisseParametres,
) -> None:
    """Le contrôle secondaire (session titulaire) reste INCHANGÉ : le caissier de guichet n'a
    besoin ni de `caisse.coffre.gerer` ni d'être désigné caissier principal — juste sa session
    ouverte sur SON poste, exactement comme avant le sous-chantier 3."""
    principal = _utilisateur(db, agence)
    caissier_guichet = _utilisateur(db, agence)
    _designer_principal(db, agence, principal)
    _session_ouverte(db, poste, caissier_guichet)

    # Permissions RÉELLES d'un CAISSIER (pas caisse.coffre.gerer, pas de désignation) — même
    # frozenset que le seed accorde réellement à ce rôle.
    permissions_caissier_reelles = frozenset(
        {"caisse.transfert.initier", "caisse.transfert.valider"}
    )
    courant_guichet = UtilisateurCourant(
        user_id=caissier_guichet.id,
        roles=(),
        permissions=permissions_caissier_reelles,
        primary_agency_id=agence.id,
        agency_id=agence.id,
        voit_tout=False,
    )

    transfert = initier_transfert(
        db,
        _courant(principal, agence),
        niveau_source="principale",
        niveau_destination="secondaire",
        poste_id=poste.id,
        montant_envoye=10_000,
        motif="Approvisionnement du guichet",
    )
    resultat = receptionner_transfert(
        db, courant_guichet, transfert.id, montant_compte=10_000
    )

    assert resultat.statut == "receptionne"
