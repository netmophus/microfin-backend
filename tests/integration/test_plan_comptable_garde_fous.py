"""Les garde-fous du plan de comptes, VUS MORDRE.

On ne se contente pas de vérifier qu'une opération légitime passe : chaque contrainte doit
REFUSER ce qu'elle est censée refuser (leçon « un mécanisme vert peut ne rien protéger »).

  - CHECK base : sens hors D/C rejeté ; classe ≠ 1er chiffre du numéro rejetée.
  - Service : compte système → sens verrouillé ; compte MOUVEMENTÉ → sens verrouillé et
    désactivation refusée ; compte à enfants actifs → désactivation refusée.
  - Import : parent manquant et numéro en double → refus EN BLOC, rien écrit.

« Mouvementé » n'a pas encore de table (journal_lines = C2) : on INJECTE la réponse pour
prouver que le garde-fou mord. Un test dédié atteste aussi qu'aujourd'hui, sans écritures,
la vérification réelle répond honnêtement « non mouvementé » (garde-fou câblé mais inerte).

Les numéros de test commencent par un chiffre (le CHECK classe_coherente lit ce 1er chiffre)
suivi d'une lettre : ils ne peuvent pas entrer en collision avec les 345 comptes du plan réel.
"""

import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import engine
from app.modules.comptabilite.models import Account
from app.modules.comptabilite.plan import ImportRefuseError, importer
from app.modules.comptabilite.service import (
    CompteAvecEnfantsActifsError,
    CompteMouvementeError,
    CompteSystemeError,
    compte_a_des_ecritures,
    desactiver,
    modifier_sens,
)

pytestmark = pytest.mark.integration


def _mouvemente(_id: uuid.UUID) -> bool:
    """Stub : « ce compte porte des écritures » (ce que journal_lines dira en C2)."""
    return True


def _vierge(_id: uuid.UUID) -> bool:
    """Stub : « aucune écriture »."""
    return False


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


def _compte(
    db: Session,
    numero: str,
    *,
    sens: str = "D",
    is_system: bool = False,
    is_posting: bool = True,
    parent_id: uuid.UUID | None = None,
) -> Account:
    compte = Account(
        account_number=numero,
        name=f"Compte {numero}",
        account_class=int(numero[0]),
        normal_side=sens,
        is_posting=is_posting,
        is_system=is_system,
        parent_id=parent_id,
    )
    db.add(compte)
    db.flush()
    return compte


# --- CHECK en base : la dernière barrière mord même hors service --------------------


def test_sens_hors_d_c_rejete_par_la_base(db: Session) -> None:
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text(
                "INSERT INTO comptabilite.accounts "
                "(account_number, name, account_class, normal_side, is_posting) "
                "VALUES ('6T990', 'Sens invalide', 6, 'X', TRUE)"
            )
        )
    # C'est bien le CHECK du sens qui mord, pas une autre contrainte.
    assert "normal_side" in str(exc.value)


def test_classe_incoherente_rejetee_par_la_base(db: Session) -> None:
    # Numéro commençant par 6, classe déclarée 5 : le CHECK classe_coherente mord.
    with pytest.raises(IntegrityError) as exc:
        db.execute(
            text(
                "INSERT INTO comptabilite.accounts "
                "(account_number, name, account_class, normal_side, is_posting) "
                "VALUES ('6T991', 'Classe incoherente', 5, 'D', TRUE)"
            )
        )
    assert "classe_coherente" in str(exc.value)


# --- Service : compte système verrouillé --------------------------------------------


def test_changer_le_sens_dun_compte_systeme_est_refuse(db: Session) -> None:
    compte = _compte(db, "6T900", sens="D", is_system=True)

    with pytest.raises(CompteSystemeError):
        modifier_sens(db, compte, "C", par=None, est_mouvemente=_vierge)


# --- Service : compte mouvementé verrouillé (le garde-fou VU MORDRE) -----------------


def test_changer_le_sens_dun_compte_mouvemente_est_refuse(db: Session) -> None:
    # Compte NON système (donc modifiable en principe) mais qui porte des écritures.
    compte = _compte(db, "6T901", sens="D", is_system=False)

    with pytest.raises(CompteMouvementeError):
        modifier_sens(db, compte, "C", par=None, est_mouvemente=_mouvemente)


def test_desactiver_un_compte_mouvemente_est_refuse(db: Session) -> None:
    compte = _compte(db, "6T902", is_system=False)

    with pytest.raises(CompteMouvementeError):
        desactiver(db, compte, par=None, est_mouvemente=_mouvemente)


# --- Service : hiérarchie cohérente -------------------------------------------------


def test_desactiver_un_compte_a_enfants_actifs_est_refuse(db: Session) -> None:
    parent = _compte(db, "6T910", sens="D", is_posting=False)
    _compte(db, "6T911", sens="D", parent_id=parent.id)  # enfant actif

    with pytest.raises(CompteAvecEnfantsActifsError):
        desactiver(db, parent, par=None, est_mouvemente=_vierge)


# --- Le chemin légitime, lui, passe -------------------------------------------------


def test_desactiver_un_compte_feuille_vierge_reussit(db: Session) -> None:
    compte = _compte(db, "6T903", is_system=False)

    desactiver(db, compte, par=None, est_mouvemente=_vierge)

    assert compte.is_active is False


def test_changer_le_sens_dun_compte_ordinaire_vierge_reussit(db: Session) -> None:
    compte = _compte(db, "6T904", sens="D", is_system=False)

    modifier_sens(db, compte, "C", par=None, est_mouvemente=_vierge)

    assert compte.normal_side == "C"


# --- Honnêteté : le garde-fou « mouvementé » est câblé mais inerte tant que C2 manque -


def test_sans_journal_lines_la_verification_dusage_repond_non(db: Session) -> None:
    # journal_lines n'existe pas avant C2 : la vérification réelle doit répondre « non
    # mouvementé » sans erreur. Le jour où C2 crée la table, ce test changera de sens.
    compte = _compte(db, "6T905", is_system=False)

    assert compte_a_des_ecritures(db, compte.id) is False


# --- Import : refus EN BLOC, rien écrit ---------------------------------------------

_ENTETE = (
    "account_number;name;short_name;class;parent_number;normal_side;is_posting;is_system;notes"
)


def _ecrire_csv(tmp_path: object, lignes: list[str]) -> str:
    chemin = f"{tmp_path}/plan.csv"
    with open(chemin, "w", encoding="utf-8", newline="") as f:
        f.write(_ENTETE + "\n")
        for li in lignes:
            f.write(li + "\n")
    return chemin


def _nombre_de_comptes(db: Session) -> int:
    return db.execute(text("SELECT count(*) FROM comptabilite.accounts")).scalar_one()


def test_import_avec_parent_manquant_refuse_tout(db: Session, tmp_path: object) -> None:
    chemin = _ecrire_csv(
        tmp_path,
        [
            "6T90;Racine test;;6;;C;FALSE;TRUE;",
            "6T9015;Orphelin;;6;6T99;C;TRUE;TRUE;",  # parent 6T99 absent du fichier
        ],
    )
    avant = _nombre_de_comptes(db)

    with pytest.raises(ImportRefuseError) as exc:
        importer(db, chemin)

    assert any("6T99" in str(a) and "introuvable" in str(a) for a in exc.value.anomalies)
    # RIEN écrit : le refus est total (le compte est inchangé).
    assert _nombre_de_comptes(db) == avant


def test_import_avec_numero_en_double_refuse_tout(db: Session, tmp_path: object) -> None:
    chemin = _ecrire_csv(
        tmp_path,
        [
            "6T90;Racine test;;6;;C;FALSE;TRUE;",
            "6T901;Sous-compte;;6;6T90;C;FALSE;TRUE;",
            "6T901;Doublon;;6;6T90;C;FALSE;TRUE;",  # 6T901 en double
        ],
    )
    avant = _nombre_de_comptes(db)

    with pytest.raises(ImportRefuseError) as exc:
        importer(db, chemin)

    assert any("double" in str(a) for a in exc.value.anomalies)
    assert _nombre_de_comptes(db) == avant
