"""Seed des ratios prudentiels RCSFD (lot P2.1.c) — preuve chiffrée sur les comptes RÉELS du
plan RCSFD (importés par le bootstrap de test, voir conftest.py) que :
  - la provision (299, via sa feuille 2991) se DÉDUIT vraiment du risque brut (292) — pas
    seulement neutralisée à zéro comme l'aurait fait un préfixe large recouvrant son propre
    contra (`19`/`29` corrigés en `191..194`/`291..294` disjoints, voir
    app/cli/seed_conformite.py) ;
  - un sous-compte « rattaché » à sens opposé (1136, Dettes rattachées créditrices nichées
    sous `11` qui est débiteur) est bien NEUTRALISÉ (contribution nette = 0), pas additionné ;
  - `evaluer_tous` tourne sans planter après seed, sur une base sans aucune activité réelle
    (dénominateurs nuls -> NON_CALCULABLE, jamais une exception).
"""

import csv
import uuid
from collections.abc import Generator
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.cli.seed_conformite import (
    AGREGATS,
    RATIOS,
    REFERENCE_REGLEMENTAIRE,
    executer_seed_conformite,
)
from app.core.database import engine
from app.modules.comptabilite import ecritures, journee
from app.modules.comptabilite.ecritures import LigneSaisie
from app.modules.comptabilite.models import Account, Journal
from app.modules.comptabilite.plan import importer
from app.modules.conformite.models import (
    AgregatCompte,
    AgregatPrudentiel,
    RatioPrudentiel,
    RatioSeuil,
)
from app.modules.conformite.moteur import (
    AVERT_FONDS_PROPRES_NULS,
    AVERT_NUMERATEUR_NUL,
    STATUT_CONFORME,
    STATUT_NON_CALCULABLE,
    STATUT_NON_CONFORME,
    agregat_valeur,
    compter_ecritures_validees,
    evaluer_ratio,
    evaluer_tous,
)
from app.modules.security.models import User
from app.modules.security.password import hasher_mot_de_passe

pytestmark = pytest.mark.integration

AUJOURDHUI = date(2026, 6, 15)


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


@pytest.fixture(autouse=True)
def _journee_ouverte(request: pytest.FixtureRequest) -> None:
    if "db" not in request.fixturenames:
        return
    db = request.getfixturevalue("db")
    journee.ouvrir_journee(db, journee.prochaine_date_ouvree(db), None)


def _id_compte(db: Session, numero: str) -> uuid.UUID:
    return db.execute(select(Account.id).where(Account.account_number == numero)).scalar_one()


def _journal_id(db: Session, code: str) -> uuid.UUID:
    return db.execute(select(Journal.id).where(Journal.code == code)).scalar_one()


def _valider_od(db: Session, lignes: list[LigneSaisie], entry_date: date) -> None:
    entry = ecritures.creer_brouillon(
        db,
        journal_id=_journal_id(db, "OD"),
        entry_date=entry_date,
        description="Mouvement de test (seed conformité)",
        lignes=lignes,
        par=None,
    )
    ecritures.valider(db, entry, None)


def test_provision_199_299_se_deduit_vraiment_pas_seulement_neutralisee(db: Session) -> None:
    """Risque brut (292, classe 29) 10 000, provision (2991, sous 299) 3 000 -> net attendu
    7 000. Si la correction 19/199 et 29/299 n'avait pas été appliquée (préfixe large '29'
    recouvrant '299'), la provision se serait neutralisée à elle-même et le résultat aurait
    été 10 000 (brut seul), pas 7 000."""
    executer_seed_conformite(db)

    caisse = _id_compte(db, "101111")
    souffrance = _id_compte(db, "292")
    provision = _id_compte(db, "2991")

    _valider_od(
        db, [LigneSaisie(souffrance, "D", 10_000), LigneSaisie(caisse, "C", 10_000)], AUJOURDHUI
    )
    _valider_od(
        db, [LigneSaisie(caisse, "D", 3_000), LigneSaisie(provision, "C", 3_000)], AUJOURDHUI
    )

    assert agregat_valeur(db, "RISQUES_PORTES", AUJOURDHUI) == 7_000


def test_rattache_sens_oppose_est_neutralise_pas_additionne(db: Session) -> None:
    """1136 (Dettes rattachées, créditrices) niché sous 11 (débiteur) : poser un mouvement
    dessus ne doit PAS faire bouger RISQUES_PORTES — il matche à la fois '11' (+1) et sa
    propre ligne corrective '1136' (-1), contribution nette = 0."""
    executer_seed_conformite(db)

    caisse = _id_compte(db, "101111")
    dette_rattachee = _id_compte(db, "1136")

    avant = agregat_valeur(db, "RISQUES_PORTES", AUJOURDHUI)

    _valider_od(
        db, [LigneSaisie(caisse, "D", 500), LigneSaisie(dette_rattachee, "C", 500)], AUJOURDHUI
    )

    assert agregat_valeur(db, "RISQUES_PORTES", AUJOURDHUI) == avant


def test_rattache_ressources_est_neutralise(db: Session) -> None:
    """Même preuve côté RESSOURCES : 25117 (Créances rattachées, débitrices) niché sous 25
    (créditeur) ne doit pas faire bouger RESSOURCES."""
    executer_seed_conformite(db)

    caisse = _id_compte(db, "101111")
    creance_rattachee = _id_compte(db, "25117")

    avant = agregat_valeur(db, "RESSOURCES", AUJOURDHUI)

    _valider_od(
        db, [LigneSaisie(creance_rattachee, "D", 300), LigneSaisie(caisse, "C", 300)], AUJOURDHUI
    )

    assert agregat_valeur(db, "RESSOURCES", AUJOURDHUI) == avant


def test_evaluer_tous_apres_seed_ne_plante_pas(db: Session) -> None:
    """Base sans aucune activité réelle (juste le seed) : les 3 ratios actifs (#1, #5, #8)
    doivent renvoyer un résultat — NON_CALCULABLE est attendu (dénominateurs nuls), jamais une
    exception, jamais un ratio inactif dans la liste."""
    executer_seed_conformite(db)

    resultats = evaluer_tous(db, AUJOURDHUI)

    assert [r.code for r in resultats] == [
        "RATIO_1_COUVERTURE_RISQUES",
        "RATIO_5_DIVISION_RISQUES",
        "RATIO_8_LIMITATION_PARTICIPATIONS",
    ]
    for resultat in resultats:
        assert resultat.statut == STATUT_NON_CALCULABLE
        assert resultat.conforme is None


# --- Avertissements non bloquants (présentation, jamais du calcul) ------------------------------

RATIO_1 = "RATIO_1_COUVERTURE_RISQUES"


def _codes(resultat: object) -> set[str]:
    return {a.code for a in resultat.avertissements}  # type: ignore[attr-defined]


def test_cas_c_vrai_zero_pourcent_reste_conforme_et_porte_ses_avertissements(
    db: Session,
) -> None:
    """Dépôts (RESSOURCES 5 000) sans aucun risque porté ni aucun fonds propres : le 0,00 % est
    EXACT et reste CONFORME (opérateur ≤ ; on ne le transforme jamais en non calculable), mais il
    porte les deux avertissements qui le rendent lisible."""
    executer_seed_conformite(db)
    caisse = _id_compte(db, "101111")
    depots = _id_compte(db, "251121")
    _valider_od(db, [LigneSaisie(caisse, "D", 5_000), LigneSaisie(depots, "C", 5_000)], AUJOURDHUI)

    resultat = evaluer_ratio(db, RATIO_1, AUJOURDHUI)

    assert resultat.valeur_numerateur == 0
    assert resultat.valeur_denominateur == 5_000
    assert resultat.valeur_ratio_pct == 0
    assert resultat.statut == STATUT_CONFORME
    assert resultat.conforme is True
    assert _codes(resultat) == {AVERT_NUMERATEUR_NUL, AVERT_FONDS_PROPRES_NULS}


def test_fonds_propres_nuls_aussi_signale_sur_le_ratio_dont_il_est_le_denominateur(
    db: Session,
) -> None:
    """Ratio 5 (encours / FONDS_PROPRES) : dénominateur nul -> NON_CALCULABLE, et l'avertissement
    explique pourquoi (des ressources existent mais aucun fonds propres)."""
    executer_seed_conformite(db)
    caisse = _id_compte(db, "101111")
    depots = _id_compte(db, "251121")
    _valider_od(db, [LigneSaisie(caisse, "D", 5_000), LigneSaisie(depots, "C", 5_000)], AUJOURDHUI)

    resultat = evaluer_ratio(db, "RATIO_5_DIVISION_RISQUES", AUJOURDHUI)

    assert resultat.statut == STATUT_NON_CALCULABLE
    assert AVERT_FONDS_PROPRES_NULS in _codes(resultat)
    assert AVERT_NUMERATEUR_NUL not in _codes(resultat)  # dénominateur nul : pas « réel »


def test_base_sans_aucune_ressource_ne_declenche_aucun_avertissement(db: Session) -> None:
    """0/0 : NON_CALCULABLE sans bruit — ni « aucun fonds propres » (RESSOURCES n'est pas
    positif), ni « aucun risque porté » (dénominateur nul)."""
    executer_seed_conformite(db)

    resultat = evaluer_ratio(db, RATIO_1, AUJOURDHUI)

    assert resultat.statut == STATUT_NON_CALCULABLE
    assert resultat.avertissements == ()


def test_ratio_conforme_sain_ne_porte_aucun_avertissement(db: Session) -> None:
    """Preuve qu'on ne pollue pas les cas sains : risques 150 000, dépôts 100 000, réserves
    50 000 -> ressources 150 000, ratio 100 % ≤ 200 %, conforme, et AUCUN avertissement."""
    executer_seed_conformite(db)
    credits = _id_compte(db, "202221")
    depots = _id_compte(db, "251121")
    reserves = _id_compte(db, "5521")
    _valider_od(
        db,
        [
            LigneSaisie(credits, "D", 150_000),
            LigneSaisie(depots, "C", 100_000),
            LigneSaisie(reserves, "C", 50_000),
        ],
        AUJOURDHUI,
    )

    resultat = evaluer_ratio(db, RATIO_1, AUJOURDHUI)

    assert resultat.statut == STATUT_CONFORME
    assert resultat.valeur_ratio_pct == 100
    assert resultat.avertissements == ()


def test_les_avertissements_ne_changent_ni_la_valeur_ni_le_statut(db: Session) -> None:
    """Un ratio NON_CONFORME garde sa valeur et son statut : l'avertissement s'ajoute, il ne
    corrige rien. Risques 600 000, dépôts 200 000 (produits 400 000 en contrepartie) -> 300 %."""
    executer_seed_conformite(db)
    credits = _id_compte(db, "202221")
    depots = _id_compte(db, "251121")
    produits = _id_compte(db, "7021")
    _valider_od(
        db,
        [
            LigneSaisie(credits, "D", 600_000),
            LigneSaisie(depots, "C", 200_000),
            LigneSaisie(produits, "C", 400_000),
        ],
        AUJOURDHUI,
    )

    resultat = evaluer_ratio(db, RATIO_1, AUJOURDHUI)

    assert resultat.valeur_ratio_pct == 300
    assert resultat.statut == "NON_CONFORME"
    assert _codes(resultat) == {AVERT_FONDS_PROPRES_NULS}


def test_compteur_ecritures_validees_distingue_une_vraie_base_vide(db: Session) -> None:
    avant = compter_ecritures_validees(db, AUJOURDHUI)
    assert compter_ecritures_validees(db, date(1900, 1, 1)) == 0  # avant toute écriture

    caisse = _id_compte(db, "101111")
    depots = _id_compte(db, "251121")
    _valider_od(db, [LigneSaisie(caisse, "D", 100), LigneSaisie(depots, "C", 100)], AUJOURDHUI)

    assert compter_ecritures_validees(db, AUJOURDHUI) == avant + 1
    assert compter_ecritures_validees(db, date(1900, 1, 1)) == 0  # la date d'arrêté compte


# --- Resynchronisation gardée : référence réglementaire + libellés « en attente » -------------

CODE_RATIO_ANCIEN = "RATIO_2_CAPITALISATION"
CODE_AGREGAT_ANCIEN = "TOTAL_ACTIF_NET"


def _ratio_par_code(db: Session, code: str) -> RatioPrudentiel:
    return db.execute(select(RatioPrudentiel).where(RatioPrudentiel.code == code)).scalar_one()


def _agregat_par_code(db: Session, code: str) -> AgregatPrudentiel:
    return db.execute(
        select(AgregatPrudentiel).where(AgregatPrudentiel.code == code)
    ).scalar_one()


def _ancien_libelle_ratio(code: str) -> str:
    ancien = next(r.ancien_libelle for r in RATIOS if r.code == code)
    assert ancien is not None
    return ancien


def _ancien_libelle_agregat(code: str) -> str:
    ancien = next(a.ancien_libelle for a in AGREGATS if a.code == code)
    assert ancien is not None
    return ancien


def _utilisateur_id(db: Session) -> uuid.UUID:
    agence = db.execute(text("SELECT id FROM parameters.agencies LIMIT 1")).scalar_one()
    s = uuid.uuid4().hex[:8]
    user = User(
        matricule=f"MAT-{s}", email=f"{s}@ex.com", username=f"u{s}",
        password_hash=hasher_mot_de_passe("Motdepasse!123"), last_name="T", first_name="A",
        primary_agency_id=agence,
    )
    db.add(user)
    db.flush()
    return user.id


def _revenir_a_l_etat_ancien(db: Session) -> None:
    """Reproduit une base seedée AVANT la correction : référence NULL, ancien libellé."""
    for definition in RATIOS:
        ratio = _ratio_par_code(db, definition.code)
        ratio.reference_reglementaire = None
        if definition.ancien_libelle is not None:
            ratio.libelle = definition.ancien_libelle
    for agregat_def in AGREGATS:
        if agregat_def.ancien_libelle is not None:
            _agregat_par_code(db, agregat_def.code).libelle = agregat_def.ancien_libelle
    db.flush()


def test_les_10_ratios_ont_une_reference_et_aucun_libelle_en_attente(db: Session) -> None:
    executer_seed_conformite(db)

    ratios = db.execute(select(RatioPrudentiel)).scalars().all()
    agregats = db.execute(select(AgregatPrudentiel)).scalars().all()

    assert len(ratios) == 10
    # Chaque ratio cite l'Instruction 010-08-2010 ; le #8 y ajoute la 016-12-2010 (câblage P2.0-b2).
    assert all("010-08-2010" in (r.reference_reglementaire or "") for r in ratios)
    assert sum(r.reference_reglementaire == REFERENCE_REGLEMENTAIRE for r in ratios) == 9
    assert REFERENCE_REGLEMENTAIRE == "Instruction 010-08-2010"
    assert not [r.libelle for r in ratios if "(en attente" in r.libelle]
    assert not [a.libelle for a in agregats if "(en attente" in a.libelle]


def test_rejeu_sur_base_a_jour_ne_resynchronise_rien(db: Session) -> None:
    executer_seed_conformite(db)

    rejeu = executer_seed_conformite(db)

    assert rejeu.references_resynchronisees == 0
    assert rejeu.libelles_resynchronises == 0
    assert rejeu.ratios_crees == 0
    assert rejeu.agregats_crees == 0


def test_lignes_anciennes_sont_corrigees_au_rejeu_puis_idempotent(db: Session) -> None:
    """Base seedée avant la correction (référence NULL, ancien libellé, jamais retouchée) :
    10 références + 8 libellés de ratios + 10 libellés d'agrégats resynchronisés — une seule
    fois."""
    executer_seed_conformite(db)
    _revenir_a_l_etat_ancien(db)

    rejeu = executer_seed_conformite(db)

    assert rejeu.references_resynchronisees == 10
    assert rejeu.libelles_resynchronises == 8 + 10
    assert _ratio_par_code(db, CODE_RATIO_ANCIEN).libelle == "Capitalisation générale"
    assert _ratio_par_code(db, CODE_RATIO_ANCIEN).reference_reglementaire == (
        REFERENCE_REGLEMENTAIRE
    )
    assert _agregat_par_code(db, CODE_AGREGAT_ANCIEN).libelle == "Total actif net"

    deuxieme = executer_seed_conformite(db)
    assert deuxieme.references_resynchronisees == 0
    assert deuxieme.libelles_resynchronises == 0


def test_garde_libelle_modifie_a_la_main_est_preserve(db: Session) -> None:
    executer_seed_conformite(db)
    _revenir_a_l_etat_ancien(db)
    _ratio_par_code(db, CODE_RATIO_ANCIEN).libelle = "Capitalisation (libellé maison)"
    _agregat_par_code(db, CODE_AGREGAT_ANCIEN).libelle = "Actif net (libellé maison)"
    db.flush()

    executer_seed_conformite(db)

    assert _ratio_par_code(db, CODE_RATIO_ANCIEN).libelle == "Capitalisation (libellé maison)"
    assert _agregat_par_code(db, CODE_AGREGAT_ANCIEN).libelle == "Actif net (libellé maison)"


def test_garde_reference_deja_renseignee_est_preservee(db: Session) -> None:
    executer_seed_conformite(db)
    _revenir_a_l_etat_ancien(db)
    _ratio_par_code(db, CODE_RATIO_ANCIEN).reference_reglementaire = "Circulaire interne 12"
    db.flush()

    executer_seed_conformite(db)

    assert _ratio_par_code(db, CODE_RATIO_ANCIEN).reference_reglementaire == (
        "Circulaire interne 12"
    )


def test_garde_ligne_retouchee_par_un_utilisateur_n_est_pas_touchee(db: Session) -> None:
    """`updated_by` renseigné = quelqu'un a modifié la ligne par l'écran/l'API : ni sa référence
    vide, ni son libellé (même égal à l'ancien) ne sont resynchronisés."""
    executer_seed_conformite(db)
    _revenir_a_l_etat_ancien(db)
    utilisateur = _utilisateur_id(db)
    ratio = _ratio_par_code(db, CODE_RATIO_ANCIEN)
    agregat = _agregat_par_code(db, CODE_AGREGAT_ANCIEN)
    ratio.updated_by = utilisateur
    agregat.updated_by = utilisateur
    db.flush()

    rejeu = executer_seed_conformite(db)

    assert ratio.reference_reglementaire is None
    assert ratio.libelle == _ancien_libelle_ratio(CODE_RATIO_ANCIEN)
    assert agregat.libelle == _ancien_libelle_agregat(CODE_AGREGAT_ANCIEN)
    # Les 9 autres ratios et 9 autres agrégats, eux, n'ont pas été retouchés.
    assert rejeu.references_resynchronisees == 9
    assert rejeu.libelles_resynchronises == 7 + 9


def test_garde_ligne_non_systeme_n_est_pas_touchee(db: Session) -> None:
    executer_seed_conformite(db)
    _revenir_a_l_etat_ancien(db)
    ratio = _ratio_par_code(db, CODE_RATIO_ANCIEN)
    ratio.is_system = False
    db.flush()

    executer_seed_conformite(db)

    assert ratio.reference_reglementaire is None
    assert ratio.libelle == _ancien_libelle_ratio(CODE_RATIO_ANCIEN)


def test_seed_est_idempotent(db: Session) -> None:
    """Un second passage ne duplique rien (codes UNIQUE)."""
    executer_seed_conformite(db)
    nb_ratios_avant = db.execute(
        text("SELECT count(*) FROM conformite.ratio_prudentiel")
    ).scalar_one()

    executer_seed_conformite(db)
    nb_ratios_apres = db.execute(
        text("SELECT count(*) FROM conformite.ratio_prudentiel")
    ).scalar_one()

    assert nb_ratios_avant == nb_ratios_apres == len(RATIOS)


# --- Ratio #8 — limitation des titres de participation (P2.0-b2) -------------------------------

RATIO_8 = "RATIO_8_LIMITATION_PARTICIPATIONS"
AGREGAT_PARTICIPATIONS = "PARTICIPATIONS_HORS_SFD_EC"
CSV_PLAN_B2 = (
    Path(__file__).resolve().parents[2] / "docs" / "reference" / "plan_comptable_import.csv"
)


def _composition(db: Session, code_agregat: str) -> set[tuple[str, int]]:
    lignes = db.execute(
        select(AgregatCompte.prefixe_compte, AgregatCompte.sens)
        .join(AgregatPrudentiel, AgregatPrudentiel.id == AgregatCompte.agregat_id)
        .where(AgregatPrudentiel.code == code_agregat)
    ).all()
    return {(prefixe, sens) for prefixe, sens in lignes}


def _seuils(db: Session, code_ratio: str) -> list[tuple[str | None, int]]:
    ratio = _ratio_par_code(db, code_ratio)
    lignes = db.execute(
        select(RatioSeuil.categorie_sfd, RatioSeuil.valeur_seuil).where(
            RatioSeuil.ratio_id == ratio.id
        )
    ).all()
    return [(categorie, int(valeur)) for categorie, valeur in lignes]


def _poser_participations(db: Session, *, brut: int, provision: int) -> None:
    """Fonds propres 100 000 (5521) ; brut 412300 et provision prélevés sur la caisse. La provision
    se saisit sur 412930 (4129 est un regroupement) : #8, qui pointe encore le préfixe 4129, la
    capte par préfixe."""
    importer(db, str(CSV_PLAN_B2))  # 412300 / 412100 sont des comptes P2.0-b1
    caisse = _id_compte(db, "101111")
    _valider_od(
        db,
        [LigneSaisie(caisse, "D", 100_000), LigneSaisie(_id_compte(db, "5521"), "C", 100_000)],
        AUJOURDHUI,
    )
    if brut:
        _valider_od(
            db,
            [LigneSaisie(_id_compte(db, "412300"), "D", brut), LigneSaisie(caisse, "C", brut)],
            AUJOURDHUI,
        )
    if provision:
        _valider_od(
            db,
            [
                LigneSaisie(caisse, "D", provision),
                LigneSaisie(_id_compte(db, "412930"), "C", provision),
            ],
            AUJOURDHUI,
        )


def test_ratio_8_est_cable_et_actif_apres_seed(db: Session) -> None:
    executer_seed_conformite(db)

    ratio = _ratio_par_code(db, RATIO_8)
    numerateur = db.get(AgregatPrudentiel, ratio.agregat_numerateur_id)
    denominateur = db.get(AgregatPrudentiel, ratio.agregat_denominateur_id)

    assert ratio.actif is True
    assert ratio.operateur == "LE"
    assert numerateur is not None and numerateur.code == AGREGAT_PARTICIPATIONS
    assert denominateur is not None and denominateur.code == "FONDS_PROPRES"  # réutilisé tel quel
    assert "010-08-2010" in (ratio.reference_reglementaire or "")
    assert "016-12-2010" in (ratio.reference_reglementaire or "")
    assert _seuils(db, RATIO_8) == [(None, 25)]  # seuil universel, comme #1 et #5
    assert _composition(db, AGREGAT_PARTICIPATIONS) == {("412300", 1), ("4129", -1)}
    assert numerateur.nets_de_provisions is True
    assert "4129" in (numerateur.reference or "")  # approximation documentée dans l'agrégat
    # L'ancien placeholder vide n'est plus semé.
    assert not db.execute(
        select(AgregatPrudentiel).where(AgregatPrudentiel.code == "PARTICIPATIONS")
    ).first()


def test_412300_et_4129_ont_des_prefixes_disjoints() -> None:
    """Pas de piège 19/199 : le préfixe du brut ne ramasse pas la provision, ni l'inverse."""
    assert not "4129".startswith("412300")
    assert not "412300".startswith("4129")
    with open(CSV_PLAN_B2, encoding="utf-8-sig", newline="") as f:
        numeros = [ligne["account_number"] for ligne in csv.DictReader(f, delimiter=";")]
    assert [n for n in numeros if n.startswith("412300")] == ["412300"]
    assert [n for n in numeros if n.startswith("4129")] == ["4129", "412910", "412930"]
    assert not any(n.startswith("412300") for n in ("4129", "412910", "412930"))


def test_ratio_8_chiffre_conforme_a_22_pour_cent_puis_non_conforme_a_30(db: Session) -> None:
    """Brut 30 000, provision 4129 de 8 000 -> net 22 000 ; fonds propres 100 000 -> 22 %."""
    executer_seed_conformite(db)
    _poser_participations(db, brut=30_000, provision=8_000)

    resultat = evaluer_ratio(db, RATIO_8, AUJOURDHUI)

    assert resultat.valeur_numerateur == 22_000
    assert resultat.valeur_denominateur == 100_000
    assert resultat.valeur_ratio_pct == 22
    assert resultat.statut == STATUT_CONFORME
    assert resultat.marge == 3  # 25 - 22
    assert resultat.avertissements == ()

    # Brut porté à 38 000 : net 30 000 -> 30 % > 25 %.
    _valider_od(
        db,
        [
            LigneSaisie(_id_compte(db, "412300"), "D", 8_000),
            LigneSaisie(_id_compte(db, "101111"), "C", 8_000),
        ],
        AUJOURDHUI,
    )
    depasse = evaluer_ratio(db, RATIO_8, AUJOURDHUI)

    assert depasse.valeur_numerateur == 30_000
    assert depasse.valeur_ratio_pct == 30
    assert depasse.statut == STATUT_NON_CONFORME
    assert depasse.marge == -5


def test_ratio_8_exclut_le_bucket_sfd_et_etablissements_de_credit(db: Session) -> None:
    """412100 (SFD + établissements de crédit) est déduit des fonds propres et exclu de #8 : le
    poser ne change PAS le numérateur."""
    executer_seed_conformite(db)
    _poser_participations(db, brut=30_000, provision=8_000)
    avant = evaluer_ratio(db, RATIO_8, AUJOURDHUI)
    _valider_od(
        db,
        [
            LigneSaisie(_id_compte(db, "412100"), "D", 50_000),
            LigneSaisie(_id_compte(db, "101111"), "C", 50_000),
        ],
        AUJOURDHUI,
    )

    apres = evaluer_ratio(db, RATIO_8, AUJOURDHUI)

    assert apres.valeur_numerateur == avant.valeur_numerateur == 22_000


def test_ratio_8_deduit_toute_la_provision_4129_meme_sans_brut(db: Session) -> None:
    """Approximation prudente documentée : 4129 est un compte global unique, TOUTE sa provision
    est soustraite du seul brut 412300 — le numérateur ne peut que diminuer."""
    executer_seed_conformite(db)
    _poser_participations(db, brut=0, provision=8_000)

    assert agregat_valeur(db, AGREGAT_PARTICIPATIONS, AUJOURDHUI) == -8_000


def test_ratios_1_et_5_ne_bougent_pas_quand_le_ratio_8_est_actif(db: Session) -> None:
    """Risques 150 000, dépôts 100 000, réserves 50 000 : #1 = 100 % conforme sans avertissement ;
    #5 = 0 % (aucun encours), et l'ordre des ratios actifs est #1, #5, #8."""
    executer_seed_conformite(db)
    credits = _id_compte(db, "202221")
    depots = _id_compte(db, "251121")
    reserves = _id_compte(db, "5521")
    _valider_od(
        db,
        [
            LigneSaisie(credits, "D", 150_000),
            LigneSaisie(depots, "C", 100_000),
            LigneSaisie(reserves, "C", 50_000),
        ],
        AUJOURDHUI,
    )

    resultats = {r.code: r for r in evaluer_tous(db, AUJOURDHUI)}

    assert list(resultats) == [
        "RATIO_1_COUVERTURE_RISQUES",
        "RATIO_5_DIVISION_RISQUES",
        RATIO_8,
    ]
    un = resultats["RATIO_1_COUVERTURE_RISQUES"]
    assert (un.valeur_numerateur, un.valeur_denominateur) == (150_000, 150_000)
    assert un.valeur_ratio_pct == 100
    assert un.statut == STATUT_CONFORME
    assert un.avertissements == ()
    cinq = resultats["RATIO_5_DIVISION_RISQUES"]
    assert (cinq.valeur_numerateur, cinq.valeur_denominateur) == (0, 50_000)
    assert cinq.statut == STATUT_CONFORME


def _remettre_ratio_8_a_l_etat_ancien(db: Session) -> RatioPrudentiel:
    """Base seedée AVANT le câblage : numérateur = ancien placeholder vide, inactif, sans
    seuil, référence d'origine."""
    ratio = _ratio_par_code(db, RATIO_8)
    placeholder = AgregatPrudentiel(
        code="PARTICIPATIONS",
        libelle="Participations hors établissements de crédit et SFD",
        type="BALANCE",
        is_system=True,
    )
    db.add(placeholder)
    db.flush()
    ratio.agregat_numerateur_id = placeholder.id
    ratio.actif = False
    ratio.reference_reglementaire = REFERENCE_REGLEMENTAIRE
    db.execute(text("DELETE FROM conformite.ratio_seuil WHERE ratio_id = :r"), {"r": ratio.id})
    db.flush()
    return ratio


def test_base_existante_est_recablee_au_rejeu_puis_idempotent(db: Session) -> None:
    executer_seed_conformite(db)
    ratio = _remettre_ratio_8_a_l_etat_ancien(db)

    rejeu = executer_seed_conformite(db)

    assert rejeu.ratios_recables == 1
    assert rejeu.seuils_crees == 1
    numerateur = db.get(AgregatPrudentiel, ratio.agregat_numerateur_id)
    assert numerateur is not None and numerateur.code == AGREGAT_PARTICIPATIONS
    assert ratio.actif is True
    assert "016-12-2010" in (ratio.reference_reglementaire or "")
    assert _seuils(db, RATIO_8) == [(None, 25)]

    deuxieme = executer_seed_conformite(db)
    assert deuxieme.ratios_recables == 0
    assert deuxieme.seuils_crees == 0


def test_garde_ratio_8_retouche_par_un_utilisateur_n_est_pas_recable(db: Session) -> None:
    executer_seed_conformite(db)
    ratio = _remettre_ratio_8_a_l_etat_ancien(db)
    ratio.updated_by = _utilisateur_id(db)
    db.flush()

    rejeu = executer_seed_conformite(db)

    assert rejeu.ratios_recables == 0
    assert ratio.actif is False
    assert _seuils(db, RATIO_8) == []


def test_garde_ratio_8_avec_seuil_deja_pose_n_est_pas_recable(db: Session) -> None:
    """Quelqu'un a commencé à le paramétrer (un seuil existe) : le seed n'y touche plus."""
    executer_seed_conformite(db)
    ratio = _remettre_ratio_8_a_l_etat_ancien(db)
    db.add(RatioSeuil(ratio_id=ratio.id, categorie_sfd=None, valeur_seuil=30))
    db.flush()

    rejeu = executer_seed_conformite(db)

    assert rejeu.ratios_recables == 0
    assert ratio.actif is False
    assert _seuils(db, RATIO_8) == [(None, 30)]  # son seuil n'est pas écrasé


def test_ratio_1_deduit_les_deux_sous_provisions_par_le_prefixe_4129(db: Session) -> None:
    """Ventilation de 4129 : la ligne (4129, -1) de RISQUES_PORTES capte 412910 ET 412930, la
    provision totale reste déduite sans modifier l'agrégat. Crédits 150 000 ; provisions
    412910 4 000 + 412930 6 000 -> risques portés nets 140 000."""
    executer_seed_conformite(db)
    importer(db, str(CSV_PLAN_B2))
    credits = _id_compte(db, "202221")
    caisse = _id_compte(db, "101111")
    _valider_od(
        db, [LigneSaisie(credits, "D", 150_000), LigneSaisie(caisse, "C", 150_000)], AUJOURDHUI
    )
    _valider_od(
        db,
        [
            LigneSaisie(caisse, "D", 10_000),
            LigneSaisie(_id_compte(db, "412910"), "C", 4_000),
            LigneSaisie(_id_compte(db, "412930"), "C", 6_000),
        ],
        AUJOURDHUI,
    )

    assert ("4129", -1) in _composition(db, "RISQUES_PORTES")
    assert agregat_valeur(db, "RISQUES_PORTES", AUJOURDHUI) == 140_000
