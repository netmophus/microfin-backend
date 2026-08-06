"""API Plan de comptes (Bloc 1) — consultation + gestion UNITAIRE.

  - consultation : recherche numéro/libellé, filtre classe, exclusion des inactifs par défaut ;
  - création : is_provisional=FALSE (décision déjà validée), is_system toujours FALSE, mêmes
    règles de cohérence que l'import CSV (classe/numéro, parent) ;
  - modification PARTIELLE du libellé (seuls les champs fournis bougent) ;
  - changer le sens / désactiver : MOTIF obligatoire, et les garde-fous (compte système,
    MOUVEMENTÉ — via un VRAI mouvement, pas un stub — enfants actifs) VUS MORDRE via l'API ;
  - permissions : compta.plan.read / compta.plan.manage, 403 sinon ;
  - traçabilité : chaque écriture pose une ligne d'audit avant/après.
"""

import uuid
from collections.abc import Generator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.comptabilite import ecritures, plan
from app.modules.comptabilite.ecritures import LigneSaisie
from app.modules.comptabilite.models import Account
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


def _agence_id(db: Session) -> uuid.UUID:
    return db.execute(text("SELECT id FROM parameters.agencies LIMIT 1")).scalar_one()


def _entete(db: Session, role_code: str) -> dict[str, str]:
    role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
    agence_id = _agence_id(db)
    s = uuid.uuid4().hex[:8]
    user = User(
        matricule=f"MAT-{s}", email=f"{s}@ex.com", username=f"u{s}",
        password_hash=hasher_mot_de_passe("Motdepasse!123"), last_name="T", first_name="A",
        primary_agency_id=agence_id,
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.flush()
    jeton = creer_access_token(
        user_id=user.id, roles=[role_code], primary_agency_id=agence_id, agency_id=agence_id
    )
    return {"Authorization": f"Bearer {jeton}"}


def _compte(db: Session, numero: str, **overrides: object) -> Account:
    valeurs = {
        "account_number": numero,
        "name": f"Compte {numero}",
        "account_class": int(numero[0]),
        "normal_side": "D",
        "is_posting": True,
        "is_system": False,
        **overrides,
    }
    compte = Account(**valeurs)
    db.add(compte)
    db.flush()
    return compte


def _rendre_mouvemente(db: Session, compte: Account) -> None:
    """Pose une VRAIE pièce équilibrée sur le compte — le garde-fou doit mordre pour de vrai,
    pas sur un stub (leçon « un mécanisme vert peut ne rien protéger »). Contrepartie : un
    compte de saisie JETABLE créé ici, jamais un compte système réel — celui-ci peut légitimement
    être verrouillé (verrouiller_saisie) sans faire échouer ce garde-fou."""
    journal_id = db.execute(
        text("SELECT id FROM comptabilite.journals WHERE code = 'OD'")
    ).scalar_one()
    contrepartie = _compte(db, f"6T{uuid.uuid4().hex[:6]}")
    entry = ecritures.creer_brouillon(
        db,
        journal_id=journal_id,
        entry_date=date(2026, 6, 1),
        description="Mouvement de test (garde-fou)",
        lignes=[
            LigneSaisie(account_id=compte.id, side="D", amount=1000),
            LigneSaisie(account_id=contrepartie.id, side="C", amount=1000),
        ],
        par=None,
    )
    ecritures.valider(db, entry, par=None)


# --- Consultation --------------------------------------------------------------------


def test_recherche_par_numero_et_libelle(client: TestClient, db: Session) -> None:
    _compte(db, "6T100", name="Charges diverses de test")
    comptable = _entete(db, "COMPTABLE")

    par_numero = client.get("/comptabilite/comptes", params={"q": "6T100"}, headers=comptable)
    assert par_numero.status_code == 200
    assert any(c["account_number"] == "6T100" for c in par_numero.json()["lignes"])

    par_libelle = client.get(
        "/comptabilite/comptes", params={"q": "Charges diverses de test"}, headers=comptable
    )
    assert any(c["account_number"] == "6T100" for c in par_libelle.json()["lignes"])


def test_filtre_par_classe(client: TestClient, db: Session) -> None:
    _compte(db, "6T101")
    comptable = _entete(db, "COMPTABLE")

    reponse = client.get(
        "/comptabilite/comptes", params={"classe": 6, "q": "6T101"}, headers=comptable
    )
    assert any(c["account_number"] == "6T101" for c in reponse.json()["lignes"])
    autre_classe = client.get(
        "/comptabilite/comptes", params={"classe": 5, "q": "6T101"}, headers=comptable
    )
    assert autre_classe.json()["lignes"] == []


def test_inactifs_exclus_par_defaut_puis_inclus(client: TestClient, db: Session) -> None:
    _compte(db, "6T102", is_active=False)
    comptable = _entete(db, "COMPTABLE")

    defaut = client.get("/comptabilite/comptes", params={"q": "6T102"}, headers=comptable)
    assert defaut.json()["lignes"] == []

    avec_inactifs = client.get(
        "/comptabilite/comptes", params={"q": "6T102", "inclure_inactifs": True}, headers=comptable
    )
    assert any(c["account_number"] == "6T102" for c in avec_inactifs.json()["lignes"])


def test_lecture_sans_permission_403(client: TestClient, db: Session) -> None:
    caissier = _entete(db, "CAISSIER")  # ne détient pas compta.plan.read
    reponse = client.get("/comptabilite/comptes", headers=caissier)
    assert reponse.status_code == 403


def test_compte_introuvable_404(client: TestClient, db: Session) -> None:
    comptable = _entete(db, "COMPTABLE")
    reponse = client.get(f"/comptabilite/comptes/{uuid.uuid4()}", headers=comptable)
    assert reponse.status_code == 404


def test_parent_resolu_par_numero_dans_la_fiche(client: TestClient, db: Session) -> None:
    parent = _compte(db, "6T110", is_posting=False)
    enfant = _compte(db, "6T111", parent_id=parent.id)
    comptable = _entete(db, "COMPTABLE")

    reponse = client.get(f"/comptabilite/comptes/{enfant.id}", headers=comptable)
    assert reponse.json()["parent_number"] == "6T110"  # le NUMÉRO, pas un UUID


# --- Création --------------------------------------------------------------------------


def test_creation_reussit_et_nest_pas_provisoire(client: TestClient, db: Session) -> None:
    comptable = _entete(db, "COMPTABLE")
    corps = {
        "account_number": "6T200",
        "name": "Nouveau compte de charge",
        "account_class": 6,
        "normal_side": "D",
        "is_posting": True,
    }
    reponse = client.post("/comptabilite/comptes", json=corps, headers=comptable)
    assert reponse.status_code == 201
    donnees = reponse.json()
    # Décision déjà validée par le comptable -> PAS provisoire. Jamais système à la création.
    assert donnees["is_provisional"] is False
    assert donnees["is_system"] is False


def test_creation_classe_incoherente_refusee(client: TestClient, db: Session) -> None:
    comptable = _entete(db, "COMPTABLE")
    corps = {
        "account_number": "6T201", "name": "Test", "account_class": 5,
        "normal_side": "D", "is_posting": True,
    }
    reponse = client.post("/comptabilite/comptes", json=corps, headers=comptable)
    assert reponse.status_code == 422
    assert "classe" in reponse.json()["detail"].lower()


def test_creation_parent_introuvable_refusee(client: TestClient, db: Session) -> None:
    comptable = _entete(db, "COMPTABLE")
    corps = {
        "account_number": "6T202", "name": "Test", "account_class": 6,
        "parent_number": "6T999", "normal_side": "D", "is_posting": True,
    }
    reponse = client.post("/comptabilite/comptes", json=corps, headers=comptable)
    assert reponse.status_code == 422
    assert "6T999" in reponse.json()["detail"]


def test_creation_parent_pas_prefixe_refusee(client: TestClient, db: Session) -> None:
    _compte(db, "6T210")
    comptable = _entete(db, "COMPTABLE")
    corps = {
        "account_number": "6T220", "name": "Test", "account_class": 6,
        "parent_number": "6T210", "normal_side": "D", "is_posting": True,
    }
    reponse = client.post("/comptabilite/comptes", json=corps, headers=comptable)
    assert reponse.status_code == 422
    assert "préfixe" in reponse.json()["detail"]


def test_creation_numero_deja_utilise_refusee(client: TestClient, db: Session) -> None:
    _compte(db, "6T230")
    comptable = _entete(db, "COMPTABLE")
    corps = {
        "account_number": "6T230", "name": "Doublon", "account_class": 6,
        "normal_side": "D", "is_posting": True,
    }
    reponse = client.post("/comptabilite/comptes", json=corps, headers=comptable)
    assert reponse.status_code == 422
    assert "existe déjà" in reponse.json()["detail"]


def test_creation_sans_permission_403(client: TestClient, db: Session) -> None:
    caissier = _entete(db, "CAISSIER")
    corps = {
        "account_number": "6T240", "name": "Test", "account_class": 6,
        "normal_side": "D", "is_posting": True,
    }
    reponse = client.post("/comptabilite/comptes", json=corps, headers=caissier)
    assert reponse.status_code == 403


# --- Modification (libellé, partielle) --------------------------------------------------


def test_modification_partielle_seuls_les_champs_fournis_bougent(
    client: TestClient, db: Session
) -> None:
    compte = _compte(db, "6T300", name="Ancien libellé", short_name="Ancien")
    comptable = _entete(db, "COMPTABLE")

    reponse = client.patch(
        f"/comptabilite/comptes/{compte.id}", json={"name": "Nouveau libellé"}, headers=comptable
    )
    assert reponse.status_code == 200
    donnees = reponse.json()
    assert donnees["name"] == "Nouveau libellé"
    assert donnees["short_name"] == "Ancien"  # non fourni -> inchangé


def test_modification_libelle_vide_refusee(client: TestClient, db: Session) -> None:
    compte = _compte(db, "6T301")
    comptable = _entete(db, "COMPTABLE")

    reponse = client.patch(
        f"/comptabilite/comptes/{compte.id}", json={"name": ""}, headers=comptable
    )
    assert reponse.status_code == 422


# --- Changer le sens : motif obligatoire, garde-fous VUS MORDRE --------------------------


def test_changer_sens_reussit_avec_motif_trace(client: TestClient, db: Session) -> None:
    compte = _compte(db, "6T400")  # normal_side="D" par défaut
    comptable = _entete(db, "COMPTABLE")

    reponse = client.post(
        f"/comptabilite/comptes/{compte.id}/sens",
        json={"normal_side": "C", "motif": "Correction du sens à la mise en service"},
        headers=comptable,
    )
    assert reponse.status_code == 200
    assert reponse.json()["normal_side"] == "C"

    # Tracé : une ligne d'audit porte l'ancien ET le nouveau sens, plus le motif.
    ligne = db.execute(
        text(
            "SELECT old_values, new_values FROM audit.audit_logs "
            "WHERE action = 'compta.plan.sens_change' AND resource_id = :r"
        ),
        {"r": compte.id},
    ).one()
    assert ligne.old_values["normal_side"] == "D"
    assert ligne.new_values["normal_side"] == "C"
    assert "motif" in ligne.new_values


def test_changer_sens_sans_motif_refuse(client: TestClient, db: Session) -> None:
    compte = _compte(db, "6T401")
    comptable = _entete(db, "COMPTABLE")

    reponse = client.post(
        f"/comptabilite/comptes/{compte.id}/sens",
        json={"normal_side": "C", "motif": ""},
        headers=comptable,
    )
    assert reponse.status_code == 422


def test_changer_sens_compte_systeme_refuse(client: TestClient, db: Session) -> None:
    compte = _compte(db, "6T402", is_system=True)
    comptable = _entete(db, "COMPTABLE")

    reponse = client.post(
        f"/comptabilite/comptes/{compte.id}/sens",
        json={"normal_side": "C", "motif": "Tentative"},
        headers=comptable,
    )
    assert reponse.status_code == 422
    assert "système" in reponse.json()["detail"].lower()


def test_changer_sens_compte_mouvemente_refuse_via_vrai_mouvement(
    client: TestClient, db: Session
) -> None:
    compte = _compte(db, "6T403")
    _rendre_mouvemente(db, compte)
    comptable = _entete(db, "COMPTABLE")

    reponse = client.post(
        f"/comptabilite/comptes/{compte.id}/sens",
        json={"normal_side": "C", "motif": "Tentative sur compte mouvementé"},
        headers=comptable,
    )
    assert reponse.status_code == 422
    assert "mouvement" in reponse.json()["detail"].lower()


# --- Verrouiller la saisie : DÉLIBÉRÉMENT PAS bloqué par système ni mouvementé -------------
#
# Contrairement à changer_sens/désactiver : fermer la saisie ne déforme aucune écriture déjà
# passée, donc système et mouvementé ne sont PAS des garde-fous ici (c'est même le cas d'usage
# — fermer un compte officiel qu'une extension à 6 chiffres a remplacé).


def test_verrouiller_saisie_reussit_meme_systeme_et_mouvemente(
    client: TestClient, db: Session
) -> None:
    compte = _compte(db, "6T600", is_system=True)
    _rendre_mouvemente(db, compte)
    comptable = _entete(db, "COMPTABLE")

    reponse = client.post(
        f"/comptabilite/comptes/{compte.id}/verrouiller-saisie",
        json={"motif": "Remplacé par une extension à 6 chiffres"},
        headers=comptable,
    )
    assert reponse.status_code == 200
    assert reponse.json()["is_posting"] is False


def test_verrouiller_saisie_sans_motif_refuse(client: TestClient, db: Session) -> None:
    compte = _compte(db, "6T601")
    comptable = _entete(db, "COMPTABLE")

    reponse = client.post(
        f"/comptabilite/comptes/{compte.id}/verrouiller-saisie", json={}, headers=comptable
    )
    assert reponse.status_code == 422


def test_verrouiller_saisie_deja_regroupement_refuse(client: TestClient, db: Session) -> None:
    compte = _compte(db, "6T602", is_posting=False)
    comptable = _entete(db, "COMPTABLE")

    reponse = client.post(
        f"/comptabilite/comptes/{compte.id}/verrouiller-saisie",
        json={"motif": "Tentative"},
        headers=comptable,
    )
    assert reponse.status_code == 422
    assert "regroupement" in reponse.json()["detail"].lower()


def test_verrouiller_saisie_sans_permission_403(client: TestClient, db: Session) -> None:
    compte = _compte(db, "6T603")
    caissier = _entete(db, "CAISSIER")

    reponse = client.post(
        f"/comptabilite/comptes/{compte.id}/verrouiller-saisie",
        json={"motif": "Tentative"},
        headers=caissier,
    )
    assert reponse.status_code == 403


def test_verrouiller_saisie_trace_en_audit(client: TestClient, db: Session) -> None:
    compte = _compte(db, "6T604")
    comptable = _entete(db, "COMPTABLE")

    client.post(
        f"/comptabilite/comptes/{compte.id}/verrouiller-saisie",
        json={"motif": "Remplacé par 6T604X"},
        headers=comptable,
    )

    ligne = db.execute(
        text(
            "SELECT action, new_values FROM audit.audit_logs "
            "WHERE resource_id = :id AND action = 'compta.plan.saisie_verrouillee'"
        ),
        {"id": str(compte.id)},
    ).mappings().one()
    assert ligne["new_values"]["motif"] == "Remplacé par 6T604X"
    assert ligne["new_values"]["is_posting"] is False


# --- Désactiver : motif obligatoire, garde-fous VUS MORDRE --------------------------------


def test_desactiver_reussit_avec_motif(client: TestClient, db: Session) -> None:
    compte = _compte(db, "6T500")
    comptable = _entete(db, "COMPTABLE")

    reponse = client.post(
        f"/comptabilite/comptes/{compte.id}/desactiver",
        json={"motif": "Compte devenu inutile"},
        headers=comptable,
    )
    assert reponse.status_code == 200
    assert reponse.json()["is_active"] is False


def test_desactiver_compte_mouvemente_refuse_via_vrai_mouvement(
    client: TestClient, db: Session
) -> None:
    compte = _compte(db, "6T501")
    _rendre_mouvemente(db, compte)
    comptable = _entete(db, "COMPTABLE")

    reponse = client.post(
        f"/comptabilite/comptes/{compte.id}/desactiver",
        json={"motif": "Tentative"},
        headers=comptable,
    )
    assert reponse.status_code == 422


def test_desactiver_compte_a_enfants_actifs_refuse(client: TestClient, db: Session) -> None:
    parent = _compte(db, "6T510", is_posting=False)
    _compte(db, "6T511", parent_id=parent.id)
    comptable = _entete(db, "COMPTABLE")

    reponse = client.post(
        f"/comptabilite/comptes/{parent.id}/desactiver",
        json={"motif": "Tentative"},
        headers=comptable,
    )
    assert reponse.status_code == 422
    assert "enfant" in reponse.json()["detail"].lower()


def test_desactiver_sans_permission_403(client: TestClient, db: Session) -> None:
    compte = _compte(db, "6T520")
    caissier = _entete(db, "CAISSIER")

    reponse = client.post(
        f"/comptabilite/comptes/{compte.id}/desactiver",
        json={"motif": "Tentative"},
        headers=caissier,
    )
    assert reponse.status_code == 403


# --- Export CSV — neutralisation d'injection de formule Excel (CLAUDE.md §12) --------

def test_export_neutralise_un_libelle_qui_ressemble_a_une_formule(db: Session) -> None:
    # Compte posé DIRECTEMENT en base (migration, création API) — pas via l'import CSV : la
    # neutralisation à l'export doit mordre même sans être passée par lire_lignes.
    _compte(
        db,
        "6T530",
        name="=CMD|'/C calc'!A1",
        notes="@SUM(1+9)*cmd|' /C calc'!A0",
    )

    contenu = plan.exporter_csv(db)

    assert "'=CMD|'/C calc'!A1" in contenu
    assert "'@SUM(1+9)*cmd|' /C calc'!A0" in contenu
    # La formule brute (sans apostrophe protectrice) ne doit apparaître nulle part dans l'export.
    assert "\n=CMD" not in contenu and ";=CMD" not in contenu


def test_aller_retour_export_puis_reimport_reste_stable(db: Session) -> None:
    _compte(db, "6T531", name="+1+1", short_name="-danger")

    exporte = plan.exporter_csv(db)
    lignes = plan.lire_bytes(exporte.encode("utf-8"))
    reexporte = plan.exporter_csv(db)

    ligne = next(li for li in lignes if li.account_number == "6T531")
    # Une seule apostrophe ajoutée, jamais deux : réimporter un export neutralisé ne re-préfixe pas.
    assert ligne.name == "'+1+1"
    assert ligne.short_name == "'-danger"
    # Un second export produit le même contenu que le premier — le cycle est stable.
    assert exporte == reexporte
