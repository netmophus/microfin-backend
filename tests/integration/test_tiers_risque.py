"""Moteur de score de risque LBC/FT (T3b).

Les cinq cas qui prouvent que le moteur « pense LBC/FT », pas « calculatrice » :
  1. données complètes, risque faible → score 0, faible ;
  2. commerçant PPE → le plancher force « eleve » malgré un score modéré ;
  3. sanctions → couperet : activation bloquée, MAIS le score est quand même calculé et archivé ;
  4. données manquantes → NE conclut pas : recense la donnée manquante, pas de faux score bas ;
  5. calcul PUR : mêmes entrées → snapshot identique (rejouable).
"""

import uuid
from collections.abc import Generator
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import engine
from app.modules.tiers.models import IndividualProfile
from app.modules.tiers.risque import (
    EntreeRisque,
    GrilleSnapshot,
    RegleGrille,
    evaluer,
    evaluer_et_enregistrer,
)

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


# --- grille de test, PURE (miroir du seed v1) : les tests unitaires ne touchent pas la base ---


def _grille() -> GrilleSnapshot:
    return GrilleSnapshot(
        id=uuid.UUID("a0000000-0000-4000-8000-000000000001"),
        version=1,
        libelle="Grille de risque LBC/FT — PROVISOIRE (À VALIDER)",
        is_provisional=True,
        regles=(
            RegleGrille(
                "PAYS_GAFI", "Pays à risque GAFI (gris/noir)",
                "contributive", "pays_gafi", 40, None, False,
            ),
            RegleGrille(
                "SECTEUR_RISQUE", "Secteur d'activité à risque",
                "contributive", "secteur_risque", 30, None, False,
            ),
            RegleGrille(
                "VOLUME_ELEVE", "Volume d'activité élevé",
                "contributive", "volume_eleve", 20, None, False,
            ),
            RegleGrille(
                "MODE_DISTANCE", "Entrée en relation à distance",
                "contributive", "mode_distance", 25, None, False,
            ),
            RegleGrille(
                "MODE_TIERS", "Entrée par tiers de confiance",
                "contributive", "mode_tiers", 10, None, False,
            ),
            RegleGrille(
                "PPE", "Personne politiquement exposée (soi ou entourage)",
                "plancher", "ppe", 0, "eleve", False,
            ),
            RegleGrille(
                "SANCTIONS", "Correspondance liste de sanctions",
                "couperet", "sanctions", 0, None, True,
            ),
        ),
        bareme=(("faible", 0), ("moyen", 30), ("eleve", 60)),
    )


# --- cas 1 : données complètes, risque faible ------------------------------------------


def test_donnees_completes_risque_faible() -> None:
    entrees = EntreeRisque(
        type_tiers="individual",
        nationalite_code="NE",
        nationalite_libelle="Niger",
        nationalite_gafi=False,
        secteur_renseigne=True,
        secteur_libelle="Agriculture, élevage, pêche",
        secteur_a_risque=False,
        profession="Agriculteur",
        ppe_status=False,
        mode_entree_relation="presentiel",
        revenus_estimes=None,
    )
    r = evaluer(entrees, _grille(), "creation")

    assert r.score == 0
    assert r.niveau_bareme == "faible"
    assert r.niveau_effectif == "faible"
    assert r.couperet_declenche is False
    assert r.donnees_manquantes == []  # tout ce qui compte est renseigné
    assert r.detail["resultat"] == {"score": 0, "niveau_effectif": "faible", "bloque": False}


# --- cas 2 : commerçant PPE → plancher force la vigilance renforcée ---------------------


def test_commercant_ppe_le_plancher_force_eleve() -> None:
    entrees = EntreeRisque(
        type_tiers="individual",
        nationalite_code="NE",
        nationalite_libelle="Niger",
        secteur_renseigne=True,
        secteur_libelle="Métaux et pierres précieuses",
        secteur_a_risque=True,
        profession="Négociant en or",
        ppe_status=True,
        ppe_relation="direct",
        ppe_fonction="Maire",
        mode_entree_relation="presentiel",
    )
    r = evaluer(entrees, _grille(), "maj_kyc")

    assert r.score == 30  # secteur à risque
    assert r.niveau_bareme == "moyen"  # 30 → moyen selon le barème
    assert r.niveau_effectif == "eleve"  # RELEVÉ par le plancher PPE
    assert r.couperet_declenche is False
    # Le plancher figure dans le snapshot, lisible.
    planchers = r.detail["calcul"]["planchers_appliques"]
    assert any(p["niveau"] == "eleve" for p in planchers)
    assert "relevé à « eleve »" in r.detail["calcul"]["explication"]


# --- cas 3 : sanctions → couperet bloque, mais le score est archivé --------------------


def test_sanctions_couperet_bloque_mais_score_archive() -> None:
    entrees = EntreeRisque(
        type_tiers="individual",
        nationalite_code="NE",
        nationalite_libelle="Niger",
        secteur_renseigne=True,
        secteur_libelle="Commerce de détail",
        secteur_a_risque=False,
        ppe_status=False,
        mode_entree_relation="presentiel",
        sanctions_match=True,  # simulé (T6 fournira la donnée)
    )
    r = evaluer(entrees, _grille(), "maj_kyc")

    assert r.couperet_declenche is True
    assert r.detail["resultat"]["bloque"] is True
    assert r.niveau_effectif == "eleve"  # un client bloqué n'est jamais « faible »
    # LE POINT : le score est calculé et ARCHIVÉ malgré le couperet (raisonnement complet).
    assert r.detail["resultat"]["score"] == 0
    assert "score" in r.detail["calcul"]
    couperets = r.detail["calcul"]["couperets_declenches"]
    assert len(couperets) == 1


# --- cas 4 : données manquantes → NE conclut pas (le cas LBC/FT) ------------------------


def test_donnees_manquantes_ne_conclut_pas() -> None:
    """Pas de profession, pas de secteur, pas de mode : le moteur ne fabrique PAS un faux score
    bas — il recense les manques (qui deviendront des conditions d'activation) et distingue
    « non évaluée » de « évaluée, pas à risque »."""
    entrees = EntreeRisque(
        type_tiers="individual",
        nationalite_code="NE",
        nationalite_libelle="Niger",
        secteur_renseigne=False,  # secteur inconnu
        profession=None,
        ppe_status=False,
        mode_entree_relation=None,  # mode inconnu
    )
    r = evaluer(entrees, _grille(), "creation")

    # On recense les manques — ils bloqueront l'activation au gate (T3c).
    assert "secteur_activite" in r.donnees_manquantes
    assert "mode_entree_relation" in r.donnees_manquantes

    # Le score n'est pas un « faux bas » présenté comme concluant : les manques sont explicites.
    regles = {rg["libelle"]: rg for rg in r.detail["regles"]}
    secteur = regles["Secteur d'activité à risque"]
    assert secteur["declenchee"] is False
    # « non évaluée » (donnée absente) et NON « pas à risque » (donnée présente négative).
    assert "non renseigné" in secteur["constat"]
    assert r.detail["entrees"]["donnees_manquantes"]  # présent dans le snapshot archivé


# --- cas 5 : calcul PUR, rejouable -----------------------------------------------------


def test_calcul_pur_meme_entree_meme_snapshot() -> None:
    entrees = EntreeRisque(
        type_tiers="individual",
        nationalite_code="NE",
        nationalite_libelle="Niger",
        secteur_renseigne=True,
        secteur_libelle="Métaux et pierres précieuses",
        secteur_a_risque=True,
        ppe_status=True,
        ppe_relation="direct",
        ppe_fonction="Député",
        mode_entree_relation="distance",
    )
    grille = _grille()
    un = evaluer(entrees, grille, "maj_kyc")
    deux = evaluer(entrees, grille, "maj_kyc")

    assert un.detail == deux.detail  # aucun effet de bord, rejouable à l'identique
    assert un.score == deux.score == 55  # secteur 30 + distance 25
    assert un.niveau_effectif == "eleve"  # 55 → moyen, relevé par le plancher PPE


# --- intégration : la vraie grille seedée + persistance append-only --------------------


def _pays(db: Session, code: str) -> uuid.UUID:
    return db.execute(
        text("SELECT id FROM parameters.countries WHERE code = :c"), {"c": code}
    ).scalar_one()


def _secteur(db: Session, code: str) -> uuid.UUID:
    return db.execute(
        text("SELECT id FROM parameters.secteurs_activite WHERE code = :c"), {"c": code}
    ).scalar_one()


def _agence(db: Session) -> uuid.UUID:
    suffixe = uuid.uuid4().hex[:8]
    return db.execute(
        text("INSERT INTO parameters.agencies (code, name) VALUES (:c, :n) RETURNING id"),
        {"c": f"AG-{suffixe}", "n": "Agence de test"},
    ).scalar_one()


def test_evaluer_et_enregistrer_archive_sur_la_vraie_grille(db: Session) -> None:
    tier = IndividualProfile(
        tier_number=f"M-2999-{uuid.uuid4().int % 10_000_000:07d}",
        primary_agency_id=_agence(db),
        last_name="Diallo",
        first_name="Amadou",
        birth_date=date(1990, 5, 12),
        gender="M",
        nationality_id=_pays(db, "SN"),
        secteur_activite_id=_secteur(db, "METAUX_PRECIEUX"),
        ppe_status=True,
        ppe_relation="direct",
        ppe_fonction="Maire",
        mode_entree_relation="presentiel",
    )
    db.add(tier)
    db.flush()

    resultat = evaluer_et_enregistrer(db, None, tier, "maj_kyc")

    # Résultat : secteur à risque (30) → moyen, relevé à élevé par le plancher PPE.
    assert resultat.niveau_effectif == "eleve"

    # Archivé dans l'historique, marqué provisoire (grille v1), avec le snapshot complet.
    ligne = db.execute(
        text(
            "SELECT niveau_effectif, is_provisional, grid_version, detail "
            "FROM tiers.risk_assessments WHERE tier_id = :t"
        ),
        {"t": tier.id},
    ).one()
    assert ligne.niveau_effectif == "eleve"
    assert ligne.is_provisional is True  # gelé : calculé sous grille provisoire
    assert ligne.grid_version == 1
    assert ligne.detail["resultat"]["score"] == 30

    # Reflet mis à jour sur la fiche (cache).
    db.refresh(tier)
    assert tier.risk_level == "eleve"
    assert tier.risk_score == 30
