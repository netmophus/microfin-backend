"""Audit transactionnel (C5) et choix d'agence multi-agences (C6) — sous-bloc 3d.

Tests d'intégration, vraie base, isolation par SAVEPOINT. Le trigger de chaînage (0003)
pose chain_hash sous verrou consultatif ; on vérifie ici QUELS événements sont écrits, que
le chaînage tient, et qu'aucun secret n'y figure.

AUCUN SECRET EN DUR : mots de passe fabriqués par générateur (secrets).
"""

import secrets
import string
import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import engine
from app.modules.audit.models import AuditLog
from app.modules.parameters.models import Agency
from app.modules.security.auth import (
    SEUIL_VERROUILLAGE,
    ActionAudit,
    CauseEchec,
    EchecAuthentificationError,
    RafraichissementError,
    authentifier,
    rafraichir,
)
from app.modules.security.jwt import decoder_access_token
from app.modules.security.models import Role, User, UserAgency, UserRole
from app.modules.security.password import hasher_mot_de_passe

pytestmark = pytest.mark.integration

IP_TEST = "203.0.113.7"  # TEST-NET-3 (RFC 5737)


def _mot_de_passe_conforme() -> str:
    familles = [string.ascii_uppercase, string.ascii_lowercase, string.digits, string.punctuation]
    alphabet = "".join(familles)
    caracteres = [secrets.choice(f) for f in familles]
    caracteres += [secrets.choice(alphabet) for _ in range(12)]
    secrets.SystemRandom().shuffle(caracteres)
    return "".join(caracteres)


@pytest.fixture
def db() -> Generator[Session, None, None]:
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(
        bind=connection,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
    )
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def mot_de_passe() -> str:
    return _mot_de_passe_conforme()


@pytest.fixture
def agence(db: Session) -> Agency:
    agence = Agency(code=f"AG-{uuid.uuid4().hex[:8]}", name="Agence de test")
    db.add(agence)
    db.flush()
    return agence


@pytest.fixture
def utilisateur(db: Session, agence: Agency, mot_de_passe: str) -> User:
    role = db.execute(select(Role).where(Role.code == "CAISSIER")).scalar_one()
    suffixe = uuid.uuid4().hex[:8]
    user = User(
        matricule=f"MAT-{suffixe}",
        email=f"u.{suffixe}@example.com",
        username=f"user_{suffixe}",
        password_hash=hasher_mot_de_passe(mot_de_passe),
        last_name="Test",
        first_name="U",
        primary_agency_id=agence.id,
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.flush()
    return user


def _actions_auditees(db: Session, user_id: uuid.UUID) -> list[str]:
    return list(
        db.execute(
            select(AuditLog.action)
            .where(AuditLog.user_id == user_id)
            .order_by(AuditLog.occurred_at)
        ).scalars()
    )


def _echouer(db: Session, user: User, fois: int) -> None:
    for _ in range(fois):
        with pytest.raises(EchecAuthentificationError):
            authentifier(db, user.username, _mot_de_passe_conforme())


# --- ce qui est audité ---------------------------------------------------------------


def test_une_connexion_reussie_est_auditee(
    db: Session, utilisateur: User, mot_de_passe: str, agence: Agency
) -> None:
    authentifier(db, utilisateur.username, mot_de_passe, ip=IP_TEST, user_agent="pytest")

    ligne = db.execute(
        select(AuditLog).where(
            AuditLog.user_id == utilisateur.id, AuditLog.action == ActionAudit.LOGIN_SUCCESS
        )
    ).scalar_one()
    assert ligne.agency_id == agence.id
    assert str(ligne.ip_address) == IP_TEST
    assert ligne.user_agent == "pytest"
    assert ligne.new_values == {"roles": ["CAISSIER"]}


def test_les_echecs_isoles_ne_sont_pas_audites(db: Session, utilisateur: User) -> None:
    # 4 échecs : le compteur monte, mais RIEN n'est écrit dans le journal indélébile.
    _echouer(db, utilisateur, 4)
    assert _actions_auditees(db, utilisateur.id) == []


def test_seule_la_pose_du_verrou_est_auditee(db: Session, utilisateur: User) -> None:
    _echouer(db, utilisateur, SEUIL_VERROUILLAGE)  # le 5e pose le verrou
    actions = _actions_auditees(db, utilisateur.id)
    # Un seul événement, malgré 5 échecs : le verrouillage, pas chaque échec.
    assert actions == [ActionAudit.ACCOUNT_LOCKED]
    ligne = db.execute(
        select(AuditLog).where(AuditLog.action == ActionAudit.ACCOUNT_LOCKED)
    ).scalar_one()
    assert ligne.new_values == {"lockout_count": 1}


def test_un_compte_inexistant_nest_jamais_audite(db: Session, mot_de_passe: str) -> None:
    """Aucune trace : ni flooding par énumération, ni écriture qui rouvrirait le timing.

    L'assertion porte sur le NOMBRE DE LIGNES AJOUTÉES par cette tentative, et non sur le
    contenu global de la table. Le premier jet interrogeait tout `audit.audit_logs` et
    affirmait qu'aucun `login.success` n'y figurait : il ne passait que tant qu'aucune
    connexion RÉELLE n'avait été committée. La première validation de bout en bout du
    frontend en a écrit — dans une table immuable, donc définitivement — et le test a viré
    au rouge sans qu'aucun code de production n'ait changé.

    Compter avant et après est à la fois plus juste (c'est bien « cette tentative n'écrit
    rien » qu'on veut affirmer, pas « la table est vierge ») et indépendant de ce que la
    base contient par ailleurs.
    """
    avant = db.execute(select(func.count()).select_from(AuditLog)).scalar_one()

    with pytest.raises(EchecAuthentificationError):
        authentifier(db, f"fantome_{uuid.uuid4().hex[:8]}", mot_de_passe)

    apres = db.execute(select(func.count()).select_from(AuditLog)).scalar_one()
    assert apres == avant


def test_un_refresh_reussi_nest_pas_audite(
    db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    connexion = authentifier(db, utilisateur.username, mot_de_passe)
    rafraichir(db, connexion.refresh_token)
    # Bruit routinier : le refresh réussi ne laisse aucune trace. Seul le login figure.
    assert _actions_auditees(db, utilisateur.id) == [ActionAudit.LOGIN_SUCCESS]


def test_la_detection_de_vol_est_auditee(db: Session, utilisateur: User, mot_de_passe: str) -> None:
    connexion = authentifier(db, utilisateur.username, mot_de_passe)
    rafraichir(db, connexion.refresh_token)
    with pytest.raises(RafraichissementError):
        rafraichir(db, connexion.refresh_token)  # rejeu → vol

    assert ActionAudit.TOKEN_REUSE_DETECTED in _actions_auditees(db, utilisateur.id)


def test_un_refresh_refuse_pour_compte_indisponible_est_audite(
    db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    connexion = authentifier(db, utilisateur.username, mot_de_passe)
    utilisateur.is_active = False
    db.flush()
    with pytest.raises(RafraichissementError):
        rafraichir(db, connexion.refresh_token)

    assert ActionAudit.REFRESH_DENIED_ACCOUNT_UNAVAILABLE in _actions_auditees(db, utilisateur.id)


# --- chaînage ------------------------------------------------------------------------


def test_le_chainage_lie_deux_evenements(db: Session, utilisateur: User, mot_de_passe: str) -> None:
    """chain_hash est posé et previous_chain_hash lie chaque maillon au précédent.

    Deux connexions successives → deux lignes. Le previous_chain_hash de la 2e doit être
    le chain_hash de la 1re : la chaîne est continue, personne n'a pu insérer entre.
    """
    authentifier(db, utilisateur.username, mot_de_passe)
    authentifier(db, utilisateur.username, mot_de_passe)

    lignes = (
        db.execute(
            select(AuditLog)
            .where(AuditLog.user_id == utilisateur.id)
            .order_by(AuditLog.chain_hash)  # ordre stable, seulement pour déballer les 2
        )
        .scalars()
        .all()
    )
    assert len(lignes) == 2
    a, b = lignes
    assert a.chain_hash is not None and len(a.chain_hash) == 64
    assert b.chain_hash is not None and len(b.chain_hash) == 64

    # Les deux maillons sont chaînés : le previous de l'un est le chain_hash de l'autre.
    # On teste dans les deux sens (XOR) sans présumer de la valeur de départ de la chaîne :
    # exactement un lien existe, la chaîne ne fourche pas.
    lien_a_vers_b = b.previous_chain_hash == a.chain_hash
    lien_b_vers_a = a.previous_chain_hash == b.chain_hash
    assert lien_a_vers_b ^ lien_b_vers_a


def test_le_chain_hash_est_pose_par_la_base_pas_par_le_service(
    db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    # Le service n'écrit jamais chain_hash (il ne le fournit pas) : c'est le trigger.
    authentifier(db, utilisateur.username, mot_de_passe)
    ligne = db.execute(select(AuditLog).where(AuditLog.user_id == utilisateur.id)).scalar_one()
    assert ligne.chain_hash is not None
    # Le tout premier maillon d'une chaîne a un previous NULL ; les suivants non. On ne
    # présume pas de la position, mais chain_hash est TOUJOURS posé.
    assert len(ligne.chain_hash) == 64


# --- aucun secret dans l'audit -------------------------------------------------------


def test_aucun_secret_ne_figure_dans_laudit(
    db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    """Ni password_hash, ni refresh token en clair, ne doivent atterrir dans le journal."""
    hash_stocke = utilisateur.password_hash
    connexion = authentifier(db, utilisateur.username, mot_de_passe, ip=IP_TEST)
    rafraichir(db, connexion.refresh_token)
    with pytest.raises(RafraichissementError):
        rafraichir(db, connexion.refresh_token)  # génère aussi un événement de vol

    lignes = db.execute(select(AuditLog).where(AuditLog.user_id == utilisateur.id)).scalars().all()
    assert lignes  # il y a bien des lignes à inspecter
    for ligne in lignes:
        empreinte = f"{ligne.new_values} {ligne.old_values}"
        assert hash_stocke not in empreinte
        assert connexion.refresh_token not in empreinte
        assert mot_de_passe not in empreinte


# --- choix d'agence C6 ---------------------------------------------------------------


def test_sans_agence_demandee_le_token_porte_lagence_de_rattachement(
    db: Session, utilisateur: User, mot_de_passe: str, agence: Agency
) -> None:
    resultat = authentifier(db, utilisateur.username, mot_de_passe)
    assert decoder_access_token(resultat.access_token).agency_id == agence.id


def test_une_agence_habilitee_est_acceptee(
    db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    autre = Agency(code=f"AG-{uuid.uuid4().hex[:8]}", name="Autre agence")
    db.add(autre)
    db.flush()
    db.add(UserAgency(user_id=utilisateur.id, agency_id=autre.id))
    db.flush()

    resultat = authentifier(db, utilisateur.username, mot_de_passe, agence_demandee=autre.id)
    assert decoder_access_token(resultat.access_token).agency_id == autre.id


def test_une_agence_non_habilitee_est_refusee_et_auditee(
    db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    interdite = Agency(code=f"AG-{uuid.uuid4().hex[:8]}", name="Agence interdite")
    db.add(interdite)
    db.flush()

    with pytest.raises(EchecAuthentificationError) as capture:
        authentifier(db, utilisateur.username, mot_de_passe, agence_demandee=interdite.id)
    assert capture.value.cause == CauseEchec.AGENCE_NON_AUTORISEE

    # Refus audité, JAMAIS de repli silencieux : aucune connexion réussie n'a eu lieu.
    actions = _actions_auditees(db, utilisateur.id)
    assert ActionAudit.LOGIN_AGENCY_DENIED in actions
    assert ActionAudit.LOGIN_SUCCESS not in actions
    ligne = db.execute(
        select(AuditLog).where(AuditLog.action == ActionAudit.LOGIN_AGENCY_DENIED)
    ).scalar_one()
    assert ligne.new_values == {"agence_demandee": str(interdite.id)}


def test_un_refus_dagence_ne_touche_pas_le_compteur_dechecs(
    db: Session, utilisateur: User, mot_de_passe: str
) -> None:
    # Le mot de passe était bon : un refus d'autorisation n'est pas un échec d'auth.
    interdite = Agency(code=f"AG-{uuid.uuid4().hex[:8]}", name="Interdite")
    db.add(interdite)
    db.flush()
    with pytest.raises(EchecAuthentificationError):
        authentifier(db, utilisateur.username, mot_de_passe, agence_demandee=interdite.id)
    assert utilisateur.failed_attempts == 0
