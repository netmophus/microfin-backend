"""Commande d'amorçage `creer-admin` (brique de `init-imf`).

Ce qu'elle garantit :

  - elle crée un compte utilisable, avec le bon rôle et le drapeau de renouvellement ;
  - elle REFUSE de s'exécuter sur une base déjà peuplée — c'est un amorçage, pas une porte
    dérobée permanente qui contournerait l'audit et le cloisonnement ;
  - le mot de passe généré n'existe qu'en retour de fonction, jamais en base.
"""

import uuid
from collections.abc import Generator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.cli.creer_admin import (
    ROLE_ADMIN,
    AgenceIntrouvableError,
    ComptesDejaPresentsError,
    creer_admin,
)
from app.core.database import engine
from app.modules.parameters.models import Agency
from app.modules.security.models import Role, User
from app.modules.security.password import verifier_mot_de_passe

pytestmark = pytest.mark.integration


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
def base_vierge(db: Session) -> None:
    """Simule une installation neuve, quels que soient les comptes déjà présents.

    NÉCESSAIRE, et pas seulement commode : la base de développement porte désormais un vrai
    compte d'amorçage (créé par la commande elle-même, et committé). Des tests qui
    supposeraient une table `users` vide passeraient au vert sur une base fraîche et
    rougiraient sur toute base ayant servi — c'est-à-dire chez n'importe qui d'autre.

    Le contrôle de `creer_admin` ne compte que les comptes VIVANTS : les marquer supprimés
    dans la transaction du test suffit à recréer les conditions d'une installation neuve, et
    le rollback rend tout à son état d'origine.
    """
    db.execute(update(User).where(User.deleted_at.is_(None)).values(deleted_at=datetime.now(UTC)))
    db.flush()


def _identite() -> dict[str, str]:
    suffixe = uuid.uuid4().hex[:8]
    return {
        "username": f"admin{suffixe}",
        "email": f"admin.{suffixe}@imf.local",
        "matricule": f"ADM-{suffixe}",
        "last_name": "Administrateur",
        "first_name": "Compte",
    }


def test_le_compte_est_utilisable_et_bien_habilite(db: Session, base_vierge: None) -> None:
    resultat = creer_admin(db, **_identite())

    admin = db.get(User, resultat.user_id)
    assert admin is not None
    assert admin.is_active is True
    # Le mot de passe affiché est bien celui du compte : sans ça, l'installateur serait
    # bloqué dehors dès la première connexion, sans moyen de comprendre pourquoi.
    assert verifier_mot_de_passe(resultat.mot_de_passe, admin.password_hash)
    assert [role.code for role in admin.roles] == [ROLE_ADMIN]


def test_le_renouvellement_est_exige_des_la_premiere_connexion(
    db: Session, base_vierge: None
) -> None:
    """Le mot de passe transite par un écran de terminal — donc potentiellement par un
    historique de session ou une capture. Il doit être périssable."""
    resultat = creer_admin(db, **_identite())

    admin = db.get(User, resultat.user_id)
    assert admin is not None
    assert admin.must_change_password is True


def test_le_mot_de_passe_n_est_jamais_stocke_en_clair(db: Session, base_vierge: None) -> None:
    resultat = creer_admin(db, **_identite())

    admin = db.get(User, resultat.user_id)
    assert admin is not None
    assert resultat.mot_de_passe not in admin.password_hash
    assert admin.password_hash.startswith("$argon2")


def test_la_commande_refuse_une_base_deja_peuplee(db: Session, base_vierge: None) -> None:
    """LE garde-fou. Sans lui, cette commande deviendrait un moyen permanent de créer des
    administrateurs hors API — donc sans audit, sans cloisonnement, sans acteur identifiable.
    """
    creer_admin(db, **_identite())

    with pytest.raises(ComptesDejaPresentsError):
        creer_admin(db, **_identite())


def test_force_autorise_le_depannage(db: Session, base_vierge: None) -> None:
    """Un réseau dont tous les administrateurs sont verrouillés doit pouvoir repartir."""
    creer_admin(db, **_identite())

    resultat = creer_admin(db, **_identite(), force=True)

    assert db.get(User, resultat.user_id) is not None


def test_sans_agence_le_compte_reste_creable(db: Session, base_vierge: None) -> None:
    """À l'installation, aucune agence n'existe encore. Rester sans agence est légitime :
    ADMIN_FONCTIONNEL détient perimetre.reseau, il n'est cloisonné par personne."""
    resultat = creer_admin(db, **_identite())

    admin = db.get(User, resultat.user_id)
    assert admin is not None
    assert admin.primary_agency_id is None


def test_une_agence_inconnue_est_refusee(db: Session, base_vierge: None) -> None:
    with pytest.raises(AgenceIntrouvableError):
        creer_admin(db, **_identite(), agence_code="AG-INEXISTANTE")


def test_une_agence_connue_est_rattachee(db: Session, base_vierge: None) -> None:
    agence = Agency(code=f"AG-{uuid.uuid4().hex[:6]}", name="Siège")
    db.add(agence)
    db.flush()

    resultat = creer_admin(db, **_identite(), agence_code=agence.code)

    admin = db.get(User, resultat.user_id)
    assert admin is not None
    assert admin.primary_agency_id == agence.id


def test_le_role_administrateur_existe_bien_dans_le_seed(db: Session) -> None:
    """Garde-fou de cohérence : ROLE_ADMIN doit correspondre à une ligne réelle, sinon la
    commande échouerait sur une base pourtant correctement seedée."""
    role = db.execute(select(Role).where(Role.code == ROLE_ADMIN)).scalar_one_or_none()

    assert role is not None
