"""Caisse CA1 — sessions : ouverture, fermeture, calcul de l'écart.

LE TEST CENTRAL (`test_solde_theorique_...`) est la preuve que la note attend : le solde
théorique est comparé à un calcul de RÉFÉRENCE INDÉPENDANT — la somme, faite par LE TEST
lui-même (pas par le code sous test), des mouvements RÉELS effectués (dépôt épargne, retrait,
décaissement crédit, remboursement) — jamais le même chemin de code qui pourrait dissimuler la
même erreur des deux côtés. Deux pièges glissés EXPRÈS et volontairement gros (999 999 F,
777 777 F) pour qu'un filtre qui ne mord pas saute aux yeux plutôt que de se noyer dans
l'arrondi : un mouvement AVANT l'ouverture, et un mouvement d'un AUTRE caissier PENDANT la
fenêtre — les deux ne doivent PAS compter (décision : une session par caissier).

Le reste : les garde-fous vus MORDRE pour de vrai (une session ouverte par caissier, IDOR sur
la fermeture, jamais de blocage sur écart en CA1), pas seulement déclarés.
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
from app.modules.caisse.service import (
    RattachementManquantError,
    SessionDejaOuverteError,
    SessionIntrouvableError,
    calculer_solde_theorique,
    fermer_session,
    lire_session,
    lister_sessions_manquantes,
    ouvrir_session,
)
from app.modules.credit.decaissement import decaisser
from app.modules.credit.demandes import creer_demande, decider
from app.modules.credit.models import Product as CreditProduct
from app.modules.credit.remboursement import rembourser
from app.modules.epargne import service as epargne_service
from app.modules.epargne.guichet import deposer, retirer
from app.modules.epargne.models import Product as SavingsProduct
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
    """Crée une agence rattachée — ET son poste (Bloc A, `ouvrir_session()` résout maintenant via
    `caisse.postes`, pas directement `Agency.compte_caisse_id`). Le poste reprend le même compte,
    miroir du backfill de la migration 0041."""
    compte_id = _cid(db, "101111")
    agence = Agency(code=code, name=f"Agence {code}", compte_caisse_id=compte_id)
    db.add(agence)
    db.flush()
    db.add(
        Poste(
            agency_id=agence.id,
            code="01",
            libelle="Caisse principale",
            compte_caisse_id=compte_id,
        )
    )
    db.flush()
    return agence


def _tier(db: Session, agence: Agency, suffixe: str) -> uuid.UUID:
    tier_id = db.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES (:n, 'individual', :a, 'actif') RETURNING id"
        ),
        {"n": f"M-CXA-{suffixe}", "a": agence.id},
    ).scalar_one()
    nat = db.execute(text("SELECT id FROM parameters.countries LIMIT 1")).scalar_one()
    db.execute(
        text(
            "INSERT INTO tiers.individual_profiles "
            "(tier_id, last_name, first_name, birth_date, gender, nationality_id) "
            "VALUES (:t, 'Test', 'Caisse', '1985-01-01', 'M', :nat)"
        ),
        {"t": tier_id, "nat": nat},
    )
    return tier_id


def _produit_epargne(db: Session) -> SavingsProduct:
    produit = SavingsProduct(
        code=f"EPX{uuid.uuid4().hex[:5]}",
        name="Épargne test caisse",
        type="a_vue",
        compte_epargne_id=_cid(db, "251111"),
        compte_epargne_client_id=_cid(db, "251121"),
    )
    db.add(produit)
    db.flush()
    return produit


def _compte_epargne(db: Session, agence: Agency, tier_id: uuid.UUID, produit: SavingsProduct):
    return epargne_service.ouvrir_compte(
        db, tier_id=tier_id, product_id=produit.id, agency_id=agence.id, par=None
    )


def _produit_credit(db: Session) -> CreditProduct:
    produit = CreditProduct(
        code=f"CRX{uuid.uuid4().hex[:5]}",
        name="Crédit test caisse",
        compte_credit_membre_id=_cid(db, "202211"),
        compte_credit_client_id=_cid(db, "202221"),
        compte_produits_interets_id=_cid(db, "7021"),
        taux_bp=0,
        methode_amortissement="capital_constant",
    )
    db.add(produit)
    db.flush()
    return produit


def _demande_decaissee(
    db: Session, agence: Agency, tier_id: uuid.UUID, produit, *, montant: int, par: uuid.UUID
):
    """Décaissée en mode CAISSE (défaut) — un montant qui claque sur 101111. `par` attribue
    l'écriture au caissier, sinon `created_by` reste NULL et le filtre du calcul l'exclurait à
    tort (piège qu'on a fait mordre une première fois en écrivant ce test)."""
    demande = creer_demande(
        db, tier_id=tier_id, agency_id=agence.id, product_id=produit.id,
        montant_demande=montant, duree_echeances=1, objet="Test caisse", par=par,
    )
    decider(db, demande, decision="approuve", montant_decide=montant, motif="OK", par=par)
    decaisser(db, demande, par=par)
    db.flush()
    return demande


def _utilisateur(db: Session, agence: Agency, suffixe: str, role_code: str = "CAISSIER") -> User:
    role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
    user = User(
        matricule=f"MAT-CXA-{suffixe}", email=f"cxa{suffixe}@ex.com", username=f"cxa{suffixe}",
        password_hash=hasher_mot_de_passe("Motdepasse!123"),
        last_name="Caissier", first_name=suffixe,
        primary_agency_id=agence.id,
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.flush()
    return user


def _poste_principal(db: Session, agence: Agency) -> Poste:
    """Le poste que `_agence()` a créé (code "01", Bloc A)."""
    return db.execute(
        select(Poste).where(Poste.agency_id == agence.id, Poste.code == "01")
    ).scalar_one()


def _assigner(db: Session, poste: Poste, user_id: uuid.UUID) -> None:
    """Idempotent : plusieurs appels pour le même (poste, utilisateur) — par exemple un caissier
    qui rouvre une session après en avoir fermé une précédente — ne doivent pas tenter une
    seconde ligne sur la même clé composite."""
    deja = db.execute(
        select(PosteAssignation).where(
            PosteAssignation.poste_id == poste.id, PosteAssignation.user_id == user_id
        )
    ).scalar_one_or_none()
    if deja is None:
        db.add(PosteAssignation(poste_id=poste.id, user_id=user_id))
        db.flush()


def _ouvrir(
    db: Session, courant: UtilisateurCourant, agence: Agency, *, fonds_initial: int
) -> CaisseSession:
    """Assigne l'acteur au poste principal de l'agence (précondition Bloc C) puis ouvre une
    session — centralise la paire assignation+ouverture répétée par presque tous les tests de ce
    fichier. `ouvrir_session()` reste testé nu là où la résolution du poste EST le point (ex.
    `test_ouverture_sans_compte_caisse_rattache_refuse`)."""
    poste = _poste_principal(db, agence)
    _assigner(db, poste, courant.user_id)
    return ouvrir_session(db, courant, poste_id=poste.id, fonds_initial=fonds_initial)


def _poste_id_assigne(db: Session, agence: Agency, user: User) -> uuid.UUID:
    """Pour les tests HTTP : assigne l'utilisateur au poste principal et renvoie son id, à
    inclure dans le corps JSON de `POST /caisse/sessions` (`poste_id` désormais obligatoire,
    Bloc C)."""
    poste = _poste_principal(db, agence)
    _assigner(db, poste, user.id)
    return poste.id


def _courant(user: User, agence: Agency) -> UtilisateurCourant:
    return UtilisateurCourant(
        user_id=user.id,
        roles=("CAISSIER",),
        permissions=frozenset(
            {
                "epargne.operation.deposit", "epargne.operation.withdraw",
                "caisse.session.open", "caisse.session.close", "caisse.session.read",
            }
        ),
        primary_agency_id=agence.id, agency_id=agence.id, voit_tout=False,
    )


def _courant_role(
    user: User,
    agence: Agency,
    *,
    role_code: str,
    permissions: frozenset[str],
    voit_tout: bool = False,
) -> UtilisateurCourant:
    """Variante de `_courant` pour les rôles de contrôle (responsable/audit/direction) —
    lecture des sessions d'AUTRES caissiers (caisse.session.read.autres)."""
    return UtilisateurCourant(
        user_id=user.id,
        roles=(role_code,),
        permissions=permissions,
        primary_agency_id=agence.id,
        agency_id=agence.id,
        voit_tout=voit_tout,
    )


def _entete(user: User, agence: Agency, role_code: str = "CAISSIER") -> dict[str, str]:
    jeton = creer_access_token(
        user_id=user.id, roles=[role_code], primary_agency_id=agence.id, agency_id=agence.id
    )
    return {"Authorization": f"Bearer {jeton}"}


# --- LE test central : preuve indépendante, garde-fous vus mordre --------------------------


def test_solde_theorique_egale_fonds_initial_plus_mouvements_reels_de_la_fenetre(
    db: Session,
) -> None:
    agence = _agence(db, "CXA1")
    tier_id = _tier(db, agence, "1")
    produit_epargne = _produit_epargne(db)
    compte = _compte_epargne(db, agence, tier_id, produit_epargne)
    produit_credit = _produit_credit(db)
    caissier_user = _utilisateur(db, agence, "1")
    caissier = _courant(caissier_user, agence)

    # PIÈGE 1 — mouvement AVANT l'ouverture, gros et rond pour sauter aux yeux si compté à tort.
    # Backdaté EXPLICITEMENT : dans UNE seule transaction (comme ce test), NOW() de Postgres est
    # figé à l'heure de début de transaction — un simple ordre chronologique d'appels ne suffit
    # PAS à produire deux instants différents (piège trouvé en écrivant ce test lui-même).
    deposer(db, caissier, compte.id, 999_999)
    # Une pièce VALIDÉE est immuable (trigger) — désactivé le temps de CE backdatage simulé,
    # comme la purge base_vierge de test_creer_admin.py (transaction annulée au rollback ;
    # aucun effet sur la garantie réelle d'immuabilité). trg_entry_equilibre est une contrainte
    # DEFERRED : sans la forcer à s'exécuter maintenant, l'ALTER TABLE refuse (« pending trigger
    # events »).
    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    db.execute(text("ALTER TABLE comptabilite.journal_entries DISABLE TRIGGER trg_entry_immuable"))
    db.execute(
        text(
            "UPDATE comptabilite.journal_entries SET posted_at = NOW() - INTERVAL '1 day' "
            "WHERE created_by = :c"
        ),
        {"c": caissier_user.id},
    )
    db.execute(text("ALTER TABLE comptabilite.journal_entries ENABLE TRIGGER trg_entry_immuable"))
    db.flush()

    session = _ouvrir(db, caissier, agence, fonds_initial=100_000)
    db.flush()

    # Les SEULS mouvements qui doivent compter : APRÈS l'ouverture, PAR CE caissier.
    deposer(db, caissier, compte.id, 50_000)  # +50 000
    retirer(db, caissier, compte.id, 20_000)  # -20 000
    demande = _demande_decaissee(
        db, agence, tier_id, produit_credit, montant=80_000, par=caissier_user.id
    )  # -80 000
    rembourser(db, demande, montant=15_000, par=caissier_user.id)  # +15 000

    # PIÈGE 2 — un AUTRE caissier, MÊME agence, MÊME compte de caisse, PENDANT la fenêtre.
    autre_user = _utilisateur(db, agence, "2")
    autre = _courant(autre_user, agence)
    deposer(db, autre, compte.id, 777_777)
    db.flush()

    theorique = calculer_solde_theorique(db, session)

    # Calcul de RÉFÉRENCE — fait ICI par le test, pas par le code sous test : si l'un des deux
    # pièges n'était pas filtré, 999 999 ou 777 777 apparaîtrait dans l'écart et l'assertion
    # échouerait de façon flagrante, pas silencieusement.
    attendu = 100_000 + 50_000 - 20_000 - 80_000 + 15_000
    assert attendu == 65_000  # le calcul de référence lui-même, lisible sans arithmétique cachée
    assert theorique == attendu


def test_solde_theorique_avant_toute_operation_egale_le_fonds_initial(db: Session) -> None:
    agence = _agence(db, "CXA2")
    caissier_user = _utilisateur(db, agence, "3")
    caissier = _courant(caissier_user, agence)

    session = _ouvrir(db, caissier, agence, fonds_initial=42_000)
    db.flush()

    assert calculer_solde_theorique(db, session) == 42_000


# --- Garde-fous vus mordre, pas seulement déclarés ------------------------------------------


def test_deux_sessions_ouvertes_pour_le_meme_caissier_refuse(db: Session) -> None:
    agence = _agence(db, "CXA3")
    caissier_user = _utilisateur(db, agence, "4")
    caissier = _courant(caissier_user, agence)
    _ouvrir(db, caissier, agence, fonds_initial=10_000)
    db.flush()

    with pytest.raises(SessionDejaOuverteError):
        _ouvrir(db, caissier, agence, fonds_initial=20_000)


def test_le_garde_fou_mord_meme_en_base_index_unique_partiel(db: Session) -> None:
    """Contourne le contrôle APPLICATIF (INSERT direct) pour prouver que l'index unique partiel
    est un VRAI dernier rempart, pas seulement documenté — même exigence que le reste du projet
    ([[mecanismes-verts-mais-inertes]])."""
    agence = _agence(db, "CXA4")
    caissier_user = _utilisateur(db, agence, "5")
    caissier = _courant(caissier_user, agence)
    _ouvrir(db, caissier, agence, fonds_initial=10_000)
    db.flush()

    with pytest.raises(Exception, match=r"uq_caisse_sessions_caissier_ouverte|duplicate key"):
        db.execute(
            text(
                "INSERT INTO caisse.sessions (agency_id, caissier_id, poste_id, "
                "compte_caisse_id, fonds_initial, status) "
                "SELECT agency_id, caissier_id, poste_id, compte_caisse_id, fonds_initial, "
                "status FROM caisse.sessions WHERE caissier_id = :c LIMIT 1"
            ),
            {"c": caissier_user.id},
        )


def test_ouverture_sans_compte_caisse_rattache_refuse(db: Session) -> None:
    """Distinct de `PosteIntrouvableError` (Bloc C) : le poste EXISTE, est actif, et l'acteur y
    est assigné — seul le rattachement comptable manque. Un poste absent/inactif/non-assigné est
    couvert ailleurs (`test_caisse_postes.py`)."""
    agence = Agency(code="CXA5", name="Agence sans caisse")  # compte_caisse_id NULL, exprès
    db.add(agence)
    db.flush()
    poste = Poste(
        agency_id=agence.id, code="01", libelle="Caisse sans compte", compte_caisse_id=None
    )
    db.add(poste)
    db.flush()
    caissier_user = _utilisateur(db, agence, "6")
    caissier = _courant(caissier_user, agence)
    _assigner(db, poste, caissier_user.id)

    with pytest.raises(RattachementManquantError):
        ouvrir_session(db, caissier, poste_id=poste.id, fonds_initial=10_000)


def test_fermer_la_session_dun_autre_caissier_refuse_404_pas_403(db: Session) -> None:
    agence = _agence(db, "CXA6")
    proprietaire = _courant(_utilisateur(db, agence, "7"), agence)
    intrus = _courant(_utilisateur(db, agence, "8"), agence)
    session = _ouvrir(db, proprietaire, agence, fonds_initial=10_000)
    db.flush()

    with pytest.raises(SessionIntrouvableError):  # -> 404 côté router, jamais 403 (IDOR)
        fermer_session(db, intrus, session.id, montant_reel=10_000)


def test_fermeture_ne_bloque_jamais_meme_avec_un_ecart_enorme(db: Session) -> None:
    """CA1 : aucune politique de tolérance/blocage n'existe encore (CA2). La fermeture doit
    réussir même face à un écart énorme — empêcher un caissier de rentrer chez lui a un coût
    réel, décision déjà actée."""
    agence = _agence(db, "CXA7")
    caissier = _courant(_utilisateur(db, agence, "9"), agence)
    session = _ouvrir(db, caissier, agence, fonds_initial=10_000)
    db.flush()

    resultat = fermer_session(db, caissier, session.id, montant_reel=999_999_999)

    assert resultat.session.status == "fermee"
    assert resultat.ecart == 999_999_999 - 10_000
    assert resultat.session.ecart == resultat.ecart


def test_fermeture_fige_lecart_et_le_calcul_ne_bouge_plus_apres(db: Session) -> None:
    """Le solde théorique figé à la fermeture ne doit PAS changer si d'autres mouvements du même
    caissier arrivent après coup (sur une session suivante, par exemple) — un cache, jamais un
    calcul qui continuerait de dériver sous les pieds d'une session déjà close."""
    agence = _agence(db, "CXA8")
    tier_id = _tier(db, agence, "10")
    produit_epargne = _produit_epargne(db)
    compte = _compte_epargne(db, agence, tier_id, produit_epargne)
    caissier_user = _utilisateur(db, agence, "10")
    caissier = _courant(caissier_user, agence)

    session = _ouvrir(db, caissier, agence, fonds_initial=10_000)
    db.flush()
    deposer(db, caissier, compte.id, 5_000)
    resultat = fermer_session(db, caissier, session.id, montant_reel=15_000)
    assert resultat.solde_theorique == 15_000
    assert resultat.ecart == 0

    # Une NOUVELLE session, un NOUVEAU dépôt — ne doit rien changer à la session déjà fermée.
    _ouvrir(db, caissier, agence, fonds_initial=0)
    db.flush()
    deposer(db, caissier, compte.id, 1_000)

    session_rechargee = db.get(CaisseSession, session.id)
    assert session_rechargee.solde_theorique_cloture == 15_000
    assert session_rechargee.ecart == 0


def test_deux_caissiers_meme_agence_sessions_simultanees_independantes(db: Session) -> None:
    """La raison d'être du modèle « par caissier », prouvée : deux sessions ouvertes en même
    temps, MÊME compte de caisse sous-jacent — chacune ne voit QUE ses propres mouvements.

    Bloc A : un poste n'a plus qu'une session ouverte à la fois (`uq_caisse_sessions_poste_
    ouverte`) — deux caissiers ne peuvent plus partager le MÊME poste simultanément (un
    guichet physique n'a qu'un tiroir). Bloc C : chaque caissier ouvre sur SON propre poste,
    explicitement choisi — l'agence en a deux, un second posé ici (pas de CRUD Bloc B, hors
    périmètre), sur le MÊME compte, pour isoler ce que ce test veut PROUVER — l'isolation par
    caissier dans `calculer_solde_theorique` — du choix du poste, qui a ses propres tests
    dédiés."""
    agence = _agence(db, "CXA9")
    tier_id = _tier(db, agence, "11")
    produit_epargne = _produit_epargne(db)
    compte = _compte_epargne(db, agence, tier_id, produit_epargne)
    a = _courant(_utilisateur(db, agence, "11"), agence)
    b_user = _utilisateur(db, agence, "12")
    b = _courant(b_user, agence)

    session_a = _ouvrir(db, a, agence, fonds_initial=1_000)
    poste_a = db.get(Poste, session_a.poste_id)
    poste_b = Poste(
        agency_id=agence.id,
        code="02",
        libelle="Caisse 2",
        compte_caisse_id=poste_a.compte_caisse_id,
    )
    db.add(poste_b)
    db.flush()
    _assigner(db, poste_b, b_user.id)
    session_b = ouvrir_session(db, b, poste_id=poste_b.id, fonds_initial=2_000)

    deposer(db, a, compte.id, 500)
    deposer(db, b, compte.id, 300)

    assert calculer_solde_theorique(db, session_a) == 1_500
    assert calculer_solde_theorique(db, session_b) == 2_300


# --- Le routeur : permissions, IDOR, bout en bout -------------------------------------------


def test_endpoint_ouverture_puis_fermeture_bout_en_bout(client: TestClient, db: Session) -> None:
    agence = _agence(db, "CXB1")
    caissier_user = _utilisateur(db, agence, "20")
    entete = _entete(caissier_user, agence)
    poste_id = _poste_id_assigne(db, agence, caissier_user)

    ouverture = client.post(
        "/caisse/sessions",
        json={"fonds_initial": 20_000, "poste_id": str(poste_id)},
        headers=entete,
    )
    assert ouverture.status_code == 201
    session_id = ouverture.json()["id"]
    assert ouverture.json()["status"] == "ouverte"

    fermeture = client.post(
        f"/caisse/sessions/{session_id}/fermeture",
        json={"montant_reel": 20_000},
        headers=entete,
    )
    assert fermeture.status_code == 200
    corps = fermeture.json()
    assert corps["status"] == "fermee"
    assert corps["solde_theorique_cloture"] == 20_000
    assert corps["ecart"] == 0


def test_endpoint_sans_permission_403(client: TestClient, db: Session) -> None:
    agence = _agence(db, "CXB2")
    role = db.execute(select(Role).where(Role.code == "AUDITEUR_INTERNE")).scalar_one()
    user = User(
        matricule="MAT-CXB2", email="cxb2@ex.com", username="cxb2",
        password_hash=hasher_mot_de_passe("Motdepasse!123"), last_name="T", first_name="A",
        primary_agency_id=agence.id,
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.flush()
    entete = _entete(user, agence, role_code="AUDITEUR_INTERNE")

    # poste_id syntaxiquement valide mais non résolu : la permission doit refuser avant même
    # que le poste soit examiné — un UUID quelconque suffit à isoler ce que ce test prouve.
    reponse = client.post(
        "/caisse/sessions",
        json={"fonds_initial": 1_000, "poste_id": str(uuid.uuid4())},
        headers=entete,
    )
    assert reponse.status_code == 403


def test_endpoint_fermer_la_session_dun_autre_404(client: TestClient, db: Session) -> None:
    agence = _agence(db, "CXB3")
    proprietaire = _utilisateur(db, agence, "21")
    intrus = _utilisateur(db, agence, "22")
    _ouvrir(db, _courant(proprietaire, agence), agence, fonds_initial=5_000)
    db.commit()
    session_id = db.execute(
        select(CaisseSession.id).where(CaisseSession.caissier_id == proprietaire.id)
    ).scalar_one()

    reponse = client.post(
        f"/caisse/sessions/{session_id}/fermeture",
        json={"montant_reel": 5_000},
        headers=_entete(intrus, agence),
    )
    assert reponse.status_code == 404


def test_endpoint_session_courante_neant_si_aucune_ouverte(client: TestClient, db: Session) -> None:
    agence = _agence(db, "CXB4")
    user = _utilisateur(db, agence, "23")

    reponse = client.get("/caisse/sessions/courante", headers=_entete(user, agence))

    assert reponse.status_code == 200
    assert reponse.json() is None


def test_endpoint_session_courante_reflete_un_mouvement_en_direct(
    client: TestClient, db: Session
) -> None:
    """`solde_theorique_actuel` doit bouger EN DIRECT (session encore ouverte) — c'est le champ
    qu'un caissier consulte en cours de journée, avant toute fermeture. Vérifié via le service
    directement (pas le guichet HTTP épargne, hors périmètre de ce test) pour isoler le seul
    comportement à prouver ici : le routeur caisse recalcule à chaque lecture, sans figer."""
    agence = _agence(db, "CXB5")
    tier_id = _tier(db, agence, "24")
    produit_epargne = _produit_epargne(db)
    compte = _compte_epargne(db, agence, tier_id, produit_epargne)
    caissier_user = _utilisateur(db, agence, "24")
    entete = _entete(caissier_user, agence)
    poste_id = _poste_id_assigne(db, agence, caissier_user)

    ouverture = client.post(
        "/caisse/sessions",
        json={"fonds_initial": 10_000, "poste_id": str(poste_id)},
        headers=entete,
    )
    assert ouverture.status_code == 201
    assert ouverture.json()["solde_theorique_actuel"] == 10_000
    assert ouverture.json()["status"] == "ouverte"

    deposer(db, _courant(caissier_user, agence), compte.id, 5_000)
    db.flush()

    courante = client.get("/caisse/sessions/courante", headers=entete)
    assert courante.status_code == 200
    assert courante.json()["solde_theorique_actuel"] == 15_000

    fermeture = client.post(
        f"/caisse/sessions/{ouverture.json()['id']}/fermeture",
        json={"montant_reel": 15_000},
        headers=entete,
    )
    assert fermeture.status_code == 200
    # Fermée : le champ EN DIRECT s'efface, seul le figé (solde_theorique_cloture) reste
    # pertinent — pas deux nombres qui prétendraient répondre à la même question.
    assert fermeture.json()["solde_theorique_actuel"] is None
    assert fermeture.json()["solde_theorique_cloture"] == 15_000


# --- Lecture élargie (lettre de demande d'explication) : la sienne toujours, une autre --------
# SEULEMENT avec caisse.session.read.autres ET dans le périmètre --------------------------------


def _session_fermee(
    db: Session,
    courant: UtilisateurCourant,
    agence: Agency,
    *,
    fonds_initial: int,
    montant_reel: int,
) -> CaisseSession:
    """Une session ouverte puis fermée, SANS mouvement réel : le solde théorique reste égal au
    fonds initial, donc `montant_reel` pilote directement le signe de l'écart — suffisant pour
    les tests de PÉRIMÈTRE ci-dessous (le calcul du solde théorique lui-même est prouvé plus
    haut, pas à reprouver ici)."""
    session = _ouvrir(db, courant, agence, fonds_initial=fonds_initial)
    db.flush()
    resultat = fermer_session(db, courant, session.id, montant_reel=montant_reel)
    db.flush()
    return resultat.session


def test_lire_session_responsable_meme_agence_autre_caissier(db: Session) -> None:
    agence = _agence(db, "CXC1")
    caissier = _courant(_utilisateur(db, agence, "30"), agence)
    session = _session_fermee(db, caissier, agence, fonds_initial=10_000, montant_reel=8_000)

    responsable_user = _utilisateur(db, agence, "31", role_code="RESPONSABLE_AGENCE")
    responsable = _courant_role(
        responsable_user, agence,
        role_code="RESPONSABLE_AGENCE", permissions=frozenset({"caisse.session.read.autres"}),
    )

    lue = lire_session(db, responsable, session.id)
    assert lue.id == session.id
    assert lue.ecart == -2_000


def test_lire_session_responsable_agence_differente_refuse(db: Session) -> None:
    agence_a = _agence(db, "CXC2")
    agence_b = _agence(db, "CXC3")
    caissier = _courant(_utilisateur(db, agence_a, "32"), agence_a)
    session = _session_fermee(db, caissier, agence_a, fonds_initial=10_000, montant_reel=8_000)

    responsable_user = _utilisateur(db, agence_b, "33", role_code="RESPONSABLE_AGENCE")
    responsable = _courant_role(
        responsable_user, agence_b,
        role_code="RESPONSABLE_AGENCE", permissions=frozenset({"caisse.session.read.autres"}),
    )

    with pytest.raises(SessionIntrouvableError):  # 404, jamais 403 : IDOR
        lire_session(db, responsable, session.id)


def test_lire_session_auditeur_reseau_entier(db: Session) -> None:
    agence = _agence(db, "CXC4")
    caissier = _courant(_utilisateur(db, agence, "34"), agence)
    session = _session_fermee(db, caissier, agence, fonds_initial=10_000, montant_reel=8_000)

    autre_agence = _agence(db, "CXC5")
    auditeur_user = _utilisateur(db, autre_agence, "35", role_code="AUDITEUR_INTERNE")
    auditeur = _courant_role(
        auditeur_user, autre_agence,
        role_code="AUDITEUR_INTERNE",
        permissions=frozenset({"caisse.session.read.autres"}),
        voit_tout=True,
    )

    lue = lire_session(db, auditeur, session.id)
    assert lue.id == session.id


def test_lire_session_caissier_sans_autres_ne_lit_pas_celle_dun_collegue(db: Session) -> None:
    """Comportement INCHANGÉ : un simple CAISSIER (caisse.session.read seul) ne voit toujours
    QUE la sienne, même dans SA PROPRE agence — caisse.session.read.autres est une permission
    à part, pas un sous-produit implicite de caisse.session.read."""
    agence = _agence(db, "CXC6")
    proprietaire = _courant(_utilisateur(db, agence, "36"), agence)
    session = _session_fermee(db, proprietaire, agence, fonds_initial=10_000, montant_reel=8_000)

    collegue = _courant(_utilisateur(db, agence, "37"), agence)  # caisse.session.read seul

    with pytest.raises(SessionIntrouvableError):
        lire_session(db, collegue, session.id)


def test_endpoint_lire_session_responsable_lit_celle_dun_autre_caissier(
    client: TestClient, db: Session
) -> None:
    agence = _agence(db, "CXC7")
    caissier_user = _utilisateur(db, agence, "38")
    caissier = _courant(caissier_user, agence)
    session = _session_fermee(db, caissier, agence, fonds_initial=10_000, montant_reel=7_000)
    db.commit()

    role = db.execute(select(Role).where(Role.code == "RESPONSABLE_AGENCE")).scalar_one()
    responsable_user = User(
        matricule="MAT-CXC7", email="cxc7@ex.com", username="cxc7",
        password_hash=hasher_mot_de_passe("Motdepasse!123"), last_name="T", first_name="R",
        primary_agency_id=agence.id,
    )
    db.add(responsable_user)
    db.flush()
    db.add(UserRole(user_id=responsable_user.id, role_id=role.id))
    db.commit()

    reponse = client.get(
        f"/caisse/sessions/{session.id}",
        headers=_entete(responsable_user, agence, role_code="RESPONSABLE_AGENCE"),
    )
    assert reponse.status_code == 200
    assert reponse.json()["ecart"] == -3_000
    assert reponse.json()["caissier_nom"]  # résolu en clair, pas un UUID nu


# --- Liste des manquants (retrouver une lettre plus tard) --------------------------------------


def test_lister_manquants_caissier_voit_les_siennes_seulement(db: Session) -> None:
    agence = _agence(db, "CXD1")
    a = _courant(_utilisateur(db, agence, "40"), agence)
    b = _courant(_utilisateur(db, agence, "41"), agence)
    session_a = _session_fermee(db, a, agence, fonds_initial=10_000, montant_reel=8_000)
    _session_fermee(db, b, agence, fonds_initial=10_000, montant_reel=6_000)  # pas la sienne

    page = lister_sessions_manquantes(db, a)

    assert [ligne.id for ligne in page.lignes] == [session_a.id]
    assert page.total == 1


def test_lister_manquants_responsable_voit_toute_son_agence(db: Session) -> None:
    agence = _agence(db, "CXD2")
    a = _courant(_utilisateur(db, agence, "42"), agence)
    b = _courant(_utilisateur(db, agence, "43"), agence)
    session_a = _session_fermee(db, a, agence, fonds_initial=10_000, montant_reel=8_000)
    session_b = _session_fermee(db, b, agence, fonds_initial=10_000, montant_reel=6_000)

    responsable_user = _utilisateur(db, agence, "44", role_code="RESPONSABLE_AGENCE")
    responsable = _courant_role(
        responsable_user, agence,
        role_code="RESPONSABLE_AGENCE", permissions=frozenset({"caisse.session.read.autres"}),
    )

    page = lister_sessions_manquantes(db, responsable)

    assert {ligne.id for ligne in page.lignes} == {session_a.id, session_b.id}
    assert page.total == 2


def test_lister_manquants_exclut_excedent_et_ecart_nul(db: Session) -> None:
    agence = _agence(db, "CXD3")
    caissier = _courant(_utilisateur(db, agence, "45"), agence)
    session_manquant = _session_fermee(
        db, caissier, agence, fonds_initial=10_000, montant_reel=8_000
    )
    _session_fermee(db, caissier, agence, fonds_initial=10_000, montant_reel=10_000)  # nul

    autre_caissier = _courant(_utilisateur(db, agence, "46"), agence)
    _session_fermee(
        db, autre_caissier, agence, fonds_initial=10_000, montant_reel=12_000
    )  # excédent

    # Périmètre réseau : voit toutes les sessions fermées de la base, seul le filtre écart<0
    # doit exclure le nul et l'excédent.
    reseau_user = _utilisateur(db, agence, "47", role_code="DIRECTION_GENERALE")
    reseau = _courant_role(
        reseau_user, agence,
        role_code="DIRECTION_GENERALE",
        permissions=frozenset({"caisse.session.read.autres"}),
        voit_tout=True,
    )

    page = lister_sessions_manquantes(db, reseau)

    ids = {ligne.id for ligne in page.lignes}
    assert session_manquant.id in ids
    assert all(ligne.ecart < 0 for ligne in page.lignes)


def test_lister_manquants_pagination(db: Session) -> None:
    agence = _agence(db, "CXD4")
    caissier = _courant(_utilisateur(db, agence, "48"), agence)
    for _ in range(3):
        _session_fermee(db, caissier, agence, fonds_initial=10_000, montant_reel=9_000)

    page = lister_sessions_manquantes(db, caissier, page=1, taille=2)

    assert page.total == 3
    assert len(page.lignes) == 2
    assert page.page == 1
    assert page.taille == 2


def test_endpoint_manquants_sans_permission_403(client: TestClient, db: Session) -> None:
    agence = _agence(db, "CXD5")
    role = db.execute(select(Role).where(Role.code == "CHARGE_CLIENTELE")).scalar_one()
    user = User(
        matricule="MAT-CXD5", email="cxd5@ex.com", username="cxd5",
        password_hash=hasher_mot_de_passe("Motdepasse!123"), last_name="T", first_name="C",
        primary_agency_id=agence.id,
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.flush()

    reponse = client.get(
        "/caisse/sessions",
        params={"manquant": "true"},
        headers=_entete(user, agence, role_code="CHARGE_CLIENTELE"),
    )
    assert reponse.status_code == 403


def test_endpoint_manquants_manquant_false_refuse(client: TestClient, db: Session) -> None:
    agence = _agence(db, "CXD6")
    user = _utilisateur(db, agence, "49")

    reponse = client.get(
        "/caisse/sessions", params={"manquant": "false"}, headers=_entete(user, agence)
    )
    assert reponse.status_code == 422


def test_endpoint_manquants_caissier_retrouve_sa_propre_session(
    client: TestClient, db: Session
) -> None:
    agence = _agence(db, "CXD7")
    caissier_user = _utilisateur(db, agence, "50")
    caissier = _courant(caissier_user, agence)
    session = _session_fermee(db, caissier, agence, fonds_initial=10_000, montant_reel=7_500)
    db.commit()

    reponse = client.get(
        "/caisse/sessions", params={"manquant": "true"}, headers=_entete(caissier_user, agence)
    )
    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["total"] == 1
    assert corps["lignes"][0]["id"] == str(session.id)
    assert corps["lignes"][0]["ecart"] == -2_500
