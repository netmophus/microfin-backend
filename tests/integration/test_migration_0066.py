"""Migration 0066 : la journée comptable d'une session devient une valeur STOCKÉE.

Aller-retour complet (upgrade, backfill, downgrade, re-upgrade) sur une base JETABLE, créée puis
détruite par le test lui-même — jamais la base de test partagée de la session. On part de l'état
0065, on y pose des sessions « d'avant » avec des journées aux horodatages connus, puis on vérifie
que le backfill reproduit la dérivation historique UNE fois, que la colonne devient NOT NULL, et que
le trigger la rend immuable.
"""

import os
import subprocess
import sys
import uuid
from collections.abc import Generator
from pathlib import Path

import psycopg
import pytest
from sqlalchemy.engine import make_url

from app.core.config import settings

pytestmark = pytest.mark.integration

RACINE_BACKEND = Path(__file__).resolve().parents[2]
BASE_JETABLE = "mifin_test_mig0066"


def _dsn(base: str) -> str:
    url = make_url(settings.DATABASE_URL).set(database=base)
    return url.render_as_string(hide_password=False).replace("+psycopg", "")


@pytest.fixture
def base_jetable() -> Generator[str, None, None]:
    admin = psycopg.connect(_dsn("postgres"), autocommit=True)
    admin.execute(f'DROP DATABASE IF EXISTS "{BASE_JETABLE}" WITH (FORCE)')
    admin.execute(f'CREATE DATABASE "{BASE_JETABLE}"')
    try:
        yield BASE_JETABLE
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{BASE_JETABLE}" WITH (FORCE)')
        admin.close()


def _alembic(base: str, *args: str) -> None:
    env = {
        **os.environ,
        "DATABASE_URL": make_url(settings.DATABASE_URL)
        .set(database=base)
        .render_as_string(hide_password=False),
    }
    resultat = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=RACINE_BACKEND, env=env, capture_output=True, text=True, check=False,
    )
    assert resultat.returncode == 0, resultat.stdout + resultat.stderr


def _journees_des_sessions(connexion: psycopg.Connection) -> dict[str, str]:
    lignes = connexion.execute("SELECT id, journee_comptable FROM caisse.sessions").fetchall()
    return {str(identifiant): jour.isoformat() for identifiant, jour in lignes}


def _poser_etat_ancien(connexion: psycopg.Connection) -> dict[str, str]:
    """Des journées et des sessions « d'avant 0066 » ; rend {id de session: journée attendue}."""
    connexion.execute("SET session_replication_role = replica")  # données minimales, FK hors jeu
    for jour, statut, ouverture, cloture in [
        ("2024-03-04", "cloturee", "2024-03-04 07:00+00", "2024-03-04 18:00+00"),
        ("2024-03-05", "cloturee", "2024-03-05 07:00+00", "2024-03-05 18:00+00"),
        ("2024-03-06", "ouverte", "2024-03-06 07:00+00", None),
    ]:
        connexion.execute(
            "INSERT INTO comptabilite.journees_comptables "
            "(date_comptable, status, opened_at, closed_at) VALUES (%s, %s, %s, %s)",
            (jour, statut, ouverture, cloture),
        )

    def session(statut: str, ouverte: str, fermee: str | None) -> str:
        identifiant = str(uuid.uuid4())
        solde = 0 if fermee else None
        connexion.execute(
            "INSERT INTO caisse.sessions (id, agency_id, caissier_id, poste_id, compte_caisse_id, "
            "fonds_initial, opened_at, closed_at, status, montant_reel_cloture, "
            "solde_theorique_cloture, ecart) "
            "VALUES (%s, %s, %s, %s, %s, 0, %s, %s, %s, %s, %s, %s)",
            (identifiant, uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(),
             ouverte, fermee, statut, solde, solde, solde),
        )
        return identifiant

    return {
        session("fermee", "2024-03-04 08:00+00", "2024-03-04 16:00+00"): "2024-03-04",
        session("fermee", "2024-03-05 08:00+00", "2024-03-05 16:00+00"): "2024-03-05",
        # Encore ouverte : la journée ouverte.
        session("ouverte", "2024-03-06 08:00+00", None): "2024-03-06",
        # Antérieure à toute journée comptable : retombe sur la date d'ouverture.
        session("fermee", "2020-01-02 08:00+00", "2020-01-02 16:00+00"): "2020-01-02",
    }


def test_migration_0066_backfill_puis_aller_retour(base_jetable: str) -> None:
    _alembic(base_jetable, "upgrade", "0065")
    with psycopg.connect(_dsn(base_jetable)) as connexion:
        attendu = _poser_etat_ancien(connexion)

    _alembic(base_jetable, "upgrade", "0066")
    with psycopg.connect(_dsn(base_jetable)) as connexion:
        # Backfill : la dérivation historique, appliquée une seule fois.
        assert _journees_des_sessions(connexion) == attendu
        nullable = connexion.execute(
            "SELECT is_nullable FROM information_schema.columns WHERE table_schema = 'caisse' "
            "AND table_name = 'sessions' AND column_name = 'journee_comptable'"
        ).fetchone()
        assert nullable is not None and nullable[0] == "NO"
        # Dernier rempart : la journée stockée ne change plus.
        with pytest.raises(psycopg.errors.RestrictViolation, match="immuable"):
            connexion.execute("UPDATE caisse.sessions SET journee_comptable = '2020-01-01'")

    _alembic(base_jetable, "downgrade", "0065")
    with psycopg.connect(_dsn(base_jetable)) as connexion:
        colonne = connexion.execute(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'caisse' "
            "AND table_name = 'sessions' AND column_name = 'journee_comptable'"
        ).fetchone()
        trigger = connexion.execute(
            "SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_session_journee_immuable'"
        ).fetchone()
        assert colonne is not None and trigger is not None
        assert (colonne[0], trigger[0]) == (0, 0)

    _alembic(base_jetable, "upgrade", "0066")
    with psycopg.connect(_dsn(base_jetable)) as connexion:
        assert _journees_des_sessions(connexion) == attendu  # le backfill se rejoue à l'identique
