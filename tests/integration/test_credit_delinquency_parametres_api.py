"""API des paliers de souffrance (CR5a, Bloc 5 du paramétrage comptable).

  - lecture triée sur seuil_jours (l'ordre EST le seuil, pas une colonne à part) ;
  - écriture (créer/modifier/retirer) : motif obligatoire, tracé avant/après ;
  - le nombre de paliers est une donnée : créer et retirer sont de VRAIES opérations, pas
    seulement modifier une ligne existante (contrairement à share_parameters, singleton) ;
  - GARDE-FOU DOUBLE sur les 2 sélecteurs (encours, dotation) : compte_saisie_actif refuse un
    compte de regroupement/désactivé même soumis directement à l'API ;
  - unicité de `code` et de `seuil_jours`, message nommant le palier en conflit ;
  - gardé sur compta.plan.read / compta.plan.manage (même paire que les autres écrans Bloc 5).
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.cli.seed_credit import executer_seed_paliers_souffrance
from app.core.database import engine, get_db
from app.main import app
from app.modules.comptabilite.models import Account
from app.modules.credit.models import DelinquencyTier
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


def _entete_auth(db: Session, role_code: str) -> dict[str, str]:
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


def _palier(db: Session, code: str, seuil_jours: int, **overrides: object) -> DelinquencyTier:
    valeurs = {
        "code": code,
        "libelle": f"Palier {code}",
        "seuil_jours": seuil_jours,
        "taux_provision_bp": 0,
        **overrides,
    }
    palier = DelinquencyTier(**valeurs)
    db.add(palier)
    db.flush()
    return palier


# --- Seed ------------------------------------------------------------------------------


def test_seed_installe_les_4_paliers_provisoires(db: Session) -> None:
    """Chantier supervision de la souffrance, lot 1 : barème 90/180/360 jours, RETARD (ex-IMPAYE)
    NON provisionné et SANS compte, les 3 autres rattachés aux comptes RCSFD par le seed
    lui-même SUR PREMIÈRE CRÉATION (voir _UPSERT_PALIER, app/cli/seed_credit.py). Table vidée
    d'abord (rollback de fin de test, voir fixture `db`) pour tester le chemin INSERT : sur
    cette base de dev partagée, les 4 paliers réels existent déjà SANS compte (état constaté,
    pas celui décrit — de façon obsolète — par docs/conformite-credit.md §2bis), donc un
    ré-import se bornerait à les mettre à jour (chemin UPDATE, qui ne touche jamais les
    comptes — voir test_seed_ne_touche_pas_un_compte_deja_rattache ci-dessous)."""
    db.execute(text("DELETE FROM credit.delinquency_tiers"))
    nb = executer_seed_paliers_souffrance(db)
    assert nb == 4
    codes = set(
        db.execute(text("SELECT code FROM credit.delinquency_tiers")).scalars().all()
    )
    assert codes == {"RETARD", "SOUFFRANCE", "DOUTEUX", "IRRECOUVRABLE"}

    retard = db.execute(
        select(DelinquencyTier).where(DelinquencyTier.code == "RETARD")
    ).scalar_one()
    assert retard.seuil_jours == 1
    assert retard.taux_provision_bp == 0
    assert retard.compte_encours_id is None  # non provisionné : aucun compte exigé

    souffrance = db.execute(
        select(DelinquencyTier).where(DelinquencyTier.code == "SOUFFRANCE")
    ).scalar_one()
    assert souffrance.seuil_jours == 90
    assert souffrance.taux_provision_bp == 4000

    douteux = db.execute(
        select(DelinquencyTier).where(DelinquencyTier.code == "DOUTEUX")
    ).scalar_one()
    assert douteux.seuil_jours == 180
    assert douteux.taux_provision_bp == 8000

    irrecouvrable = db.execute(
        select(DelinquencyTier).where(DelinquencyTier.code == "IRRECOUVRABLE")
    ).scalar_one()
    assert irrecouvrable.seuil_jours == 360
    assert irrecouvrable.is_terminal is True
    assert irrecouvrable.taux_provision_bp == 10000
    assert irrecouvrable.is_provisional is True

    # Les 3 paliers provisionnés ont bien leurs 4 comptes résolus par le seed — numéros vérifiés
    # via jointure, jamais un UUID deviné.
    comptes = db.execute(
        text(
            "SELECT ae.account_number, ad.account_number, "
            "       ap.account_number, ar.account_number "
            "FROM credit.delinquency_tiers t "
            "JOIN comptabilite.accounts ae ON ae.id = t.compte_encours_id "
            "JOIN comptabilite.accounts ad ON ad.id = t.compte_dotation_id "
            "JOIN comptabilite.accounts ap ON ap.id = t.compte_provision_id "
            "JOIN comptabilite.accounts ar ON ar.id = t.compte_reprise_id "
            "WHERE t.code = 'SOUFFRANCE'"
        )
    ).one()
    assert comptes == ("292", "66412", "2991", "764")


def test_seed_idempotent_ne_duplique_pas(db: Session) -> None:
    executer_seed_paliers_souffrance(db)
    executer_seed_paliers_souffrance(db)
    total = db.execute(text("SELECT COUNT(*) FROM credit.delinquency_tiers")).scalar_one()
    assert total == 4


def test_seed_ne_touche_pas_un_compte_deja_rattache(db: Session) -> None:
    """Re-jouer le seed ne doit PAS effacer un rattachement déjà posé par l'écran — même
    discipline que le plan de comptes."""
    compte = _compte(db, "9T2E1", normal_side="C")
    executer_seed_paliers_souffrance(db)
    db.execute(
        text(
            "UPDATE credit.delinquency_tiers SET compte_encours_id = :c WHERE code = 'SOUFFRANCE'"
        ),
        {"c": compte.id},
    )
    executer_seed_paliers_souffrance(db)  # rejoué

    encours = db.execute(
        text("SELECT compte_encours_id FROM credit.delinquency_tiers WHERE code = 'SOUFFRANCE'")
    ).scalar_one()
    assert encours == compte.id


# --- Lecture -----------------------------------------------------------------------------


def test_lecture_triee_sur_seuil_jours(client: TestClient, db: Session) -> None:
    # Seuils hors de {1, 90, 180, 360} (seed réel de démonstration) — une base de dev partagée
    # porte déjà ces valeurs, un INSERT dessus violerait l'UNIQUE(seuil_jours).
    _palier(db, "P530", 530)
    _palier(db, "P505", 505)
    _palier(db, "P780", 780)
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.get("/credit/paliers-souffrance", headers=comptable)

    assert reponse.status_code == 200
    seuils = [p["seuil_jours"] for p in reponse.json()]
    assert seuils == sorted(seuils)


def test_lecture_resout_les_comptes_en_numero_jamais_uuid(client: TestClient, db: Session) -> None:
    encours = _compte(db, "9T2E2", normal_side="C")
    _palier(db, "P-LEC", 45, compte_encours_id=encours.id)
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.get("/credit/paliers-souffrance", headers=comptable)

    ligne = next(p for p in reponse.json() if p["code"] == "P-LEC")
    assert ligne["compte_encours"] == {"account_number": "9T2E2", "name": "Compte 9T2E2"}
    assert ligne["compte_dotation"] is None


def test_lecture_sans_permission_403(client: TestClient, db: Session) -> None:
    caissier = _entete_auth(db, "CAISSIER")
    reponse = client.get("/credit/paliers-souffrance", headers=caissier)
    assert reponse.status_code == 403


def test_direction_lit_les_paliers_via_credit_delinquency_read(
    client: TestClient, db: Session
) -> None:
    """DIRECTION_GENERALE n'a PAS compta.plan.read (tout le Bloc 5) — seulement
    credit.delinquency.read (lecture seule des paliers, moindre privilège). exige_une_de doit
    laisser passer malgré l'absence de compta.plan.read."""
    _palier(db, "P-DIR", 50)
    direction = _entete_auth(db, "DIRECTION_GENERALE")

    reponse = client.get("/credit/paliers-souffrance", headers=direction)

    assert reponse.status_code == 200
    assert any(p["code"] == "P-DIR" for p in reponse.json())


def test_direction_ne_peut_pas_modifier_les_paliers(client: TestClient, db: Session) -> None:
    """La lecture élargie (credit.delinquency.read) n'ouvre PAS l'écriture — toujours
    compta.plan.manage seul, que DIRECTION_GENERALE n'a pas."""
    palier = _palier(db, "P-DIR-W", 51)
    direction = _entete_auth(db, "DIRECTION_GENERALE")

    reponse = client.patch(
        f"/credit/paliers-souffrance/{palier.id}",
        json={
            "code": "P-DIR-W", "libelle": "X", "seuil_jours": 51, "taux_provision_bp": 0,
            "compte_encours": None, "compte_dotation": None, "is_terminal": False,
            "motif": "Tentative",
        },
        headers=direction,
    )
    assert reponse.status_code == 403


# --- Création : ajouter un palier, le nombre est une donnée ------------------------------


def test_creation_reussie_avec_motif_trace(client: TestClient, db: Session) -> None:
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.post(
        "/credit/paliers-souffrance",
        json={
            "code": "NOUVEAU",
            "libelle": "Palier intermédiaire",
            # Valeur HORS barème seedé (RETARD 1j, SOUFFRANCE 90j, DOUTEUX 180j, IRRECOUVRABLE
            # 360j) pour éviter la collision UNIQUE(seuil_jours) — jamais une valeur « entre »
            # deux seuils réels, qui redeviendrait occupée au prochain ajustement du barème.
            "seuil_jours": 9999,
            "taux_provision_bp": 2500,
            "compte_encours": None,
            "compte_dotation": None,
            "is_terminal": False,
            "motif": "Ajout d'un palier intermédiaire, retour terrain",
        },
        headers=comptable,
    )

    assert reponse.status_code == 201
    corps = reponse.json()
    assert corps["code"] == "NOUVEAU"
    assert corps["is_provisional"] is True

    ligne = db.execute(
        text(
            "SELECT new_values FROM audit.audit_logs "
            "WHERE action = 'credit.delinquency_tier.created' AND resource_id = :r"
        ),
        {"r": corps["id"]},
    ).one()
    assert ligne.new_values["motif"] == "Ajout d'un palier intermédiaire, retour terrain"


def test_creation_code_deja_utilise_refusee(client: TestClient, db: Session) -> None:
    _palier(db, "DUP", 60)
    db.commit()
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.post(
        "/credit/paliers-souffrance",
        json={
            "code": "DUP", "libelle": "Doublon", "seuil_jours": 61,
            "taux_provision_bp": 0, "compte_encours": None, "compte_dotation": None,
            "is_terminal": False, "motif": "Tentative de doublon",
        },
        headers=comptable,
    )

    assert reponse.status_code == 422
    assert "code" in reponse.json()["detail"].lower()


def test_creation_seuil_deja_utilise_nomme_le_palier_en_conflit(
    client: TestClient, db: Session
) -> None:
    _palier(db, "EXISTANT", 60, libelle="Palier existant")
    db.commit()
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.post(
        "/credit/paliers-souffrance",
        json={
            "code": "AUTRE", "libelle": "Autre", "seuil_jours": 60,
            "taux_provision_bp": 0, "compte_encours": None, "compte_dotation": None,
            "is_terminal": False, "motif": "Tentative de doublon de seuil",
        },
        headers=comptable,
    )

    assert reponse.status_code == 422
    assert "Palier existant" in reponse.json()["detail"]


def test_creation_sans_permission_403(client: TestClient, db: Session) -> None:
    caissier = _entete_auth(db, "CAISSIER")
    reponse = client.post(
        "/credit/paliers-souffrance",
        json={
            "code": "X", "libelle": "X", "seuil_jours": 10, "taux_provision_bp": 0,
            "compte_encours": None, "compte_dotation": None, "is_terminal": False,
            "motif": "Tentative",
        },
        headers=caissier,
    )
    assert reponse.status_code == 403


def test_creation_compte_de_regroupement_directement_soumis_est_refuse(
    client: TestClient, db: Session
) -> None:
    regroupement = _compte(db, "9T2E3", is_posting=False, normal_side="C")
    db.commit()
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.post(
        "/credit/paliers-souffrance",
        json={
            "code": "GRP", "libelle": "Test regroupement", "seuil_jours": 15,
            "taux_provision_bp": 0, "compte_encours": regroupement.account_number,
            "compte_dotation": None, "is_terminal": False,
            "motif": "Tentative de contournement du sélecteur",
        },
        headers=comptable,
    )

    assert reponse.status_code == 422
    assert "regroupement" in reponse.json()["detail"].lower()
    trouve = db.execute(
        select(DelinquencyTier).where(DelinquencyTier.code == "GRP")
    ).scalar_one_or_none()
    assert trouve is None


# --- Modification : remplace l'état complet -----------------------------------------------


def test_modification_reussie_avec_motif_trace(client: TestClient, db: Session) -> None:
    ancien = _compte(db, "9T2E4", normal_side="C")
    nouveau = _compte(db, "9T2E5", normal_side="D")
    palier = _palier(db, "MOD", 60, compte_encours_id=ancien.id)
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.patch(
        f"/credit/paliers-souffrance/{palier.id}",
        json={
            "code": "MOD", "libelle": "Palier modifié", "seuil_jours": 75,
            "taux_provision_bp": 3000, "compte_encours": ancien.account_number,
            "compte_dotation": nouveau.account_number, "is_terminal": False,
            "motif": "Ajustement du seuil suite à revue",
        },
        headers=comptable,
    )

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["seuil_jours"] == 75
    assert corps["taux_provision_bp"] == 3000
    assert corps["compte_dotation"]["account_number"] == "9T2E5"

    ligne = db.execute(
        text(
            "SELECT old_values, new_values FROM audit.audit_logs "
            "WHERE action = 'credit.delinquency_tier.updated' AND resource_id = :r"
        ),
        {"r": palier.id},
    ).one()
    assert ligne.old_values["seuil_jours"] == 60
    assert ligne.new_values["seuil_jours"] == 75


def test_modification_motif_absent_refusee(client: TestClient, db: Session) -> None:
    palier = _palier(db, "NOMOTIF", 20)
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.patch(
        f"/credit/paliers-souffrance/{palier.id}",
        json={
            "code": "NOMOTIF", "libelle": "X", "seuil_jours": 20, "taux_provision_bp": 0,
            "compte_encours": None, "compte_dotation": None, "is_terminal": False, "motif": "",
        },
        headers=comptable,
    )
    assert reponse.status_code == 422


def test_palier_introuvable_404(client: TestClient, db: Session) -> None:
    comptable = _entete_auth(db, "COMPTABLE")
    reponse = client.patch(
        f"/credit/paliers-souffrance/{uuid.uuid4()}",
        json={
            "code": "X", "libelle": "X", "seuil_jours": 1, "taux_provision_bp": 0,
            "compte_encours": None, "compte_dotation": None, "is_terminal": False,
            "motif": "Tentative",
        },
        headers=comptable,
    )
    assert reponse.status_code == 404


def test_modification_compte_desactive_directement_soumis_est_refuse(
    client: TestClient, db: Session
) -> None:
    desactive = _compte(db, "9T2E6", is_active=False, normal_side="C")
    palier = _palier(db, "INACTIF", 25)
    db.commit()
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.patch(
        f"/credit/paliers-souffrance/{palier.id}",
        json={
            "code": "INACTIF", "libelle": "X", "seuil_jours": 25, "taux_provision_bp": 0,
            "compte_encours": desactive.account_number, "compte_dotation": None,
            "is_terminal": False, "motif": "Tentative de contournement du sélecteur",
        },
        headers=comptable,
    )

    assert reponse.status_code == 422
    assert (
        db.execute(
            select(DelinquencyTier.compte_encours_id).where(DelinquencyTier.id == palier.id)
        ).scalar_one()
        is None
    )


# --- Suppression : retirer un palier, le nombre est une donnée ---------------------------


def test_suppression_reussie_avec_motif_trace(client: TestClient, db: Session) -> None:
    palier = _palier(db, "RETIRE", 40)
    comptable = _entete_auth(db, "COMPTABLE")

    reponse = client.post(
        f"/credit/paliers-souffrance/{palier.id}/retirer",
        json={"motif": "Palier créé par erreur"},
        headers=comptable,
    )

    assert reponse.status_code == 204
    trouve = db.execute(
        select(DelinquencyTier).where(DelinquencyTier.id == palier.id)
    ).scalar_one_or_none()
    assert trouve is None
    ligne = db.execute(
        text(
            "SELECT old_values FROM audit.audit_logs "
            "WHERE action = 'credit.delinquency_tier.deleted' AND resource_id = :r"
        ),
        {"r": palier.id},
    ).one()
    assert ligne.old_values["motif"] == "Palier créé par erreur"


def test_suppression_sans_permission_403(client: TestClient, db: Session) -> None:
    palier = _palier(db, "P403", 40)
    caissier = _entete_auth(db, "CAISSIER")
    reponse = client.post(
        f"/credit/paliers-souffrance/{palier.id}/retirer",
        json={"motif": "Tentative"},
        headers=caissier,
    )
    assert reponse.status_code == 403


def test_suppression_palier_introuvable_404(client: TestClient, db: Session) -> None:
    comptable = _entete_auth(db, "COMPTABLE")
    reponse = client.post(
        f"/credit/paliers-souffrance/{uuid.uuid4()}/retirer",
        json={"motif": "Tentative"},
        headers=comptable,
    )
    assert reponse.status_code == 404
