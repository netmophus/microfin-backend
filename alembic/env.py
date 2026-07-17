"""Environnement Alembic : URL et metadata proviennent de l'application, pas de l'ini."""

import re
from logging.config import fileConfig
from typing import Any

from sqlalchemy import create_engine, pool

from alembic import context
from app.core.config import settings
from app.core.database import Base

# Les modèles sont importés ici pour être enregistrés sur Base.metadata et donc
# visibles par --autogenerate. Tout module de modèles ajouté plus tard doit l'être
# ici aussi, sinon autogenerate croira ses tables absentes et proposera de les créer.
from app.modules.audit import models as audit_models
from app.modules.parameters import models as parameters_models
from app.modules.security import models as security_models

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Le socle vit dans des schémas non-défaut. Sans include_schemas, Alembic ne réfléchit
# que « public » : il ne verrait aucune de nos tables et proposerait de recréer chaque
# modèle. Avec, il réfléchit tout — d'où le filtre ci-dessous.
INCLUDE_SCHEMAS = True

# Objets réels que l'ORM ne mappe pas volontairement. Sans cette exclusion, autogenerate
# les voit en base, ne les trouve pas dans la metadata et propose de les SUPPRIMER.
PARTITION_AUDIT_LOGS = re.compile(r"^audit_logs_\d{4}_\d{2}$")
TABLES_NON_MAPPEES = frozenset({"audit_chain_head"})


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    """Écarte de l'autogenerate ce qui est géré par le SQL des migrations, pas par l'ORM.

    - Les partitions mensuelles de audit.audit_logs : créées par la 0001 puis par le job
      C13. Les mapper n'aurait aucun sens — on lit et écrit toujours la table parente.
    - audit.audit_chain_head : pointeur de tête du chaînage, alimenté par le seul trigger
      de la 0003. Aucun code applicatif ne la touche. Contrepartie assumée : une dérive
      de cette table ne serait pas détectée par « alembic check ».
    """
    if type_ == "table":
        if PARTITION_AUDIT_LOGS.match(name or ""):
            return False
        if name in TABLES_NON_MAPPEES:
            return False
    return True


def run_migrations_offline() -> None:
    """Génère le SQL sans se connecter (alembic upgrade head --sql)."""
    context.configure(
        url=settings.DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_schemas=INCLUDE_SCHEMAS,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Se connecte à la base et applique les migrations."""
    connectable = create_engine(settings.DATABASE_URL, poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_schemas=INCLUDE_SCHEMAS,
            include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
