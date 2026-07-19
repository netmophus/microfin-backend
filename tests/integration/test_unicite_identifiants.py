"""Unicité des identifiants hors comptes supprimés (migration 0006).

Le test qui compte est celui du cycle créer → supprimer → recréer : c'est le scénario réel
(un employé qui part et revient, une adresse de service réattribuée) et il était IMPOSSIBLE
avant la 0006, la ligne supprimée occupant l'index pour toujours.

Ces tests portent sur la CONTRAINTE DE BASE, pas sur le service : ils écrivent en SQL/ORM
direct. C'est voulu — une garantie posée en base doit être vérifiée en base, sinon on teste
la politesse de l'appelant plutôt que la contrainte.
"""

import uuid
from collections.abc import Generator
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import engine
from app.modules.parameters.models import Agency
from app.modules.security.models import User

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
def agence(db: Session) -> Agency:
    agence = Agency(code=f"AG-{uuid.uuid4().hex[:6]}", name="Agence de test")
    db.add(agence)
    db.flush()
    return agence


def _user(agence: Agency, identite: str) -> User:
    """Trois identifiants dérivés d'une même racine, pour que les collisions soient nettes."""
    return User(
        matricule=f"MAT-{identite}",
        email=f"{identite}@example.com",
        username=f"u{identite}",
        password_hash="x" * 32,
        last_name="Kané",
        first_name="Fatou",
        primary_agency_id=agence.id,
    )


def test_deux_comptes_vivants_ne_peuvent_pas_partager_un_identifiant(
    db: Session, agence: Agency
) -> None:
    """L'unicité qui compte — celle qui empêche deux personnes de se connecter pareil."""
    identite = uuid.uuid4().hex[:8]
    db.add(_user(agence, identite))
    db.flush()

    db.add(_user(agence, identite))

    with pytest.raises(IntegrityError):
        db.flush()


def test_un_identifiant_est_libere_par_la_suppression(db: Session, agence: Agency) -> None:
    """LE cas qui était impossible avant la 0006.

    Un employé part (suppression logique), puis revient. Sans index partiel, sa ligne
    supprimée occupait l'index à jamais : il ne pouvait ni retrouver son matricule, ni
    réutiliser son adresse. Le défaut ne se voyait pas la première année.
    """
    identite = uuid.uuid4().hex[:8]
    parti = _user(agence, identite)
    db.add(parti)
    db.flush()

    parti.deleted_at = datetime.now(UTC)
    db.flush()

    revenu = _user(agence, identite)
    db.add(revenu)
    db.flush()  # ne doit pas lever

    assert revenu.id != parti.id
    assert parti.deleted_at is not None


def test_plusieurs_comptes_supprimes_peuvent_partager_un_identifiant(
    db: Session, agence: Agency
) -> None:
    """Corollaire : l'historique s'accumule sans jamais bloquer une réattribution.

    Une adresse de service (caisse@…) réattribuée trois fois en cinq ans laisse trois lignes
    supprimées identiques. Aucune ne doit gêner la quatrième attribution.
    """
    identite = uuid.uuid4().hex[:8]
    for _ in range(3):
        ancien = _user(agence, identite)
        db.add(ancien)
        db.flush()
        ancien.deleted_at = datetime.now(UTC)
        db.flush()

    db.add(_user(agence, identite))
    db.flush()  # ne doit pas lever


def test_restaurer_un_compte_dont_l_identifiant_a_ete_reattribue_echoue(
    db: Session, agence: Agency
) -> None:
    """Le revers de la liberté gagnée : une restauration peut désormais entrer en conflit.

    Scénario : un compte est supprimé, son identifiant réattribué à quelqu'un d'autre, puis
    on tente d'annuler la suppression initiale. Deux comptes VIVANTS porteraient alors le
    même identifiant : la base doit refuser.

    Ce refus est la bonne issue — mieux vaut un échec net qu'une restauration silencieuse
    qui casserait la connexion des deux personnes. Il faudra en tenir compte le jour où une
    fonction « restaurer un compte » sera écrite : elle devra vérifier la disponibilité des
    identifiants, pas se contenter d'effacer deleted_at.
    """
    identite = uuid.uuid4().hex[:8]
    ancien = _user(agence, identite)
    db.add(ancien)
    db.flush()
    ancien.deleted_at = datetime.now(UTC)
    db.flush()

    db.add(_user(agence, identite))  # identifiant réattribué
    db.flush()

    ancien.deleted_at = None  # tentative de restauration

    with pytest.raises(IntegrityError):
        db.flush()


def test_l_unicite_de_l_email_ignore_la_casse(db: Session, agence: Agency) -> None:
    """email est en CITEXT : l'index partiel en hérite.

    « A.Kane@imf.ne » et « a.kane@imf.ne » doivent rester le même compte — sinon deux
    employés croiraient détenir des adresses distinctes.
    """
    identite = uuid.uuid4().hex[:8]
    premier = _user(agence, identite)
    db.add(premier)
    db.flush()

    second = _user(agence, f"{identite}-bis")
    second.email = f"{identite.upper()}@EXAMPLE.COM"
    db.add(second)

    with pytest.raises(IntegrityError):
        db.flush()
