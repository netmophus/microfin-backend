"""Report des échéances de crédit sur le prochain jour ouvré — chantier P1bis, lot 4b.

REPORT À LA GÉNÉRATION (décision actée) : chaque date d'échéance théorique qui tombe un jour
NON ouvré (week-end ou férié, voir `comptabilite.calendrier`) est reportée au PROCHAIN JOUR
OUVRÉ — jour inclus — et c'est CETTE date reportée qui est stockée dans `Installment.due_date`.
Conséquence directe, vérifiée ici : la souffrance (`jours_de_retard`), qui lit `due_date` tel
quel, bénéficie du report SANS AUCUNE modification de son propre code.

AUCUNE DÉRIVE CUMULATIVE (point le plus sensible) : le report ajuste UNIQUEMENT la date
stockée de l'échéance concernée — la date THÉORIQUE (celle qu'aurait l'échéance si le
calendrier n'existait pas) reste seule base du calcul de l'échéance suivante. `_ajouter_periode`
continue de chaîner sur les dates théoriques, jamais sur les dates reportées.

AUCUN IMPACT SUR LES MONTANTS : le calcul périodique des intérêts (`echeancier.py`) est
proportionnel — taux annuel / nombre de périodes FIXE, jamais un prorata sur le nombre réel de
jours (`base_jours` du produit n'intervient PAS ici, voir le docstring de ce module) — un
report de quelques jours ne change donc jamais le montant d'une échéance.

Dates de test : septembre 2032 à septembre 2033, loin de toute donnée réelle.
"""

import uuid
from collections.abc import Generator
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import engine
from app.modules.caisse.models import Poste, PosteAssignation
from app.modules.caisse.service import ouvrir_session
from app.modules.comptabilite import calendrier, journee
from app.modules.comptabilite.models import Exercice
from app.modules.credit.decaissement import _dater_echeances, decaisser
from app.modules.credit.demandes import creer_demande, decider
from app.modules.credit.echeancier import Echeance
from app.modules.credit.models import Installment, Product
from app.modules.credit.reclassification import jours_de_retard
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant

pytestmark = pytest.mark.integration

# Le 10 de chaque mois, septembre 2032 -> septembre 2033 : 2032-10-10 (dimanche) et
# 2033-04-10 (dimanche) tombent un jour non ouvré — vérifié indépendamment (date.weekday()).
DEPART = date(2032, 9, 10)


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


def _cid(db: Session, numero: str) -> uuid.UUID:
    return db.execute(
        text("SELECT id FROM comptabilite.accounts WHERE account_number = :n"), {"n": numero}
    ).scalar_one()


def _echeance(numero: int) -> Echeance:
    return Echeance(numero=numero, capital=10_000, interets=0, total=10_000, capital_restant_du=0)


# --- _dater_echeances, directement : le report ponctuel -----------------------------------------


def test_echeance_theorique_un_dimanche_reportee_au_lundi(db: Session) -> None:
    [(_, due)] = _dater_echeances(db, [_echeance(1)], DEPART, "mensuelle")
    assert date(2032, 10, 10).weekday() == 6  # dimanche, prémisse du test
    assert due == date(2032, 10, 11)  # lundi suivant


def test_echeance_theorique_un_jour_ferie_reportee(db: Session) -> None:
    # Théorique du mois 2 (novembre) = 2032-11-10, un mercredi ordinaire : on le rend férié.
    calendrier.ajouter_jour_ferie(db, date(2032, 11, 10), "Férié de test", None)
    [_, due1], [_, due2] = _dater_echeances(db, [_echeance(1), _echeance(2)], DEPART, "mensuelle")
    assert due1 == date(2032, 10, 11)  # échéance 1 : dimanche -> lundi (inchangé)
    assert due2 == date(2032, 11, 11)  # échéance 2 : jeudi, jour ouvré suivant le férié


# --- Aucune dérive cumulative sur 12 échéances mensuelles ---------------------------------------


def test_aucune_derive_cumulative_sur_douze_echeances_mensuelles(db: Session) -> None:
    """Deux échéances sur les douze tombent un dimanche (théoriques #1 et #7) — chacune est
    reportée INDÉPENDAMMENT, sans jamais décaler la base théorique des échéances suivantes :
    toutes les autres restent exactement ancrées sur le 10 du mois."""
    echeances = [_echeance(n) for n in range(1, 13)]
    resultat = _dater_echeances(db, echeances, DEPART, "mensuelle")
    dates = [d for _, d in resultat]
    attendu = [
        date(2032, 10, 11),  # 1 : dimanche 10 -> lundi 11
        date(2032, 11, 10),  # 2 : mercredi, inchangé
        date(2032, 12, 10),  # 3 : vendredi, inchangé
        date(2033, 1, 10),  # 4 : lundi, inchangé
        date(2033, 2, 10),  # 5 : jeudi, inchangé
        date(2033, 3, 10),  # 6 : jeudi, inchangé
        date(2033, 4, 11),  # 7 : dimanche 10 -> lundi 11
        date(2033, 5, 10),  # 8 : mardi, inchangé
        date(2033, 6, 10),  # 9 : vendredi, inchangé
        date(2033, 7, 11),  # 10 : dimanche 10 -> lundi 11
        date(2033, 8, 10),  # 11 : mercredi, inchangé
        date(2033, 9, 12),  # 12 : samedi 10 -> lundi 12 (saute DEUX jours, pas un seul)
    ]
    assert dates == attendu


# --- Intégration bout en bout via decaisser() ----------------------------------------------------


def _tier(db: Session, agence: Agency, suffixe: str) -> uuid.UUID:
    tier_id = db.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES (:n, 'individual', :a, 'actif') RETURNING id"
        ),
        {"n": f"M-J4B-{suffixe}", "a": agence.id},
    ).scalar_one()
    nat = db.execute(text("SELECT id FROM parameters.countries LIMIT 1")).scalar_one()
    db.execute(
        text(
            "INSERT INTO tiers.individual_profiles "
            "(tier_id, last_name, first_name, birth_date, gender, nationality_id) "
            "VALUES (:t, 'Kone', 'Fatou', '1985-01-01', 'F', :nat)"
        ),
        {"t": tier_id, "nat": nat},
    )
    return tier_id


def test_decaissement_reel_stocke_la_date_reportee_et_la_souffrance_la_lit(db: Session) -> None:
    """Bout en bout : un décaissement dont la première échéance théorique tombe un dimanche
    stocke bien le LUNDI suivant — et la souffrance, qui ne lit QUE `due_date`, calcule le
    retard depuis CETTE date reportée, jamais depuis le dimanche théorique (sinon le mardi
    suivant la vraie échéance afficherait 2 jours de retard au lieu d'1)."""
    journee.ouvrir_journee(db, DEPART, None)
    db.add(
        Exercice(
            code="J4B", label="Exercice J4B",
            date_debut=date(2032, 1, 1), date_fin=date(2033, 12, 31),
        )
    )

    agence = Agency(code="J4B", name="Agence J4B", compte_caisse_id=_cid(db, "101111"))
    db.add(agence)
    db.flush()
    tier_id = _tier(db, agence, "D1")
    produit = Product(
        code="J4B", name="Crédit test", compte_credit_membre_id=_cid(db, "202211"),
        compte_credit_client_id=_cid(db, "202221"), taux_bp=0, periodicite="mensuelle",
    )
    db.add(produit)
    db.flush()
    demande = creer_demande(
        db, tier_id=tier_id, agency_id=agence.id, product_id=produit.id,
        montant_demande=120_000, duree_echeances=2, objet="Test lot4b", par=None,
    )
    decider(db, demande, decision="approuve", montant_decide=120_000, motif="OK", par=None)

    uid = db.execute(text("SELECT id FROM security.users LIMIT 1")).scalar_one()
    poste = Poste(
        agency_id=agence.id, code="01", libelle="Caisse test",
        compte_caisse_id=agence.compte_caisse_id,
    )
    db.add(poste)
    db.flush()
    db.add(PosteAssignation(poste_id=poste.id, user_id=uid))
    db.flush()
    ouvrir_session(
        db,
        UtilisateurCourant(
            user_id=uid, roles=(), permissions=frozenset(),
            primary_agency_id=agence.id, agency_id=agence.id, voit_tout=True,
        ),
        poste_id=poste.id, fonds_initial=0,
    )

    decaisser(db, demande, par=uid)

    premiere = (
        db.query(Installment)
        .filter_by(application_id=demande.id, numero=1)
        .one()
    )
    assert premiere.due_date == date(2032, 10, 11)  # reportée, pas le dimanche théorique

    # Sur l'échéance elle-même (lundi) : pas encore en retard.
    assert jours_de_retard(db, demande.id, aujourdhui=date(2032, 10, 11)) == 0
    # UN jour après la date RÉELLEMENT due (mardi) : 1 jour de retard — PAS 2, ce qui serait le
    # cas si le calcul se basait encore sur le dimanche théorique.
    assert jours_de_retard(db, demande.id, aujourdhui=date(2032, 10, 12)) == 1


def test_echeance_non_encore_due_un_dimanche_nest_pas_comptee_en_retard(db: Session) -> None:
    """Le dimanche théorique lui-même (avant le report au lundi) : l'échéance n'est pas encore
    due, donc pas en retard — triviallement vrai dans les deux cas, mais garde-fou explicite
    demandé pour ce lot."""
    journee.ouvrir_journee(db, DEPART, None)
    db.add(
        Exercice(
            code="J4B2", label="Exercice J4B2",
            date_debut=date(2032, 1, 1), date_fin=date(2033, 12, 31),
        )
    )

    agence = Agency(code="J4B2", name="Agence J4B2", compte_caisse_id=_cid(db, "101111"))
    db.add(agence)
    db.flush()
    tier_id = _tier(db, agence, "D2")
    produit = Product(
        code="J4B2", name="Crédit test", compte_credit_membre_id=_cid(db, "202211"),
        compte_credit_client_id=_cid(db, "202221"), taux_bp=0, periodicite="mensuelle",
    )
    db.add(produit)
    db.flush()
    demande = creer_demande(
        db, tier_id=tier_id, agency_id=agence.id, product_id=produit.id,
        montant_demande=120_000, duree_echeances=2, objet="Test lot4b", par=None,
    )
    decider(db, demande, decision="approuve", montant_decide=120_000, motif="OK", par=None)

    uid = db.execute(text("SELECT id FROM security.users LIMIT 1")).scalar_one()
    poste = Poste(
        agency_id=agence.id, code="01", libelle="Caisse test",
        compte_caisse_id=agence.compte_caisse_id,
    )
    db.add(poste)
    db.flush()
    db.add(PosteAssignation(poste_id=poste.id, user_id=uid))
    db.flush()
    ouvrir_session(
        db,
        UtilisateurCourant(
            user_id=uid, roles=(), permissions=frozenset(),
            primary_agency_id=agence.id, agency_id=agence.id, voit_tout=True,
        ),
        poste_id=poste.id, fonds_initial=0,
    )

    decaisser(db, demande, par=uid)

    assert jours_de_retard(db, demande.id, aujourdhui=date(2032, 10, 10)) == 0
