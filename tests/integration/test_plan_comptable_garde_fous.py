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

import csv
import uuid
from collections.abc import Generator
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import engine
from app.modules.comptabilite import ecritures
from app.modules.comptabilite.ecritures import LigneSaisie
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
def _mouvementer(db: Session, compte: Account, *, valider: bool = True) -> None:
    """Pose une VRAIE pièce équilibrée sur `compte` (contrepartie jetable) : le garde-fou doit
    mordre sur de vraies écritures, pas sur un stub. `valider=False` laisse un brouillon."""
    journal_id = db.execute(
        text("SELECT id FROM comptabilite.journals WHERE code = 'OD'")
    ).scalar_one()
    contrepartie = _compte(db, f"6T{uuid.uuid4().hex[:6]}")
    entry = ecritures.creer_brouillon(
        db,
        journal_id=journal_id,
        entry_date=date(2026, 6, 1),
        description="Mouvement de test (garde-fou import)",
        lignes=[
            LigneSaisie(account_id=compte.id, side="D", amount=1000),
            LigneSaisie(account_id=contrepartie.id, side="C", amount=1000),
        ],
        par=None,
    )
    if valider:
        ecritures.valider(db, entry, par=None)


# --- Import : garde-fou « sens d'un compte mouvementé » -----------------------------------


def _sens_en_base(db: Session, numero: str) -> str:
    return db.execute(
        text("SELECT normal_side FROM comptabilite.accounts WHERE account_number = :n"),
        {"n": numero},
    ).scalar_one()


def test_import_refuse_de_changer_le_sens_d_un_compte_avec_ecriture_validee(
    db: Session, tmp_path: object
) -> None:
    compte = _compte(db, "6T950", sens="D")
    _mouvementer(db, compte)
    chemin = _ecrire_csv(tmp_path, ["6T950;Compte 6T950;;6;;C;TRUE;FALSE;"])

    with pytest.raises(ImportRefuseError) as exc:
        importer(db, chemin)

    message = " ; ".join(str(a) for a in exc.value.anomalies)
    assert "6T950" in message  # le compte en conflit est nommé
    assert "débiteur à créditeur" in message
    assert "écritures" in message
    assert _sens_en_base(db, "6T950") == "D"  # rien n'a été écrasé


def test_import_change_le_sens_d_un_compte_sans_ecriture(db: Session, tmp_path: object) -> None:
    _compte(db, "6T951", sens="D")
    chemin = _ecrire_csv(tmp_path, ["6T951;Compte 6T951;;6;;C;TRUE;FALSE;"])

    rapport = importer(db, chemin)

    assert rapport.mis_a_jour == 1
    assert _sens_en_base(db, "6T951") == "C"


def test_import_meme_sens_sur_compte_mouvemente_reste_autorise(
    db: Session, tmp_path: object
) -> None:
    """Le garde-fou ne vise que le CHANGEMENT de sens : réimporter un compte mouvementé à
    l'identique (ou en changeant son libellé) doit continuer à passer — idempotence."""
    compte = _compte(db, "6T952", sens="D")
    _mouvementer(db, compte)
    chemin = _ecrire_csv(tmp_path, ["6T952;Libellé corrigé;;6;;D;TRUE;FALSE;"])

    rapport = importer(db, chemin)

    assert rapport.mis_a_jour == 1
    assert _sens_en_base(db, "6T952") == "D"


def test_import_un_seul_conflit_refuse_tout_l_import(db: Session, tmp_path: object) -> None:
    """Tout ou rien : le compte en conflit bloque AUSSI la création d'un compte neuf et le
    changement légitime d'un compte vierge présents dans le même fichier."""
    mouvemente = _compte(db, "6T953", sens="D")
    _mouvementer(db, mouvemente)
    _compte(db, "6T954", sens="D")  # vierge : changement normalement autorisé
    chemin = _ecrire_csv(
        tmp_path,
        [
            "6T953;Compte 6T953;;6;;C;TRUE;FALSE;",  # conflit
            "6T954;Compte 6T954;;6;;C;TRUE;FALSE;",  # légitime
            "6T955;Compte neuf;;6;;C;TRUE;FALSE;",  # création
        ],
    )

    with pytest.raises(ImportRefuseError):
        importer(db, chemin)

    assert _sens_en_base(db, "6T953") == "D"
    assert _sens_en_base(db, "6T954") == "D"  # pas appliqué : refus global
    assert (
        db.execute(
            text("SELECT count(*) FROM comptabilite.accounts WHERE account_number = '6T955'")
        ).scalar_one()
        == 0
    )


def test_import_un_brouillon_suffit_a_proteger_le_sens(db: Session, tmp_path: object) -> None:
    """Même définition de « mouvementé » que l'écran de changement de sens : toute ligne
    d'écriture, brouillon compris — une fois validé, il subirait le nouveau sens."""
    compte = _compte(db, "6T956", sens="D")
    _mouvementer(db, compte, valider=False)
    chemin = _ecrire_csv(tmp_path, ["6T956;Compte 6T956;;6;;C;TRUE;FALSE;"])

    with pytest.raises(ImportRefuseError):
        importer(db, chemin)

    assert _sens_en_base(db, "6T956") == "D"


# --- Sens des provisions : 4319 / 4329 (P2.0-a) --------------------------------------------

DOCS_REFERENCE = Path(__file__).resolve().parents[2] / "docs" / "reference"
CSV_IMPORT = DOCS_REFERENCE / "plan_comptable_import.csv"
CSV_ENRICHI = DOCS_REFERENCE / "plan_comptable_enrichi.csv"


def _sens_par_compte(chemin: Path) -> dict[str, str]:
    with open(chemin, encoding="utf-8-sig", newline="") as f:
        return {
            ligne["account_number"]: ligne["normal_side"]
            for ligne in csv.DictReader(f, delimiter=";")
        }


def test_les_deux_csv_du_plan_portent_le_meme_sens_pour_chaque_compte() -> None:
    """Import (création des comptes) et enrichi (mapping) doivent rester cohérents."""
    sens_import = _sens_par_compte(CSV_IMPORT)
    sens_enrichi = _sens_par_compte(CSV_ENRICHI)

    assert sens_import.keys() == sens_enrichi.keys()
    divergences = {n: (sens_import[n], sens_enrichi[n]) for n in sens_import
                   if sens_import[n] != sens_enrichi[n]}
    assert divergences == {}


def test_4319_et_4329_sont_crediteurs_dans_les_deux_csv() -> None:
    for chemin in (CSV_IMPORT, CSV_ENRICHI):
        sens = _sens_par_compte(chemin)
        assert sens["4319"] == "C", chemin.name
        assert sens["4329"] == "C", chemin.name


def test_toute_provision_ou_amortissement_d_actif_est_crediteur_dans_le_plan() -> None:
    """Garde-fou de données : en classes 1 à 4, un compte dont l'intitulé commence par
    « Provisions » ou « Amortissements » est un contra-actif, donc créditeur. C'est la règle
    qui aurait attrapé 4319/4329 (classes 6 et 7 exclues : les dotations y sont des charges)."""
    with open(CSV_IMPORT, encoding="utf-8-sig", newline="") as f:
        fautifs = [
            f"{ligne['account_number']} {ligne['name']}"
            for ligne in csv.DictReader(f, delimiter=";")
            if ligne["class"] in {"1", "2", "3", "4"}
            and ligne["name"].lower().startswith(("provisions", "amortissements"))
            and ligne["normal_side"] != "C"
        ]
    assert fautifs == []


def test_apres_import_du_plan_4319_et_4329_ressortent_crediteurs(db: Session) -> None:
    """Sur une base seedée AVANT la correction (4319/4329 en D, sans écriture), un réimport du
    CSV corrigé les passe en C — le sens est stocké en base, pas seulement dans le fichier."""
    db.execute(
        text(
            "UPDATE comptabilite.accounts SET normal_side = 'D' "
            "WHERE account_number IN ('4319', '4329')"
        )
    )

    importer(db, str(CSV_IMPORT))

    assert _sens_en_base(db, "4319") == "C"
    assert _sens_en_base(db, "4329") == "C"


# --- P2.0-b1 : comptes bruts et écart de réévaluation (extensions projet) -----------------------

CSV_OFFICIEL = DOCS_REFERENCE / "plan_comptable_rcsfd_officiel.csv"

# numéro -> (parent, sens normal)
EXTENSIONS_P20B1 = {
    "412100": ("412", "D"),  # participations dans SFD et établissements de crédit (brut)
    "412300": ("412", "D"),  # participations dans les autres entités (brut)
    "441100": ("441", "D"),  # immobilisations incorporelles d'exploitation (brut)
    "442100": ("442", "D"),  # immobilisations corporelles d'exploitation (brut)
    "552400": ("552", "C"),  # écart de réévaluation (capitaux propres)
}
BRUTS_P20B1 = [n for n in EXTENSIONS_P20B1 if n != "552400"]


def _comptes_du_plan(chemin: Path) -> dict[str, dict[str, str]]:
    with open(chemin, encoding="utf-8-sig", newline="") as f:
        return {ligne["account_number"]: ligne for ligne in csv.DictReader(f, delimiter=";")}


def test_le_csv_d_import_reste_valide_apres_ajout_des_extensions() -> None:
    from app.modules.comptabilite.plan import lire_csv, valider

    assert valider(lire_csv(str(CSV_IMPORT))) == []


def test_extensions_p20b1_sont_bien_formees_et_hors_nomenclature_officielle() -> None:
    plan_import = _comptes_du_plan(CSV_IMPORT)
    with open(CSV_OFFICIEL, encoding="utf-8-sig", newline="") as f:
        officiels = {ligne["compte"] for ligne in csv.DictReader(f, delimiter=";")}

    for numero, (parent, sens) in EXTENSIONS_P20B1.items():
        ligne = plan_import[numero]
        assert len(numero) == 6  # convention des extensions du projet
        assert numero not in officiels  # jamais présenté comme un compte officiel
        assert ligne["parent_number"] == parent
        assert ligne["normal_side"] == sens
        assert ligne["is_posting"] == "TRUE"
        assert ligne["is_system"] == "TRUE"
        assert "Extension projet" in ligne["notes"]


def test_bruts_sont_disjoints_de_leurs_provisions_et_amortissements() -> None:
    """Aucun autre compte du plan n'est un préfixe d'un brut, ni n'a un brut pour préfixe, à
    l'exception de ses propres ancêtres (regroupements) : un agrégat posé sur le préfixe d'un
    brut ne peut donc jamais ramasser une provision (piège 19/199)."""
    plan_import = _comptes_du_plan(CSV_IMPORT)

    def ancetres(numero: str) -> set[str]:
        resultat: set[str] = set()
        parent = plan_import[numero]["parent_number"]
        while parent:
            resultat.add(parent)
            parent = plan_import[parent]["parent_number"]
        return resultat

    for brut in BRUTS_P20B1:
        autorises = ancetres(brut)
        for autre in plan_import:
            if autre == brut:
                continue
            if brut.startswith(autre):
                assert autre in autorises, f"{autre} est un préfixe de {brut}"
            assert not autre.startswith(brut), f"{brut} est un préfixe de {autre}"


def test_creation_des_extensions_p20b1_passe_le_garde_fou_de_sens(db: Session) -> None:
    """Ce sont des CRÉATIONS : le garde-fou de sens (b0a8008) ne doit rien refuser."""
    from app.modules.comptabilite.plan import conflits_de_sens, lire_csv

    lignes = lire_csv(str(CSV_IMPORT))
    assert conflits_de_sens(db, lignes) == []

    importer(db, str(CSV_IMPORT))

    for numero, (parent, sens) in EXTENSIONS_P20B1.items():
        assert _sens_en_base(db, numero) == sens
        parent_en_base = db.execute(
            text(
                "SELECT p.account_number FROM comptabilite.accounts a "
                "JOIN comptabilite.accounts p ON p.id = a.parent_id "
                "WHERE a.account_number = :n"
            ),
            {"n": numero},
        ).scalar_one()
        assert parent_en_base == parent


def test_les_participations_ont_deux_buckets_bruts_sfd_ec_et_autres() -> None:
    """P2.0-b1-bis : les participations dans les SFD et dans les établissements de crédit sont
    traitées identiquement par les trois normes (déduites des fonds propres, exclues de #8/#9) :
    un seul bucket brut pour les deux, un autre pour toutes les autres entités. 412200
    (établissements de crédit seuls) n'existe plus."""
    plan_import = _comptes_du_plan(CSV_IMPORT)
    plan_enrichi = _comptes_du_plan(CSV_ENRICHI)

    brutes = sorted(
        n for n, ligne in plan_import.items() if ligne["parent_number"] == "412"
        and n.startswith(("4121", "4122", "4123"))
    )

    assert brutes == ["412100", "412300"]
    assert "412200" not in plan_import
    assert "412200" not in plan_enrichi
    assert plan_import["412100"]["name"] == (
        "Titres de participation dans SFD et etablissements de credit - valeur brute"
    )
    assert plan_enrichi["412100"]["name"] == plan_import["412100"]["name"]
    # 412300 : paire symétrique de 412100 (« hors SFD et établissements de crédit »), formulation
    # du texte du ratio #8 — il couvre aussi une entité financière qui n'est ni l'un ni l'autre.
    assert plan_import["412300"]["name"] == (
        "Titres de participation hors SFD et etablissements de credit - valeur brute"
    )
    assert plan_enrichi["412300"]["name"] == plan_import["412300"]["name"]


# --- P2.0-b1-ter : ventilation de la provision des participations (4129) ------------------------

SOUS_PROVISIONS = {
    "412910": "Provisions pour depreciation des titres de participation dans SFD et etablissements"
    " de credit",
    "412930": "Provisions pour depreciation des titres de participation hors SFD et "
    "etablissements de credit",
}


def test_les_sous_provisions_de_4129_sont_bien_formees_et_hors_plan_officiel() -> None:
    plan_import = _comptes_du_plan(CSV_IMPORT)
    plan_enrichi = _comptes_du_plan(CSV_ENRICHI)
    with open(CSV_OFFICIEL, encoding="utf-8-sig", newline="") as f:
        officiels = {ligne["compte"] for ligne in csv.DictReader(f, delimiter=";")}

    for numero, libelle in SOUS_PROVISIONS.items():
        ligne = plan_import[numero]
        assert len(numero) == 6
        assert numero not in officiels  # jamais présenté comme officiel
        assert ligne["name"] == libelle and libelle.isascii()
        assert ligne["parent_number"] == "4129"
        assert ligne["normal_side"] == "C"
        assert (ligne["is_posting"], ligne["is_system"]) == ("TRUE", "TRUE")
        assert "Extension projet" in ligne["notes"]
        assert plan_enrichi[numero]["name"] == libelle
        assert plan_enrichi[numero]["masse"] == "CONTRA_ACTIF"  # comme 4129
        assert plan_enrichi[numero]["etat"] == "BILAN"


def test_4129_est_devenu_regroupement_et_garde_sens_mapping_et_parent() -> None:
    for chemin in (CSV_IMPORT, CSV_ENRICHI):
        ligne = _comptes_du_plan(chemin)["4129"]
        assert ligne["is_posting"] == "FALSE", chemin.name
        assert ligne["normal_side"] == "C", chemin.name
    assert _comptes_du_plan(CSV_IMPORT)["4129"]["parent_number"] == "412"
    assert _comptes_du_plan(CSV_IMPORT)["4129"]["is_system"] == "TRUE"
    assert _comptes_du_plan(CSV_ENRICHI)["4129"]["masse"] == "CONTRA_ACTIF"


def test_provisions_et_bruts_des_participations_sont_disjoints_deux_a_deux() -> None:
    """Chiffré : parmi 412100, 412300, 412910, 412930, aucun n'est préfixe d'un autre. Et le
    préfixe 4129 (regroupement) capte exactement les deux provisions, aucun brut."""
    comptes = ["412100", "412300", "412910", "412930"]
    for a in comptes:
        for b in comptes:
            if a != b:
                assert not b.startswith(a), f"{a} est un préfixe de {b}"
    plan = _comptes_du_plan(CSV_IMPORT)
    assert sorted(n for n in plan if n.startswith("4129")) == ["4129", "412910", "412930"]
    assert [n for n in plan if n.startswith("4121")] == ["412100"]
    assert [n for n in plan if n.startswith("4123")] == ["412300"]


def _rouvrir_4129(db: Session) -> None:
    """Simule une base seedée AVANT la ventilation : 4129 compte de saisie."""
    db.execute(
        text("UPDATE comptabilite.accounts SET is_posting = TRUE WHERE account_number = '4129'")
    )


def _is_posting_4129(db: Session) -> bool:
    return db.execute(
        text("SELECT is_posting FROM comptabilite.accounts WHERE account_number = '4129'")
    ).scalar_one()


def test_import_bascule_4129_en_regroupement_quand_il_est_vierge(db: Session) -> None:
    importer(db, str(CSV_IMPORT))
    _rouvrir_4129(db)

    importer(db, str(CSV_IMPORT))

    assert _is_posting_4129(db) is False


def _poser_ecriture_sur_4129(db: Session, *, valider: bool) -> None:
    importer(db, str(CSV_IMPORT))
    _rouvrir_4129(db)
    compte = db.execute(select(Account).where(Account.account_number == "4129")).scalar_one()
    _mouvementer(db, compte, valider=valider)


def test_import_refuse_de_passer_en_regroupement_un_compte_mouvemente(db: Session) -> None:
    """Le garde-fou de nature MORD : 4129 avec une écriture validée -> refus, compte nommé."""
    _poser_ecriture_sur_4129(db, valider=True)

    with pytest.raises(ImportRefuseError) as exc:
        importer(db, str(CSV_IMPORT))

    message = " ; ".join(str(a) for a in exc.value.anomalies)
    assert "4129" in message and "regroupement" in message and "écritures" in message
    assert _is_posting_4129(db) is True  # rien n'a été écrasé


def test_import_un_brouillon_suffit_a_proteger_la_nature(db: Session) -> None:
    _poser_ecriture_sur_4129(db, valider=False)

    with pytest.raises(ImportRefuseError):
        importer(db, str(CSV_IMPORT))

    assert _is_posting_4129(db) is True


def test_refus_de_nature_est_global_et_visible_a_l_apercu(
    db: Session, tmp_path: object
) -> None:
    """Tout ou rien : le compte neuf du même fichier n'est pas créé ; et `conflits_de_nature`
    (utilisé par l'aperçu du back-office) renvoie le conflit sans rien écrire."""
    from app.modules.comptabilite.plan import conflits_de_nature, lire_csv

    _poser_ecriture_sur_4129(db, valider=True)
    chemin = _ecrire_csv(
        tmp_path,
        [
            "4;CLASSE 4;;4;;D;FALSE;TRUE;",
            "4129;Provisions;;4;4;C;FALSE;TRUE;",  # conflit : saisie -> regroupement
            "6T962;Compte neuf;;6;;C;TRUE;FALSE;",  # création
        ],
    )
    avant = _nombre_de_comptes(db)

    assert [a.account_number for a in conflits_de_nature(db, lire_csv(chemin))] == ["4129"]
    with pytest.raises(ImportRefuseError):
        importer(db, chemin)
    assert _nombre_de_comptes(db) == avant  # 6T962 n'a pas été créé


def test_nature_inchangee_ou_regroupement_vers_saisie_reste_autorise(
    db: Session, tmp_path: object
) -> None:
    """Le garde-fou ne vise que saisie -> regroupement sur un compte mouvementé : réimporter
    à l'identique passe (idempotence), et l'inverse n'est pas concerné."""
    compte = _compte(db, "6T960", sens="D")
    _mouvementer(db, compte)
    chemin = _ecrire_csv(tmp_path, ["6T960;Libellé corrigé;;6;;D;TRUE;FALSE;"])
    assert importer(db, chemin).mis_a_jour == 1

    _compte(db, "6T961", sens="D", is_posting=False)
    chemin = _ecrire_csv(tmp_path, ["6T961;Compte 6T961;;6;;D;TRUE;FALSE;"])
    assert importer(db, chemin).mis_a_jour == 1
