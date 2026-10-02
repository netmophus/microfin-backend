"""Charge TOUS les modèles ORM avant les tests d'intégration, et provisionne la base de TEST
jetable (chantier P1ter — isolation vis-à-vis de la base de dev).

Sans l'import des modèles, un test qui n'importe que ceux d'un module (ex. épargne) laisse des
FK inter-schémas non résolues (epargne.accounts.tier_id -> tiers.tiers) : le mapping échoue
quand on lance ce test SEUL, alors qu'il passe dans la suite complète (un autre test ayant
importé tiers). Importer tout ici rend chaque test robuste à l'isolation — caisse et credit
ajoutés (manquaient : le gain pratique était nul tant que la suite entière était toujours
collectée avant le premier test, mais un run filtré sur un sous-ensemble sans caisse/credit
y serait resté exposé).
"""

import os
import re
from collections.abc import Generator
from datetime import date
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from alembic import command as alembic_command
from app.cli.creer_admin import creer_admin
from app.cli.seed_comptabilite import (
    executer_seed_journaux,
    executer_seed_schemas,
    ouvrir_exercice,
    rattacher_caisse_agences,
    seed_niveaux_caisse_dev,
    seed_parametres_caisse,
    seed_parametres_parts,
)
from app.cli.seed_epargne import executer_seed_produits
from app.cli.seed_financial_statement_mapping import executer_seed_mapping_etats
from app.cli.seed_security import executer_seed
from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.modules.audit import models as audit_models
from app.modules.caisse import models as caisse_models
from app.modules.comptabilite import models as comptabilite_models
from app.modules.comptabilite.plan import importer
from app.modules.credit import models as credit_models
from app.modules.epargne import models as epargne_models
from app.modules.parameters import models as parameters_models
from app.modules.parameters.models import Agency
from app.modules.security import models as security_models
from app.modules.tiers import models as tiers_models

# Référencés pour enregistrer les mappers ; le tuple évite un « import inutilisé ».
_MODELES = (
    audit_models,
    caisse_models,
    comptabilite_models,
    credit_models,
    epargne_models,
    parameters_models,
    security_models,
    tiers_models,
)

_RACINE_BACKEND = Path(__file__).resolve().parents[2]
_NOM_BASE_SUR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _executer_ddl_maintenance(instruction: str) -> None:
    """DROP/CREATE DATABASE : interdits dans une transaction, donc connexion AUTOCOMMIT sur
    la base de maintenance `postgres` du même cluster — jamais sur la base cible elle-même."""
    url_cible = make_url(settings.DATABASE_URL)
    url_maintenance = url_cible.set(database="postgres")
    moteur = create_engine(url_maintenance, isolation_level="AUTOCOMMIT")
    try:
        with moteur.connect() as connexion:
            connexion.execute(text(instruction))
    finally:
        moteur.dispose()


@pytest.fixture(scope="session", autouse=True)
def _base_de_test_jetable() -> Generator[None, None, None]:
    """Provisionne une base PostgreSQL jetable pour toute la session de test, et la détruit à
    la fin — DROP puis CREATE (jamais de réutilisation d'un résidu d'un run précédent), migrée
    (`alembic upgrade head`) puis amorcée avec le MINIMUM dont la suite a besoin pour démarrer
    (rôles/permissions, agence + utilisateur, plan de comptes RCSFD, rattachement caisse) —
    déterminé par grep sur les `LIMIT 1`/`_cid()` que les fixtures de test supposent déjà en
    place (parameters.agencies, security.users, comptabilite.accounts).

    Garde-fou redondant avec celui de `conftest.py` racine, volontairement : celui-ci vérifie
    l'URL EFFECTIVEMENT portée par l'engine applicatif déjà importé, juste avant la première
    opération destructive (DROP DATABASE) — défense en profondeur, pas une simple répétition.
    """
    nom_base = engine.url.database or ""
    if "test" not in nom_base.lower() or not _NOM_BASE_SUR.fullmatch(nom_base):
        pytest.exit(
            f"Garde-fou : l'engine applicatif pointe sur « {nom_base} », qui ne ressemble pas "
            "à une base de test jetable — arrêt avant toute opération destructive.",
            returncode=1,
        )

    _executer_ddl_maintenance(f'DROP DATABASE IF EXISTS "{nom_base}" WITH (FORCE)')
    _executer_ddl_maintenance(f'CREATE DATABASE "{nom_base}" OWNER mifin')

    config_alembic = Config(str(_RACINE_BACKEND / "alembic.ini"))
    alembic_command.upgrade(config_alembic, "head")

    with SessionLocal() as db:
        executer_seed(db)
        db.commit()

    with SessionLocal() as db:
        resultat_admin = creer_admin(
            db,
            username="admin",
            email="admin@imf.local",
            matricule="ADM-001",
            last_name="Administrateur",
            first_name="Compte",
            agence_nom="Siège",
        )
        agence_siege_id = db.execute(
            select(Agency.id).where(Agency.code == resultat_admin.agence_code)
        ).scalar_one()
        db.commit()

    # import-plan-comptable AVANT tout ce qui en dépend : le plan de comptes (101111, 1011,
    # 378, 3791, 3792, 591...) n'existe qu'à partir d'ici — les deux blocs suivants posent des
    # comptes-FEUILLES rattachés à des parents que SEUL cet import crée.
    with SessionLocal() as db:
        importer(db, str(_RACINE_BACKEND / "docs" / "reference" / "plan_comptable_import.csv"))
        db.commit()

    with SessionLocal() as db:
        # PROVISOIRE P1ter lot 2 : à RETIRER dès que ces 6 comptes seront créés par un chemin
        # officiel côté prod (CSV de référence complété, ou migration dédiée) — voir le rapport
        # du chantier. Comptes de démo « coffre »/« principale » (101115/101114, DEV UNIQUEMENT,
        # voir seed_comptabilite.py) — nécessaires à test_caisse_transferts.py et
        # test_comptabilite_journee_datation.py, qui les supposent « déjà en base ».
        seed_niveaux_caisse_dev(db, agence_siege_id)
        # 101112 existe sur la base de DEV PARTAGÉE par une édition manuelle jamais tracée dans
        # aucun seed/migration (absent de la CSV et de seed_comptabilite.py) ; utilisé par
        # test_caisse_transferts.py comme compte « secondaire » générique. Reproduit ICI, dans
        # la seule couche de test, faute d'un mécanisme applicatif existant qui le crée — hors
        # périmètre de ce chantier de modifier seed_comptabilite.py pour ça (voir le rapport).
        db.execute(
            text(
                "INSERT INTO comptabilite.accounts "
                "(account_number, name, account_class, parent_id, normal_side, is_posting, "
                " is_system, is_provisional) "
                "SELECT '101112', 'Caisse secondaire (test)', 1, id, 'D', TRUE, FALSE, TRUE "
                "FROM comptabilite.accounts WHERE account_number = '1011' "
                "ON CONFLICT (account_number) DO NOTHING"
            )
        )
        # PROVISOIRE P1ter lot 2 : à RETIRER dès que docs/reference/plan_comptable_import.csv
        # (ou plan_comptable_enrichi.csv) sera corrigé côté prod pour inclure ces 3 comptes.
        # GAP DÉCOUVERT PAR CE CHANTIER (hors périmètre de corriger ici, signalé au rapport) :
        # docs/reference/plan_comptable_enrichi.csv (lu par seed-mapping-etats) suppose 3
        # comptes-feuilles — 378100, 379110, 379210 (transit caisses / écarts de transfert) —
        # que docs/reference/plan_comptable_import.csv (le SEUL chemin d'import officiel) ne
        # crée PAS (il ne pose que leurs parents 378/3791/3792). Sur la base de dev partagée,
        # ces 3 comptes ont été ajoutés à la main un jour, hors de tout seed traçable — un
        # import-plan-comptable + seed-mapping-etats enchaînés sur une base VRAIMENT neuve
        # (jamais exercé avant ce chantier) tombe donc sur l'écart. Reproduit ici, dans la
        # seule couche de test, en attendant une correction du côté des CSV de référence.
        for numero, nom, parent, cote in (
            ("378100", "Virements internes de fonds - transit caisses", "378", "D"),
            ("379110", "Ecart de transfert - manquant", "3791", "D"),
            ("379210", "Ecart de transfert - excedent", "3792", "C"),
        ):
            db.execute(
                text(
                    "INSERT INTO comptabilite.accounts "
                    "(account_number, name, account_class, parent_id, normal_side, is_posting, "
                    " is_system, is_provisional) "
                    "SELECT :numero, :nom, 3, id, :cote, TRUE, FALSE, TRUE "
                    "FROM comptabilite.accounts WHERE account_number = :parent "
                    "ON CONFLICT (account_number) DO NOTHING"
                ),
                {"numero": numero, "nom": nom, "parent": parent, "cote": cote},
            )
        db.commit()

    with SessionLocal() as db:
        # Mapping bilan/compte de résultat (591 -> BILAN/PASSIF, etc.) — sans lui, les comptes
        # SYSTÈME réels (591 notamment) sont absents du bilan/compte de résultat calculés, ce
        # que test_comptabilite_etats_financiers_api.py::
        # test_bilan_desequilibre_avant_cloture_equilibre_apres suppose déjà en place (ses
        # propres comptes de test sont mappés explicitement, mais 591 est un compte réel).
        executer_seed_mapping_etats(db)
        db.commit()

    with SessionLocal() as db:
        executer_seed_journaux(db)
        executer_seed_schemas(db)
        rattacher_caisse_agences(db)
        seed_parametres_parts(db)
        seed_parametres_caisse(db)
        # PROVISOIRE P1ter lot 2 : à RETIRER si une future migration prod prend en charge le
        # backfill pour les agences créées APRÈS coup (pas seulement celles déjà là en 0041).
        # Backfill du premier poste de caisse ("01", code/libellé fixes) pour toute agence
        # rattachée à un compte de caisse — exactement ce que fait la migration 0041
        # (alembic/versions/0041_caisse_postes.py) pour les agences qui existaient DÉJÀ au
        # moment où elle a tourné. Notre Siège est créé APRÈS les migrations (par
        # `creer_admin`, ci-dessus) : sans ce backfill répété ici, aucun poste n'existe pour
        # lui et plusieurs tests (ex. test_credit_engagements.py) qui en supposent un
        # "backfillé par la migration 0041" échoueraient.
        db.execute(
            text(
                "INSERT INTO caisse.postes (agency_id, code, libelle, compte_caisse_id) "
                "SELECT a.id, '01', 'Caisse principale', a.compte_caisse_id "
                "FROM parameters.agencies a "
                "WHERE a.compte_caisse_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM caisse.postes p WHERE p.agency_id = a.id)"
            )
        )
        db.commit()

    with SessionLocal() as db:
        # EAV (épargne à vue) : test_epargne_api.py::test_lister_les_produits en suppose
        # l'existence — produit de démo standard, PROVISOIRE comme en dev/installation réelle.
        executer_seed_produits(db)
        db.commit()

    # PROVISOIRE P1ter lot 2 : à RETIRER/généraliser si la suite cesse un jour de dépendre de
    # dates 2026 écrites en dur. Exercice ambiant, calé sur l'ANNÉE CIVILE LITTÉRALE 2026 — pas
    # `date.today().year` : la
    # base de dev réelle n'a qu'un seul exercice ("2026", 2026-01-01 -> 2026-12-31) et une bonne
    # partie de la suite poste des écritures sur des dates 2026 écrites en dur dans le code des
    # tests (ex. 2026-06-15), pas sur « aujourd'hui ». Les tests qui ont besoin d'un exercice sur
    # une autre période (2031-2033, pour rester loin de toute donnée réelle sur l'UNIQUE
    # `date_comptable`) ouvrent le leur explicitement dans leur propre setup.
    with SessionLocal() as db:
        ouvrir_exercice(
            db,
            code="2026",
            label="Exercice 2026",
            date_debut=date(2026, 1, 1),
            date_fin=date(2026, 12, 31),
        )
        db.commit()

    yield

    engine.dispose()
    if not os.environ.get("KEEP_TEST_DB"):
        _executer_ddl_maintenance(f'DROP DATABASE IF EXISTS "{nom_base}" WITH (FORCE)')
