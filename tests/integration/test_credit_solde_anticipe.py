"""Crédit — solde anticipé (clôture totale avant terme), chantier remboursement anticipé
lot B.

  - capital restant dû (encours_actuel) + intérêts courus (prorata jour-par-jour) exactement,
    AUCUNE pénalité, intérêts des échéances futures ANNULÉS ;
  - les `Installment` futures ne sont JAMAIS touchées (le plan reste un témoin historique) ;
  - date de référence du prorata : due_date de la dernière échéance 'paye', sinon decaissement ;
  - conséquence GRATUITE : status='solde' coupe rembourser() ET la reclassification, sans
    modifier ces deux fichiers ;
  - refuse si non décaissé, déjà soldé, ou échéance courante déjà partiellement payée (v1).
"""

import uuid
from collections.abc import Generator
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.caisse.models import CaisseSession, Poste, PosteAssignation
from app.modules.caisse.service import ouvrir_session
from app.modules.comptabilite.models import Account
from app.modules.credit.decaissement import RattachementManquantError, decaisser
from app.modules.credit.demandes import creer_demande, decider
from app.modules.credit.models import Application, Installment, Product
from app.modules.credit.reclassification import executer_reclassification
from app.modules.credit.remboursement import (
    AucuneEcheanceAReglerError,
    EcheanceEnCoursDejaVerseeError,
    rembourser,
    solder_par_anticipation,
)
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant
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


def _cid(db: Session, numero: str) -> uuid.UUID:
    return db.execute(
        text("SELECT id FROM comptabilite.accounts WHERE account_number = :n"), {"n": numero}
    ).scalar_one()


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


def _agence(db: Session, code: str) -> Agency:
    agence = Agency(code=code, name=f"Agence {code}", compte_caisse_id=_cid(db, "101111"))
    db.add(agence)
    db.flush()
    return agence


def _tier(db: Session, agence: Agency) -> uuid.UUID:
    tier_id = db.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES (:n, 'individual', :a, 'actif') RETURNING id"
        ),
        {"n": f"M-CSA-{uuid.uuid4().hex[:6]}", "a": agence.id},
    ).scalar_one()
    nat = db.execute(text("SELECT id FROM parameters.countries LIMIT 1")).scalar_one()
    db.execute(
        text(
            "INSERT INTO tiers.individual_profiles "
            "(tier_id, last_name, first_name, birth_date, gender, nationality_id) "
            "VALUES (:t, 'Toure', 'Aminata', '1985-01-01', 'F', :nat)"
        ),
        {"t": tier_id, "nat": nat},
    )
    return tier_id


def _produit(db: Session, *, taux_bp: int = 1200, sans_compte_interets: bool = False) -> Product:
    produit = Product(
        code=f"CSA{uuid.uuid4().hex[:5]}",
        name="Crédit test solde anticipé",
        compte_credit_membre_id=_cid(db, "202211"),
        compte_credit_client_id=_cid(db, "202221"),
        compte_produits_interets_id=None if sans_compte_interets else _cid(db, "7021"),
        taux_bp=taux_bp,
        methode_amortissement="capital_constant",
        periodicite="mensuelle",
    )
    db.add(produit)
    db.flush()
    return produit


def _entete(db: Session, agence: Agency, role_code: str) -> dict[str, str]:
    role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
    suffixe = uuid.uuid4().hex[:8]
    user = User(
        matricule=f"MAT-{suffixe}", email=f"{suffixe}@ex.com", username=f"u{suffixe}",
        password_hash=hasher_mot_de_passe("Motdepasse!123"), last_name="T", first_name="A",
        primary_agency_id=agence.id,
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.flush()
    jeton = creer_access_token(
        user_id=user.id, roles=[role_code], primary_agency_id=agence.id, agency_id=agence.id
    )
    return {"Authorization": f"Bearer {jeton}"}


def _ouvrir_session_caisse(db: Session, agence: Agency) -> uuid.UUID:
    uid = db.execute(text("SELECT id FROM security.users LIMIT 1")).scalar_one()
    deja_ouverte = db.execute(
        select(CaisseSession.id).where(
            CaisseSession.caissier_id == uid, CaisseSession.status == "ouverte"
        )
    ).first()
    if deja_ouverte is None:
        poste = Poste(
            agency_id=agence.id, code="01", libelle="Caisse principale",
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
    return uid


def _credit_decaisse(
    db: Session,
    agence: Agency,
    tier_id: uuid.UUID,
    produit: Product,
    *,
    montant: int = 300_000,
    duree_echeances: int = 3,
    entry_date: date = date(2026, 1, 5),
) -> Application:
    demande = creer_demande(
        db, tier_id=tier_id, agency_id=agence.id, product_id=produit.id,
        montant_demande=montant, duree_echeances=duree_echeances, objet="Test", par=None,
    )
    decider(db, demande, decision="approuve", montant_decide=montant, motif="OK", par=None)
    par = _ouvrir_session_caisse(db, agence)
    decaisser(db, demande, par=par, entry_date=entry_date)
    # `decaisser()` fixe `disbursed_at` à NOW() (horloge murale), indépendant de `entry_date`
    # (date COMPTABLE). En production les deux coïncident quasi toujours (entry_date par
    # défaut = aujourd'hui) ; ici, pour un test déterministe avec une date antérieure, on
    # aligne explicitement `disbursed_at` — c'est lui que `solder_par_anticipation` utilise
    # comme référence du prorata tant qu'aucune échéance n'est encore 'paye'.
    demande.disbursed_at = datetime(
        entry_date.year, entry_date.month, entry_date.day, tzinfo=UTC
    )
    db.commit()
    return demande


def _installments(db: Session, demande: Application) -> list[Installment]:
    return list(
        db.execute(
            select(Installment)
            .where(Installment.application_id == demande.id)
            .order_by(Installment.numero)
        ).scalars()
    )


def _solde_compte(db: Session, account_id: uuid.UUID) -> int:
    lignes = db.execute(
        text(
            "SELECT side, amount FROM comptabilite.journal_lines "
            "JOIN comptabilite.journal_entries je ON je.id = entry_id "
            "WHERE account_id = :a AND je.status = 'validee'"
        ),
        {"a": account_id},
    ).all()
    return sum(m if s == "D" else -m for s, m in lignes)


# --- Capital + intérêts courus, écriture, statut ------------------------------------------


def test_solde_capital_et_interets_courus_depuis_decaissement(db: Session) -> None:
    """300 000 F, 12 %/an, rien versé, soldé 15 jours après décaissement :
    capital = 300 000 F (rien versé) ; intérêts = 300 000 x 0,12 x 15/360 = 1 500 F."""
    agence = _agence(db, "CSA1")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande = _credit_decaisse(db, agence, tier_id, produit, entry_date=date(2026, 1, 5))
    par = _ouvrir_session_caisse(db, agence)

    caisse_id = agence.compte_caisse_id
    assert caisse_id is not None
    compte_interets_id = produit.compte_produits_interets_id
    assert compte_interets_id is not None
    # compte_credit_id est l'ANCRAGE RÉSOLU (membre OU client selon le tiers, decaissement.py)
    # — c'est lui, jamais deviné entre compte_credit_membre_id/compte_credit_client_id.
    compte_credit_id = demande.compte_credit_id
    assert compte_credit_id is not None

    # DELTAS, pas des valeurs absolues : "101111"/"202211"/"202221"/"7021" sont des comptes
    # RÉELS du plan RCSFD, partagés avec le reste de la base de dev — seule la VARIATION
    # introduite par ce test est significative (même discipline qu'ailleurs dans ce projet).
    avant_caisse = _solde_compte(db, caisse_id)
    avant_credit = _solde_compte(db, compte_credit_id)
    avant_interets = _solde_compte(db, compte_interets_id)

    resultat = solder_par_anticipation(
        db, demande, par=par, entry_date=date(2026, 1, 20)
    )

    assert resultat.capital_regle == 300_000
    assert resultat.interets_courus == 1_500
    assert resultat.montant_total == 301_500
    assert resultat.jours_courus == 15

    assert demande.status == "solde"
    assert demande.solde_at is not None
    assert demande.solde_by == par

    # D caisse (301 500) / C compte crédit (300 000) / C intérêts (1 500).
    assert _solde_compte(db, caisse_id) - avant_caisse == 301_500
    assert _solde_compte(db, compte_credit_id) - avant_credit == -300_000
    assert _solde_compte(db, compte_interets_id) - avant_interets == -1_500


def test_installments_futures_jamais_touchees(db: Session) -> None:
    """Le plan reste intact : toutes les échéances restent 'a_echoir', montant_paye=0 — la
    SEULE trace du solde anticipé est le statut de la demande."""
    agence = _agence(db, "CSA2")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande = _credit_decaisse(db, agence, tier_id, produit)
    par = _ouvrir_session_caisse(db, agence)

    solder_par_anticipation(db, demande, par=par, entry_date=date(2026, 1, 20))

    for echeance in _installments(db, demande):
        assert echeance.status == "a_echoir"
        assert echeance.montant_paye == 0


def test_reference_devient_due_date_apres_versement_complet(db: Session) -> None:
    """Échéance #1 (100 000 capital + 3 000 intérêts = 103 000) soldée normalement le
    05/02 -> devient la date de référence. Solde anticipé le 20/02 (15 jours après) :
    capital restant = 200 000 F ; intérêts = 200 000 x 0,12 x 15/360 = 1 000 F."""
    agence = _agence(db, "CSA3")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande = _credit_decaisse(db, agence, tier_id, produit, entry_date=date(2026, 1, 5))
    par = _ouvrir_session_caisse(db, agence)

    rembourser(db, demande, montant=103_000, par=par, entry_date=date(2026, 2, 5))

    resultat = solder_par_anticipation(db, demande, par=par, entry_date=date(2026, 2, 20))

    assert resultat.capital_regle == 200_000
    assert resultat.jours_courus == 15
    assert resultat.interets_courus == 1_000


# --- Refus ----------------------------------------------------------------------------


def test_refuse_si_non_decaisse(db: Session) -> None:
    agence = _agence(db, "CSA4")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande = creer_demande(
        db, tier_id=tier_id, agency_id=agence.id, product_id=produit.id,
        montant_demande=300_000, duree_echeances=3, objet="Test", par=None,
    )
    par = _ouvrir_session_caisse(db, agence)

    with pytest.raises(AucuneEcheanceAReglerError):
        solder_par_anticipation(db, demande, par=par)


def test_refuse_si_deja_solde(db: Session) -> None:
    agence = _agence(db, "CSA5")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande = _credit_decaisse(db, agence, tier_id, produit)
    par = _ouvrir_session_caisse(db, agence)
    solder_par_anticipation(db, demande, par=par, entry_date=date(2026, 1, 20))

    with pytest.raises(AucuneEcheanceAReglerError):
        solder_par_anticipation(db, demande, par=par, entry_date=date(2026, 1, 21))


def test_refuse_si_echeance_courante_deja_partiellement_payee(db: Session) -> None:
    agence = _agence(db, "CSA6")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande = _credit_decaisse(db, agence, tier_id, produit, entry_date=date(2026, 1, 5))
    par = _ouvrir_session_caisse(db, agence)

    rembourser(db, demande, montant=10_000, par=par, entry_date=date(2026, 1, 10))

    with pytest.raises(EcheanceEnCoursDejaVerseeError):
        solder_par_anticipation(db, demande, par=par, entry_date=date(2026, 1, 20))


def test_refuse_si_taux_non_nul_sans_compte_interets_rattache(db: Session) -> None:
    """Intérêts courus > 0 mais compte de produits d'intérêts absent -> refus explicite,
    jamais une écriture déséquilibrée silencieuse."""
    agence = _agence(db, "CSA7")
    tier_id = _tier(db, agence)
    produit = _produit(db, sans_compte_interets=True)
    demande = _credit_decaisse(db, agence, tier_id, produit, entry_date=date(2026, 1, 5))
    par = _ouvrir_session_caisse(db, agence)

    with pytest.raises(RattachementManquantError):
        solder_par_anticipation(db, demande, par=par, entry_date=date(2026, 1, 20))


# --- Conséquences gratuites (status='solde' coupe les deux autres chemins) ----------------


def test_rembourser_refuse_apres_solde_anticipe(db: Session) -> None:
    agence = _agence(db, "CSA8")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande = _credit_decaisse(db, agence, tier_id, produit)
    par = _ouvrir_session_caisse(db, agence)
    solder_par_anticipation(db, demande, par=par, entry_date=date(2026, 1, 20))

    with pytest.raises(AucuneEcheanceAReglerError):
        rembourser(db, demande, montant=1, par=par)


def test_reclassification_ignore_un_credit_solde(db: Session) -> None:
    agence = _agence(db, "CSA9")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande = _credit_decaisse(db, agence, tier_id, produit)
    par = _ouvrir_session_caisse(db, agence)
    solder_par_anticipation(db, demande, par=par, entry_date=date(2026, 1, 20))

    rapport = executer_reclassification(db, aujourdhui=date(2026, 6, 1), par=None)

    assert demande.application_number not in [
        ligne.application_number for ligne in rapport.lignes
    ]
