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
from decimal import Decimal
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
from app.cli.seed_financial_statement_mapping import executer_seed_mapping_etats
from app.core.database import engine
from app.modules.comptabilite import ecritures, etats_financiers, journee
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
    """Base sans aucune activité réelle (juste le seed) : les 5 ratios actifs (#1, #2, #5, #8, #9)
    doivent renvoyer un résultat — NON_CALCULABLE est attendu (dénominateurs nuls), jamais une
    exception, jamais un ratio inactif dans la liste."""
    executer_seed_conformite(db)

    resultats = evaluer_tous(db, AUJOURDHUI)

    assert [r.code for r in resultats] == [
        "RATIO_1_COUVERTURE_RISQUES",
        "RATIO_2_CAPITALISATION",
        "RATIO_5_DIVISION_RISQUES",
        "RATIO_8_LIMITATION_PARTICIPATIONS",
        "RATIO_9_IMMOS_PLUS_PARTICIPATIONS",
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
    # Chaque ratio cite l'Instruction 010-08-2010 ; #8 et #9 y ajoutent la 016-12-2010.
    assert all("010-08-2010" in (r.reference_reglementaire or "") for r in ratios)
    assert sum(r.reference_reglementaire == REFERENCE_REGLEMENTAIRE for r in ratios) == 8
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
    assert rejeu.libelles_resynchronises == 8 + 9
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
    # Les 9 autres ratios et 8 autres agrégats, eux, n'ont pas été retouchés.
    assert rejeu.references_resynchronisees == 9
    assert rejeu.libelles_resynchronises == 7 + 8


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
    se saisit sur 412930 (4129 est un regroupement), la provision dédiée du bucket de #8."""
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
    assert _composition(db, AGREGAT_PARTICIPATIONS) == {("412300", 1), ("412930", -1)}
    assert numerateur.nets_de_provisions is True
    assert "412930" in (numerateur.reference or "")  # provision dédiée documentée
    assert "prudente" not in (numerateur.reference or "")
    # L'ancien placeholder vide n'est plus semé.
    assert not db.execute(
        select(AgregatPrudentiel).where(AgregatPrudentiel.code == "PARTICIPATIONS")
    ).first()


def test_412300_et_412930_ont_des_prefixes_disjoints() -> None:
    """Pas de piège 19/199 : le préfixe du brut ne ramasse pas la provision, ni l'inverse."""
    assert not "412930".startswith("412300")
    assert not "412300".startswith("412930")
    with open(CSV_PLAN_B2, encoding="utf-8-sig", newline="") as f:
        numeros = [ligne["account_number"] for ligne in csv.DictReader(f, delimiter=";")]
    assert [n for n in numeros if n.startswith("412300")] == ["412300"]
    assert [n for n in numeros if n.startswith("4129")] == ["4129", "412910", "412930"]
    assert not any(n.startswith("412300") for n in ("4129", "412910", "412930"))


def test_ratio_8_chiffre_conforme_a_22_pour_cent_puis_non_conforme_a_30(db: Session) -> None:
    """Brut 30 000, provision 412930 de 8 000 -> net 22 000 ; fonds propres 100 000 -> 22 %."""
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


def test_ratio_8_deduit_sa_provision_412930_meme_sans_brut(db: Session) -> None:
    """#8 déduit la provision DÉDIÉE 412930 du seul brut 412300 : sans brut, le numérateur est
    négatif."""
    executer_seed_conformite(db)
    _poser_participations(db, brut=0, provision=8_000)

    assert agregat_valeur(db, AGREGAT_PARTICIPATIONS, AUJOURDHUI) == -8_000


def test_ratio_8_n_est_pas_affecte_par_une_provision_du_bucket_sfd_et_etablissements(
    db: Session,
) -> None:
    """Tout l'intérêt de la ventilation : une provision saisie sur 412910 (titres dans SFD et
    établissements de crédit) ne change PAS le numérateur de #8, alors que 412930 le change."""
    executer_seed_conformite(db)
    _poser_participations(db, brut=30_000, provision=8_000)
    avant = evaluer_ratio(db, RATIO_8, AUJOURDHUI)
    assert avant.valeur_numerateur == 22_000
    caisse = _id_compte(db, "101111")

    _valider_od(
        db,
        [LigneSaisie(caisse, "D", 5_000), LigneSaisie(_id_compte(db, "412910"), "C", 5_000)],
        AUJOURDHUI,
    )
    apres_412910 = evaluer_ratio(db, RATIO_8, AUJOURDHUI)
    assert apres_412910.valeur_numerateur == 22_000

    _valider_od(
        db,
        [LigneSaisie(caisse, "D", 2_000), LigneSaisie(_id_compte(db, "412930"), "C", 2_000)],
        AUJOURDHUI,
    )
    assert evaluer_ratio(db, RATIO_8, AUJOURDHUI).valeur_numerateur == 20_000


def test_ratio_8_ne_deduit_plus_par_le_prefixe_4129(db: Session) -> None:
    """La composition ne contient plus aucune ligne 4129 : seul le préfixe 412930 déduit."""
    executer_seed_conformite(db)

    prefixes = {p for p, _ in _composition(db, AGREGAT_PARTICIPATIONS)}
    assert prefixes == {"412300", "412930"}


def test_ratio_1_continue_de_pointer_4129_et_capte_les_deux_sous_provisions(db: Session) -> None:
    executer_seed_conformite(db)

    assert ("4129", -1) in _composition(db, "RISQUES_PORTES")


def _remettre_agregat_participations_a_l_etat_p20b2(db: Session) -> AgregatPrudentiel:
    """Base seedée AVANT la correction : 412300 (+1) / 4129 (-1), référence d'origine."""
    agregat = db.execute(
        select(AgregatPrudentiel).where(AgregatPrudentiel.code == AGREGAT_PARTICIPATIONS)
    ).scalar_one()
    db.execute(
        text(
            "UPDATE conformite.agregat_compte SET prefixe_compte = '4129' "
            "WHERE agregat_id = :a AND prefixe_compte = '412930'"
        ),
        {"a": agregat.id},
    )
    agregat.reference = (
        "Instruction 010-08-2010 et 016-12-2010 — brut 412300 moins TOUTE la "
        "provision 4129 (compte global unique) : approximation prudente"
    )
    db.flush()
    db.refresh(agregat)
    return agregat


def test_composition_de_8_est_recablee_au_rejeu_puis_idempotente(db: Session) -> None:
    executer_seed_conformite(db)
    agregat = _remettre_agregat_participations_a_l_etat_p20b2(db)
    assert _composition(db, AGREGAT_PARTICIPATIONS) == {("412300", 1), ("4129", -1)}

    rejeu = executer_seed_conformite(db)

    assert rejeu.agregats_recables == 1
    assert _composition(db, AGREGAT_PARTICIPATIONS) == {("412300", 1), ("412930", -1)}
    assert "412930" in (agregat.reference or "") and "prudente" not in (agregat.reference or "")
    assert executer_seed_conformite(db).agregats_recables == 0


def test_garde_composition_retouchee_ou_agregat_modifie_n_est_pas_recablee(db: Session) -> None:
    executer_seed_conformite(db)
    agregat = _remettre_agregat_participations_a_l_etat_p20b2(db)
    agregat.updated_by = _utilisateur_id(db)  # quelqu'un l'a paramétré via l'API
    db.flush()

    rejeu = executer_seed_conformite(db)

    assert rejeu.agregats_recables == 0
    assert _composition(db, AGREGAT_PARTICIPATIONS) == {("412300", 1), ("4129", -1)}


def test_garde_composition_differente_de_l_ancienne_n_est_pas_recablee(db: Session) -> None:
    executer_seed_conformite(db)
    agregat = _remettre_agregat_participations_a_l_etat_p20b2(db)
    db.add(AgregatCompte(agregat_id=agregat.id, prefixe_compte="4126", sens=1, is_system=True))
    db.flush()

    rejeu = executer_seed_conformite(db)

    assert rejeu.agregats_recables == 0
    assert ("4129", -1) in _composition(db, AGREGAT_PARTICIPATIONS)


def test_ratios_1_et_5_ne_bougent_pas_quand_le_ratio_8_est_actif(db: Session) -> None:
    """Risques 150 000, dépôts 100 000, réserves 50 000 : #1 = 100 % conforme sans avertissement ;
    #5 = 0 % (aucun encours), et l'ordre des ratios actifs est #1, #2, #5, #8, #9."""
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
        "RATIO_2_CAPITALISATION",
        "RATIO_5_DIVISION_RISQUES",
        RATIO_8,
        "RATIO_9_IMMOS_PLUS_PARTICIPATIONS",
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


# --- Ratio #2 : capitalisation générale (P2.0-c) -------------------------------------------------

RATIO_2 = "RATIO_2_CAPITALISATION"
PREFIXES_DEDUCTIONS_FP = ("412100", "412910", "4311", "4319", "441100", "4418", "4419")


def _preparer_plan_et_mapping(db: Session) -> None:
    """Seed conformité + plan réel + mapping des états financiers (le total actif net du bilan en
    dépend)."""
    executer_seed_conformite(db)
    importer(db, str(CSV_PLAN_B2))
    executer_seed_mapping_etats(db)


def _poser_bilan_simple(db: Session, *, fonds_propres: int, depots: int) -> None:
    """Caisse = fonds propres + dépôts : actif net = fonds_propres + depots."""
    _valider_od(
        db,
        [
            LigneSaisie(_id_compte(db, "101111"), "D", fonds_propres + depots),
            LigneSaisie(_id_compte(db, "5521"), "C", fonds_propres),
            LigneSaisie(_id_compte(db, "251121"), "C", depots),
        ],
        AUJOURDHUI,
    )


def test_ratio_2_est_cable_et_actif_apres_seed(db: Session) -> None:
    executer_seed_conformite(db)

    ratio = _ratio_par_code(db, RATIO_2)
    numerateur = db.get(AgregatPrudentiel, ratio.agregat_numerateur_id)
    denominateur = db.get(AgregatPrudentiel, ratio.agregat_denominateur_id)

    assert ratio.actif is True
    assert ratio.operateur == "GE"
    assert _seuils(db, RATIO_2) == [(None, 15)]  # seuil universel, minimum
    assert numerateur is not None and numerateur.code == "FONDS_PROPRES"
    assert denominateur is not None and denominateur.code == "TOTAL_ACTIF_NET"
    assert denominateur.type == "SPECIAL" and denominateur.calcul_special == "TOTAL_ACTIF_NET"
    assert _composition(db, "TOTAL_ACTIF_NET") == set()  # un SPECIAL n'a pas de composition


def test_total_actif_net_special_est_positif_et_egal_au_bilan(db: Session) -> None:
    _preparer_plan_et_mapping(db)
    _poser_bilan_simple(db, fonds_propres=200_000, depots=800_000)

    valeur = agregat_valeur(db, "TOTAL_ACTIF_NET", AUJOURDHUI)

    assert valeur == 1_000_000
    assert valeur == etats_financiers.bilan(db, AUJOURDHUI).total_actif_net


def test_ratio_2_conforme_a_20_pour_cent(db: Session) -> None:
    _preparer_plan_et_mapping(db)
    _poser_bilan_simple(db, fonds_propres=200_000, depots=800_000)

    resultat = evaluer_ratio(db, RATIO_2, AUJOURDHUI)

    assert resultat.valeur_numerateur == 200_000
    assert resultat.valeur_denominateur == 1_000_000
    assert resultat.valeur_ratio_pct == 20
    assert resultat.statut == STATUT_CONFORME


def test_ratio_2_non_conforme_a_12_pour_cent(db: Session) -> None:
    _preparer_plan_et_mapping(db)
    _poser_bilan_simple(db, fonds_propres=120_000, depots=880_000)

    resultat = evaluer_ratio(db, RATIO_2, AUJOURDHUI)

    assert resultat.valeur_ratio_pct == 12
    assert resultat.statut == STATUT_NON_CONFORME


def _poser_immobilisations_et_provisions(db: Session) -> None:
    """Après `_poser_bilan_simple(400 000, 600 000)` : participations SFD/EC 100 000 (provision
    20 000), incorporelles d'exploitation 50 000 (amortissement 10 000, provision 5 000),
    incorporelles en cours 30 000 (provision 4 000), participations hors SFD/EC 10 000
    (provision 3 000). Tout est prélevé sur la caisse : l'actif net ne change pas."""
    caisse = _id_compte(db, "101111")
    _valider_od(
        db,
        [
            LigneSaisie(_id_compte(db, "412100"), "D", 100_000),
            LigneSaisie(_id_compte(db, "441100"), "D", 50_000),
            LigneSaisie(_id_compte(db, "4311"), "D", 30_000),
            LigneSaisie(_id_compte(db, "412300"), "D", 10_000),
            LigneSaisie(caisse, "C", 190_000),
        ],
        AUJOURDHUI,
    )
    _valider_od(
        db,
        [
            LigneSaisie(caisse, "D", 42_000),
            LigneSaisie(_id_compte(db, "412910"), "C", 20_000),
            LigneSaisie(_id_compte(db, "4418"), "C", 10_000),
            LigneSaisie(_id_compte(db, "4419"), "C", 5_000),
            LigneSaisie(_id_compte(db, "4319"), "C", 4_000),
            LigneSaisie(_id_compte(db, "412930"), "C", 3_000),
        ],
        AUJOURDHUI,
    )


def test_fonds_propres_deduisent_participations_et_incorporelles_nettes(db: Session) -> None:
    """FP 400 000 - (100 000 - 20 000) participations SFD/EC - (50 000 - 10 000 - 5 000)
    incorporelles d'exploitation - (30 000 - 4 000) incorporelles en cours = 259 000. Le brut
    412300 et sa provision 412930 (autre bucket) n'y touchent pas."""
    _preparer_plan_et_mapping(db)
    _poser_bilan_simple(db, fonds_propres=400_000, depots=600_000)
    assert agregat_valeur(db, "FONDS_PROPRES", AUJOURDHUI) == 400_000

    _poser_immobilisations_et_provisions(db)

    assert agregat_valeur(db, "FONDS_PROPRES", AUJOURDHUI) == 259_000
    resultat = evaluer_ratio(db, RATIO_2, AUJOURDHUI)
    assert resultat.valeur_numerateur == 259_000  # la déduction s'applique bien au #2
    assert resultat.valeur_denominateur == 1_000_000  # actif net : prélèvements sur la caisse
    assert resultat.valeur_ratio_pct == Decimal("25.9")
    assert resultat.statut == STATUT_CONFORME


def test_propagation_aux_ratios_5_et_8_et_ressources_inchange(db: Session) -> None:
    """FONDS_PROPRES passe de 400 000 à 259 000 : le dénominateur de #5 et de #8 suit.
    RESSOURCES (dénominateur de #1) reste à 1 000 000, ainsi que #1 : il ne reçoit pas les
    déductions prudentielles."""
    _preparer_plan_et_mapping(db)
    _poser_bilan_simple(db, fonds_propres=400_000, depots=600_000)
    avant = {r.code: r for r in evaluer_tous(db, AUJOURDHUI)}
    ressources_avant = agregat_valeur(db, "RESSOURCES", AUJOURDHUI)
    assert avant[RATIO_8].valeur_denominateur == 400_000
    assert avant["RATIO_5_DIVISION_RISQUES"].valeur_denominateur == 400_000
    assert avant[RATIO_1].valeur_denominateur == ressources_avant == 1_000_000

    _poser_immobilisations_et_provisions(db)
    apres = {r.code: r for r in evaluer_tous(db, AUJOURDHUI)}

    assert apres[RATIO_8].valeur_denominateur == 259_000
    assert apres[RATIO_8].valeur_numerateur == 7_000  # 412300 10 000 - 412930 3 000
    assert apres["RATIO_5_DIVISION_RISQUES"].valeur_denominateur == 259_000
    assert agregat_valeur(db, "RESSOURCES", AUJOURDHUI) == 1_000_000
    assert apres[RATIO_1].valeur_denominateur == 1_000_000


def test_ressources_ne_recoit_aucune_deduction_prudentielle(db: Session) -> None:
    executer_seed_conformite(db)

    prefixes_ressources = {p for p, _ in _composition(db, "RESSOURCES")}
    prefixes_fonds_propres = {p for p, _ in _composition(db, "FONDS_PROPRES")}

    assert prefixes_ressources.isdisjoint(PREFIXES_DEDUCTIONS_FP)
    assert set(PREFIXES_DEDUCTIONS_FP) <= prefixes_fonds_propres
    assert ("412100", -1) in _composition(db, "FONDS_PROPRES")
    assert ("412910", 1) in _composition(db, "FONDS_PROPRES")
    for brut in ("4311", "441100"):
        assert (brut, -1) in _composition(db, "FONDS_PROPRES")
    for contra in ("4319", "4418", "4419"):
        assert (contra, 1) in _composition(db, "FONDS_PROPRES")


def test_prefixes_des_deductions_sont_disjoints_entre_eux_et_du_reste_de_fonds_propres() -> None:
    from app.cli.seed_conformite import AGREGATS

    with open(CSV_PLAN_B2, encoding="utf-8-sig", newline="") as f:
        numeros = [ligne["account_number"] for ligne in csv.DictReader(f, delimiter=";")]
    # Chaque préfixe de déduction ne ramasse QUE son propre compte (ni 412300/412930, ni un
    # autre compte de la famille).
    for prefixe in PREFIXES_DEDUCTIONS_FP:
        assert [n for n in numeros if n.startswith(prefixe)] == [prefixe]
    composition = next(a for a in AGREGATS if a.code == "FONDS_PROPRES").composition
    prefixes = [ligne.prefixe for ligne in composition]
    assert len(prefixes) == len(set(prefixes))
    for a in prefixes:
        for b in prefixes:
            if a != b:
                assert not b.startswith(a), f"{a} ramasse {b}"


def _remettre_fp_tan_et_ratio_2_a_l_etat_ancien(db: Session) -> None:
    """Base seedée AVANT P2.0-c : FP sans déductions (référence d'origine), TOTAL_ACTIF_NET
    BALANCE vide, ratio #2 inactif sans seuil."""
    from app.cli.seed_conformite import AGREGATS

    fonds_propres = _agregat_par_code(db, "FONDS_PROPRES")
    precedent = next(a for a in AGREGATS if a.code == "FONDS_PROPRES").cablage_precedent
    assert precedent is not None
    db.execute(
        text(
            "DELETE FROM conformite.agregat_compte "
            "WHERE agregat_id = :a AND prefixe_compte = ANY(:p)"
        ),
        {"a": fonds_propres.id, "p": list(PREFIXES_DEDUCTIONS_FP)},
    )
    fonds_propres.reference = precedent.reference
    total = _agregat_par_code(db, "TOTAL_ACTIF_NET")
    total.type = "BALANCE"
    total.calcul_special = None
    total.reference = None
    ratio = _ratio_par_code(db, RATIO_2)
    ratio.actif = False
    db.execute(text("DELETE FROM conformite.ratio_seuil WHERE ratio_id = :r"), {"r": ratio.id})
    db.flush()


def test_base_existante_est_recablee_pour_le_ratio_2_puis_idempotent(db: Session) -> None:
    executer_seed_conformite(db)
    _remettre_fp_tan_et_ratio_2_a_l_etat_ancien(db)
    assert len(_composition(db, "FONDS_PROPRES")) == 16

    rejeu = executer_seed_conformite(db)

    assert rejeu.agregats_recables == 2  # FONDS_PROPRES + TOTAL_ACTIF_NET
    assert rejeu.ratios_recables == 1
    assert rejeu.seuils_crees == 1
    assert len(_composition(db, "FONDS_PROPRES")) == 16 + len(PREFIXES_DEDUCTIONS_FP)
    total = _agregat_par_code(db, "TOTAL_ACTIF_NET")
    assert (total.type, total.calcul_special) == ("SPECIAL", "TOTAL_ACTIF_NET")
    assert _ratio_par_code(db, RATIO_2).actif is True
    assert _seuils(db, RATIO_2) == [(None, 15)]
    assert "nette" in (_agregat_par_code(db, "FONDS_PROPRES").reference or "")

    deuxieme = executer_seed_conformite(db)
    assert (deuxieme.agregats_recables, deuxieme.ratios_recables, deuxieme.seuils_crees) == (
        0, 0, 0,
    )


def test_garde_fonds_propres_retouche_n_est_pas_recable(db: Session) -> None:
    executer_seed_conformite(db)
    _remettre_fp_tan_et_ratio_2_a_l_etat_ancien(db)
    _agregat_par_code(db, "FONDS_PROPRES").updated_by = _utilisateur_id(db)
    db.flush()

    rejeu = executer_seed_conformite(db)

    assert rejeu.agregats_recables == 1  # TOTAL_ACTIF_NET seulement
    assert len(_composition(db, "FONDS_PROPRES")) == 16


# --- Ratio #9 : financement des immobilisations et participations (P2.0-d) ----------------------

RATIO_9 = "RATIO_9_IMMOS_PLUS_PARTICIPATIONS"
AGREGAT_IMMOS = "IMMOS_ET_PARTICIPATIONS"

COMPOSITION_IMMOS = {
    ("4311", 1), ("4319", -1),
    ("441100", 1), ("4418", -1), ("4419", -1),
    ("4321", 1), ("4329", -1),
    ("442100", 1), ("4428", -1), ("4429", -1),
    ("412300", 1), ("412930", -1),
}

# (brut, montant brut, {contra: montant}) — le net attendu est brut - somme des contras.
GROUPES_IMMOS = [
    ("4311", 20_000, {"4319": 2_000}),  # net 18 000
    ("441100", 30_000, {"4418": 6_000, "4419": 2_000}),  # net 22 000
    ("4321", 15_000, {"4329": 1_000}),  # net 14 000
    ("442100", 40_000, {"4428": 9_000, "4429": 3_000}),  # net 28 000
    ("412300", 12_000, {"412930": 2_000}),  # net 10 000
]
GROUPE_SFD_EXCLU = ("412100", 50_000, {"412910": 5_000})  # net 45 000, hors #9


def _poser_groupe_net(db: Session, brut: str, montant: int, contras: dict[str, int]) -> None:
    """Brut D, contras C, le net prélevé sur la caisse."""
    net = montant - sum(contras.values())
    lignes = [LigneSaisie(_id_compte(db, brut), "D", montant)]
    lignes += [LigneSaisie(_id_compte(db, n), "C", m) for n, m in contras.items()]
    lignes.append(LigneSaisie(_id_compte(db, "101111"), "C", net))
    _valider_od(db, lignes, AUJOURDHUI)


def test_ratio_9_est_cable_et_actif_apres_seed(db: Session) -> None:
    executer_seed_conformite(db)

    ratio = _ratio_par_code(db, RATIO_9)
    numerateur = db.get(AgregatPrudentiel, ratio.agregat_numerateur_id)
    denominateur = db.get(AgregatPrudentiel, ratio.agregat_denominateur_id)

    assert ratio.actif is True
    assert ratio.operateur == "LE"
    assert _seuils(db, RATIO_9) == [(None, 100)]
    assert "016-12-2010" in (ratio.reference_reglementaire or "")
    assert numerateur is not None and numerateur.code == AGREGAT_IMMOS
    assert denominateur is not None and denominateur.code == "FONDS_PROPRES"
    assert _composition(db, AGREGAT_IMMOS) == COMPOSITION_IMMOS
    assert not db.execute(
        select(AgregatPrudentiel).where(AgregatPrudentiel.code == "IMMOS_PLUS_PARTICIPATIONS")
    ).first()


def test_prefixes_de_l_agregat_immos_sont_disjoints_et_excluent_sfd_et_frais() -> None:
    with open(CSV_PLAN_B2, encoding="utf-8-sig", newline="") as f:
        lignes = csv.DictReader(f, delimiter=";")
        plan = {ligne["account_number"]: ligne["name"] for ligne in lignes}
    prefixes = [p for p, _ in COMPOSITION_IMMOS]

    for prefixe in prefixes:  # chaque préfixe ne ramasse que son propre compte
        assert [n for n in plan if n.startswith(prefixe)] == [prefixe]
    for a in prefixes:
        for b in prefixes:
            if a != b:
                assert not b.startswith(a), f"{a} ramasse {b}"
    # Participations SFD/établissements de crédit : exclues par le texte.
    assert not {"412100", "412910"} & set(prefixes)
    # Frais immobilisés : aucun compte du plan ne porte ce nom, et rien de ce qui est capté ne
    # ressemble à des frais ou charges à répartir.
    captes = [plan[n] for n in plan if n.startswith(tuple(prefixes))]
    assert not [nom for nom in captes if "frais" in nom.lower() or "répartir" in nom.lower()]
    assert not [n for n, nom in plan.items() if "frais immobilis" in nom.lower()]


def test_chaque_net_de_l_agregat_immos_est_brut_moins_amortissement_moins_provision(
    db: Session,
) -> None:
    _preparer_plan_et_mapping(db)
    _poser_bilan_simple(db, fonds_propres=400_000, depots=600_000)
    cumul = 0

    for brut, montant, contras in GROUPES_IMMOS:
        _poser_groupe_net(db, brut, montant, contras)
        cumul += montant - sum(contras.values())
        assert agregat_valeur(db, AGREGAT_IMMOS, AUJOURDHUI) == cumul, brut

    assert cumul == 92_000  # 18 000 + 22 000 + 14 000 + 28 000 + 10 000


def test_participations_sfd_et_etablissements_de_credit_n_entrent_pas_dans_9(db: Session) -> None:
    _preparer_plan_et_mapping(db)
    _poser_bilan_simple(db, fonds_propres=400_000, depots=600_000)
    _poser_groupe_net(db, "442100", 40_000, {"4428": 9_000, "4429": 3_000})
    avant = agregat_valeur(db, AGREGAT_IMMOS, AUJOURDHUI)
    assert avant == 28_000

    _poser_groupe_net(db, *GROUPE_SFD_EXCLU)

    assert agregat_valeur(db, AGREGAT_IMMOS, AUJOURDHUI) == avant  # 412100/412910 : hors #9


def test_ratio_9_conforme_a_80_pour_cent(db: Session) -> None:
    """Corporelles d'exploitation nettes 80 000 / fonds propres 100 000 = 80 %."""
    _preparer_plan_et_mapping(db)
    _valider_od(
        db,
        [
            LigneSaisie(_id_compte(db, "442100"), "D", 80_000),
            LigneSaisie(_id_compte(db, "101111"), "D", 20_000),
            LigneSaisie(_id_compte(db, "5521"), "C", 100_000),
        ],
        AUJOURDHUI,
    )

    resultat = evaluer_ratio(db, RATIO_9, AUJOURDHUI)

    assert resultat.valeur_numerateur == 80_000
    assert resultat.valeur_denominateur == 100_000
    assert resultat.valeur_ratio_pct == 80
    assert resultat.statut == STATUT_CONFORME


def test_ratio_9_non_conforme_a_120_pour_cent(db: Session) -> None:
    _preparer_plan_et_mapping(db)
    _valider_od(
        db,
        [
            LigneSaisie(_id_compte(db, "442100"), "D", 120_000),
            LigneSaisie(_id_compte(db, "5521"), "C", 100_000),
            LigneSaisie(_id_compte(db, "251121"), "C", 20_000),
        ],
        AUJOURDHUI,
    )

    resultat = evaluer_ratio(db, RATIO_9, AUJOURDHUI)

    assert resultat.valeur_ratio_pct == 120
    assert resultat.statut == STATUT_NON_CONFORME


def test_ratio_9_ne_fait_bouger_ni_1_ni_2_ni_5_ni_8(db: Session) -> None:
    """Scénario complet (FP 400 000, immobilisations nettes 92 000, participations SFD nettes
    45 000) : les valeurs de #1, #2, #5 et #8 sont celles que leur câblage donne, sans effet de
    #9 — qui ne modifie aucun agrégat existant."""
    _preparer_plan_et_mapping(db)
    _poser_bilan_simple(db, fonds_propres=400_000, depots=600_000)
    for brut, montant, contras in [*GROUPES_IMMOS, GROUPE_SFD_EXCLU]:
        _poser_groupe_net(db, brut, montant, contras)

    resultats = {r.code: r for r in evaluer_tous(db, AUJOURDHUI)}

    # FP = 400 000 - incorporelles nettes (18 000 + 22 000) - participations SFD nettes 45 000.
    assert resultats[RATIO_2].valeur_numerateur == 315_000
    assert resultats[RATIO_8].valeur_numerateur == 10_000  # 412300 12 000 - 412930 2 000
    assert resultats[RATIO_8].valeur_denominateur == 315_000
    assert resultats["RATIO_5_DIVISION_RISQUES"].valeur_denominateur == 315_000
    assert resultats[RATIO_1].valeur_denominateur == 1_000_000  # RESSOURCES : inchangé
    # #9 : 92 000 / 315 000 (les incorporelles nettes pèsent des deux côtés, voir la doc).
    assert resultats[RATIO_9].valeur_numerateur == 92_000
    assert resultats[RATIO_9].valeur_denominateur == 315_000
    assert resultats[RATIO_9].statut == STATUT_CONFORME


def test_base_existante_recable_le_ratio_9_puis_idempotent(db: Session) -> None:
    executer_seed_conformite(db)
    ratio = _ratio_par_code(db, RATIO_9)
    placeholder = AgregatPrudentiel(
        code="IMMOS_PLUS_PARTICIPATIONS",
        libelle="Immobilisations nettes + participations",
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

    rejeu = executer_seed_conformite(db)

    assert rejeu.ratios_recables == 1
    assert rejeu.seuils_crees == 1
    numerateur = db.get(AgregatPrudentiel, ratio.agregat_numerateur_id)
    assert numerateur is not None and numerateur.code == AGREGAT_IMMOS
    assert ratio.actif is True
    assert "016-12-2010" in (ratio.reference_reglementaire or "")
    assert _seuils(db, RATIO_9) == [(None, 100)]
    assert executer_seed_conformite(db).ratios_recables == 0

    ratio.agregat_numerateur_id = placeholder.id  # retouché par un utilisateur : on n'y touche pas
    ratio.actif = False
    ratio.updated_by = _utilisateur_id(db)
    db.execute(text("DELETE FROM conformite.ratio_seuil WHERE ratio_id = :r"), {"r": ratio.id})
    db.flush()
    assert executer_seed_conformite(db).ratios_recables == 0
    assert ratio.actif is False


# --- Ratio #1 : les titres de participation entrent dans les risques portés (P2.0-e) -----------

AGREGAT_RISQUES = "RISQUES_PORTES"
PARTICIPATIONS_AJOUTEES = {("412100", 1), ("412300", 1)}


def _poser_participations_pour_risques(db: Session) -> None:
    """Fonds propres 100 000 (5521) ; brut 412100 20 000 (SFD/EC) + 412300 30 000 (hors SFD/EC) ;
    provision 8 000 répartie 412910 3 000 + 412930 5 000 ; le tout prélevé sur la caisse."""
    caisse = _id_compte(db, "101111")
    _valider_od(
        db,
        [LigneSaisie(caisse, "D", 100_000), LigneSaisie(_id_compte(db, "5521"), "C", 100_000)],
        AUJOURDHUI,
    )
    _valider_od(
        db,
        [
            LigneSaisie(_id_compte(db, "412100"), "D", 20_000),
            LigneSaisie(_id_compte(db, "412300"), "D", 30_000),
            LigneSaisie(caisse, "C", 50_000),
        ],
        AUJOURDHUI,
    )
    _valider_od(
        db,
        [
            LigneSaisie(caisse, "D", 8_000),
            LigneSaisie(_id_compte(db, "412910"), "C", 3_000),
            LigneSaisie(_id_compte(db, "412930"), "C", 5_000),
        ],
        AUJOURDHUI,
    )


def test_risques_portes_contient_le_brut_des_deux_buckets_et_la_provision_une_seule_fois(
    db: Session,
) -> None:
    executer_seed_conformite(db)

    composition = _composition(db, AGREGAT_RISQUES)

    assert composition >= PARTICIPATIONS_AJOUTEES
    assert {("4126", 1), ("4127", 1), ("4129", -1)} <= composition  # conservés
    prefixes = [p for p, _ in composition]
    assert "412910" not in prefixes and "412930" not in prefixes  # pas de doublon de provision
    assert len(composition) == 26 + 2  # les 26 lignes d'avant + les 2 bruts


def test_prefixes_des_bruts_participations_sont_disjoints_des_provisions_et_de_4126_4127() -> None:
    with open(CSV_PLAN_B2, encoding="utf-8-sig", newline="") as f:
        numeros = [ligne["account_number"] for ligne in csv.DictReader(f, delimiter=";")]
    for brut in ("412100", "412300"):
        assert [n for n in numeros if n.startswith(brut)] == [brut]
        for autre in ("4126", "4127", "4129", "412910", "412930"):
            assert not brut.startswith(autre) and not autre.startswith(brut), (brut, autre)
    # Le préfixe 4129 (-1) capte exactement les deux sous-provisions, et aucun brut.
    assert [n for n in numeros if n.startswith("4129")] == ["4129", "412910", "412930"]


def test_les_participations_nettes_entrent_dans_les_risques_portes_une_seule_fois_la_provision(
    db: Session,
) -> None:
    """Brut 50 000 (30 000 hors SFD + 20 000 SFD), provision 8 000 -> contribution nette 42 000
    (et NON 34 000 : la provision n'est pas déduite deux fois). Avec 4126 (1 000) et 4127 (500)
    qui restent dans l'exposition : 43 500."""
    executer_seed_conformite(db)
    importer(db, str(CSV_PLAN_B2))
    assert agregat_valeur(db, AGREGAT_RISQUES, AUJOURDHUI) == 0

    _poser_participations_pour_risques(db)
    assert agregat_valeur(db, AGREGAT_RISQUES, AUJOURDHUI) == 42_000

    caisse = _id_compte(db, "101111")
    _valider_od(
        db,
        [
            LigneSaisie(_id_compte(db, "4126"), "D", 1_000),
            LigneSaisie(_id_compte(db, "4127"), "D", 500),
            LigneSaisie(caisse, "C", 1_500),
        ],
        AUJOURDHUI,
    )
    assert agregat_valeur(db, AGREGAT_RISQUES, AUJOURDHUI) == 43_500


def test_ratio_1_integre_les_participations_et_ressources_ne_bouge_pas(db: Session) -> None:
    executer_seed_conformite(db)
    importer(db, str(CSV_PLAN_B2))
    _valider_od(
        db,
        [
            LigneSaisie(_id_compte(db, "101111"), "D", 100_000),
            LigneSaisie(_id_compte(db, "5521"), "C", 100_000),
        ],
        AUJOURDHUI,
    )
    avant = evaluer_ratio(db, RATIO_1, AUJOURDHUI)
    assert (avant.valeur_numerateur, avant.valeur_denominateur) == (0, 100_000)

    # Participations : brut 50 000, provision 8 000 (le fonds propres 100 000 déjà posé).
    caisse = _id_compte(db, "101111")
    _valider_od(
        db,
        [
            LigneSaisie(_id_compte(db, "412100"), "D", 20_000),
            LigneSaisie(_id_compte(db, "412300"), "D", 30_000),
            LigneSaisie(caisse, "C", 50_000),
        ],
        AUJOURDHUI,
    )
    _valider_od(
        db,
        [
            LigneSaisie(caisse, "D", 8_000),
            LigneSaisie(_id_compte(db, "412910"), "C", 3_000),
            LigneSaisie(_id_compte(db, "412930"), "C", 5_000),
        ],
        AUJOURDHUI,
    )
    apres = evaluer_ratio(db, RATIO_1, AUJOURDHUI)

    assert apres.valeur_numerateur == 42_000  # brut des 2 buckets net de la provision totale
    assert apres.valeur_denominateur == 100_000  # RESSOURCES inchangé


def test_ratios_2_5_8_et_9_n_utilisent_pas_risques_portes_et_restent_inchanges(
    db: Session,
) -> None:
    """Mêmes participations : #2 = FP 100 000 - (20 000 - 3 000) ; #8 et #9 voient seulement le
    bucket hors SFD net (30 000 - 5 000) ; #5 a pour dénominateur les fonds propres nets."""
    executer_seed_conformite(db)
    importer(db, str(CSV_PLAN_B2))
    executer_seed_mapping_etats(db)
    _poser_participations_pour_risques(db)

    resultats = {r.code: r for r in evaluer_tous(db, AUJOURDHUI)}

    assert resultats[RATIO_2].valeur_numerateur == 83_000
    assert resultats[RATIO_8].valeur_numerateur == 25_000
    assert resultats[RATIO_8].valeur_denominateur == 83_000
    assert resultats["RATIO_5_DIVISION_RISQUES"].valeur_denominateur == 83_000
    assert resultats[RATIO_9].valeur_numerateur == 25_000
    assert _composition(db, "PARTICIPATIONS_HORS_SFD_EC") == {("412300", 1), ("412930", -1)}
    assert _composition(db, AGREGAT_IMMOS) == COMPOSITION_IMMOS


def _remettre_risques_portes_a_l_etat_ancien(db: Session) -> AgregatPrudentiel:
    agregat = _agregat_par_code(db, AGREGAT_RISQUES)
    db.execute(
        text(
            "DELETE FROM conformite.agregat_compte "
            "WHERE agregat_id = :a AND prefixe_compte IN ('412100', '412300')"
        ),
        {"a": agregat.id},
    )
    agregat.reference = None
    db.flush()
    return agregat


def test_composition_de_risques_portes_est_recablee_au_rejeu_puis_idempotente(
    db: Session,
) -> None:
    executer_seed_conformite(db)
    agregat = _remettre_risques_portes_a_l_etat_ancien(db)
    assert len(_composition(db, AGREGAT_RISQUES)) == 26

    rejeu = executer_seed_conformite(db)

    assert rejeu.agregats_recables == 1
    assert _composition(db, AGREGAT_RISQUES) >= PARTICIPATIONS_AJOUTEES
    assert len(_composition(db, AGREGAT_RISQUES)) == 28
    assert "DRS-SFD" in (agregat.reference or "")
    assert executer_seed_conformite(db).agregats_recables == 0


def test_garde_risques_portes_retouche_ou_different_n_est_pas_recable(db: Session) -> None:
    executer_seed_conformite(db)
    agregat = _remettre_risques_portes_a_l_etat_ancien(db)
    agregat.updated_by = _utilisateur_id(db)  # paramétré à la main via l'API
    db.flush()
    assert executer_seed_conformite(db).agregats_recables == 0
    assert len(_composition(db, AGREGAT_RISQUES)) == 26

    agregat.updated_by = None  # composition différente de l'ancienne : une ligne en plus
    db.add(AgregatCompte(agregat_id=agregat.id, prefixe_compte="4121", sens=1, is_system=True))
    db.flush()
    assert executer_seed_conformite(db).agregats_recables == 0
    assert len(_composition(db, AGREGAT_RISQUES)) == 27
