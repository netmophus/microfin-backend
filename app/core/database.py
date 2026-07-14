"""Engine SQLAlchemy synchrone, session factory, Base déclarative et dépendance FastAPI."""

from collections.abc import Generator

from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,  # recycle les connexions coupées par PostgreSQL/pgbouncer
    pool_size=5,
    max_overflow=10,
    echo=False,
)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)

# Nomme automatiquement index/contraintes : sans ça, PostgreSQL génère des noms
# implicites qu'Alembic ne sait pas cibler lors d'un downgrade ou d'un ALTER.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referenced_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base déclarative dont hériteront tous les modèles."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def get_db() -> Generator[Session, None, None]:
    """Dépendance FastAPI : ouvre une session par requête et la ferme systématiquement."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
