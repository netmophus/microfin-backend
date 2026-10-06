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

import uuid
from collections.abc import Generator
from datetime import date

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
from app.modules.conformite.models import AgregatPrudentiel, RatioPrudentiel
from app.modules.conformite.moteur import (
    AVERT_FONDS_PROPRES_NULS,
    AVERT_NUMERATEUR_NUL,
    STATUT_CONFORME,
    STATUT_NON_CALCULABLE,
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
    """Base sans aucune activité réelle (juste le seed) : les 2 ratios actifs doivent
    renvoyer un résultat — NON_CALCULABLE est attendu (dénominateurs nuls), jamais une
    exception, jamais un ratio inactif dans la liste."""
    executer_seed_conformite(db)

    resultats = evaluer_tous(db, AUJOURDHUI)

    assert [r.code for r in resultats] == [
        "RATIO_1_COUVERTURE_RISQUES",
        "RATIO_5_DIVISION_RISQUES",
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
    assert all(r.reference_reglementaire == REFERENCE_REGLEMENTAIRE for r in ratios)
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
    10 références + 8 libellés de ratios + 11 libellés d'agrégats resynchronisés — une seule
    fois."""
    executer_seed_conformite(db)
    _revenir_a_l_etat_ancien(db)

    rejeu = executer_seed_conformite(db)

    assert rejeu.references_resynchronisees == 10
    assert rejeu.libelles_resynchronises == 8 + 11
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
    # Les 9 autres ratios et 10 autres agrégats, eux, n'ont pas été retouchés.
    assert rejeu.references_resynchronisees == 9
    assert rejeu.libelles_resynchronises == 7 + 10


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
