"""Crédit CR5d — prélèvement automatique des échéances (migration 0039).

- configurer_prelevement : ownership (tiers), compte actif, DAT exclu (même raison qu'au
  décaissement crédité sur compte), dossier décaissé requis, audité avant/après ;
- executer_prelevement : réutilise rembourser() en PARTIEL (capacité CR5b, pas une nouvelle
  logique de ventilation) quand le solde disponible est insuffisant — le reliquat reste dû ;
- écriture D EPARGNE / C CREDIT / C PRODUITS_INTERETS sur le journal OD (jamais la caisse) ;
- idempotence : une échéance ne peut avoir qu'UNE tentative par date_echeance
  (prelevement_tentatives, UNIQUE), mais reste retentable un autre jour ;
- CHAQUE DOSSIER COMMITTÉ SÉPARÉMENT : un rattachement manquant sur un dossier n'arrête pas
  le lot (même patron que verser_interets / executer_reclassification).
"""

import uuid
from collections.abc import Generator
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine
from app.modules.caisse.models import CaisseSession, Poste, PosteAssignation
from app.modules.caisse.service import ouvrir_session
from app.modules.credit.decaissement import decaisser
from app.modules.credit.demandes import creer_demande, decider
from app.modules.credit.models import Application, Installment, PrelevementTentative
from app.modules.credit.models import Product as CreditProduct
from app.modules.credit.prelevement import (
    CompteInvalideError,
    DemandeNonDecaisseeError,
    configurer_prelevement,
    executer_prelevement,
)
from app.modules.epargne import service as epargne_service
from app.modules.epargne.guichet import deposer
from app.modules.epargne.models import Product as EpargneProduct
from app.modules.epargne.models import SavingsAccount, SavingsMovement
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant

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


def _cid(db: Session, numero: str) -> uuid.UUID:
    return db.execute(
        text("SELECT id FROM comptabilite.accounts WHERE account_number = :n"), {"n": numero}
    ).scalar_one()


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
        {"n": f"M-PRL-{uuid.uuid4().hex[:6]}", "a": agence.id},
    ).scalar_one()
    nat = db.execute(text("SELECT id FROM parameters.countries LIMIT 1")).scalar_one()
    db.execute(
        text(
            "INSERT INTO tiers.individual_profiles "
            "(tier_id, last_name, first_name, birth_date, gender, nationality_id) "
            "VALUES (:t, 'Oumarou', 'Adamou', '1985-01-01', 'M', :nat)"
        ),
        {"t": tier_id, "nat": nat},
    )
    return tier_id


def _produit_credit(db: Session, *, avec_compte_interets: bool = True) -> CreditProduct:
    produit = CreditProduct(
        code=f"PRL{uuid.uuid4().hex[:5]}",
        name="Crédit test prélèvement",
        compte_credit_membre_id=_cid(db, "202211"),
        compte_credit_client_id=_cid(db, "202221"),
        compte_produits_interets_id=_cid(db, "7021") if avec_compte_interets else None,
        taux_bp=1200,
        methode_amortissement="capital_constant",
    )
    db.add(produit)
    db.flush()
    return produit


def _produit_epargne(db: Session, *, type_: str = "a_vue", min_balance: int = 0) -> EpargneProduct:
    produit = EpargneProduct(
        code=f"EPP{uuid.uuid4().hex[:5]}",
        name="Épargne test prélèvement",
        type=type_,
        compte_epargne_id=_cid(db, "251111"),
        compte_epargne_client_id=_cid(db, "251121"),
        min_balance=min_balance,
    )
    db.add(produit)
    db.flush()
    return produit


def _compte_epargne(
    db: Session, agence: Agency, tier_id: uuid.UUID, produit: EpargneProduct
) -> SavingsAccount:
    return epargne_service.ouvrir_compte(
        db, tier_id=tier_id, product_id=produit.id, agency_id=agence.id, par=None
    )


def _caissier(db: Session, agence: Agency) -> UtilisateurCourant:
    """Idempotent : appelé plusieurs fois par test avec LA MÊME agence (dépôt de financement
    répété) — n'ouvre qu'UNE session (Bloc C4), la réutilise sinon."""
    uid = db.execute(text("SELECT id FROM security.users LIMIT 1")).scalar_one()
    courant = UtilisateurCourant(
        user_id=uid,
        roles=("CAISSIER",),
        permissions=frozenset({"epargne.operation.deposit"}),
        primary_agency_id=agence.id,
        agency_id=agence.id,
        voit_tout=False,
    )
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
        ouvrir_session(db, courant, poste_id=poste.id, fonds_initial=0)
    return courant


def _demande_decaissee(
    db: Session,
    agence: Agency,
    tier_id: uuid.UUID,
    produit: CreditProduct,
    *,
    montant: int = 300000,
    duree_echeances: int = 12,
) -> Application:
    demande = creer_demande(
        db,
        tier_id=tier_id,
        agency_id=agence.id,
        product_id=produit.id,
        montant_demande=montant,
        duree_echeances=duree_echeances,
        objet="Test prélèvement",
        par=None,
    )
    decider(db, demande, decision="approuve", montant_decide=montant, motif="OK", par=None)
    decaisser(db, demande, par=None)
    db.commit()
    return demande


def _premiere_echeance(db: Session, demande: Application) -> Installment:
    return db.execute(
        select(Installment)
        .where(Installment.application_id == demande.id)
        .order_by(Installment.numero)
        .limit(1)
    ).scalar_one()


# --- configurer_prelevement : ownership, actif, pas DAT, dossier décaissé ------------------


def test_configurer_prelevement_ancre_le_compte_et_audite(db: Session) -> None:
    agence = _agence(db, "PRA1")
    tier_id = _tier(db, agence)
    produit_credit = _produit_credit(db)
    produit_epargne = _produit_epargne(db)
    compte = _compte_epargne(db, agence, tier_id, produit_epargne)
    demande = _demande_decaissee(db, agence, tier_id, produit_credit)

    configurer_prelevement(db, demande, compte.id, par=None)
    db.commit()

    rechargee = db.get(Application, demande.id)
    assert rechargee.compte_prelevement_id == compte.id

    audit = db.execute(
        text(
            "SELECT action, new_values FROM audit.audit_logs "
            "WHERE action = 'credit.demande.prelevement_configure' AND resource_id = :r"
        ),
        {"r": str(demande.id)},
    ).one()
    assert audit.new_values["compte_prelevement_id"] == str(compte.id)


def test_configurer_prelevement_compte_dun_autre_tiers_refuse(db: Session) -> None:
    agence = _agence(db, "PRA2")
    tier_id = _tier(db, agence)
    autre_tier_id = _tier(db, agence)
    produit_credit = _produit_credit(db)
    produit_epargne = _produit_epargne(db)
    compte_dautrui = _compte_epargne(db, agence, autre_tier_id, produit_epargne)
    demande = _demande_decaissee(db, agence, tier_id, produit_credit)

    with pytest.raises(CompteInvalideError, match="n'appartient pas"):
        configurer_prelevement(db, demande, compte_dautrui.id, par=None)


def test_configurer_prelevement_compte_ferme_refuse(db: Session) -> None:
    agence = _agence(db, "PRA3")
    tier_id = _tier(db, agence)
    produit_credit = _produit_credit(db)
    produit_epargne = _produit_epargne(db)
    compte = _compte_epargne(db, agence, tier_id, produit_epargne)
    epargne_service.cloturer_compte(db, compte, par=None)
    demande = _demande_decaissee(db, agence, tier_id, produit_credit)

    with pytest.raises(CompteInvalideError, match="fermé"):
        configurer_prelevement(db, demande, compte.id, par=None)


def test_configurer_prelevement_dat_refuse(db: Session) -> None:
    """Même raison que le décaissement crédité sur compte (charger_compte_pour_credit_externe) :
    un DAT n'a aucun mécanisme de blocage dans ce module — le prélever romprait le blocage
    prévu à terme."""
    agence = _agence(db, "PRA4")
    tier_id = _tier(db, agence)
    produit_credit = _produit_credit(db)
    produit_dat = _produit_epargne(db, type_="terme")
    compte = _compte_epargne(db, agence, tier_id, produit_dat)
    demande = _demande_decaissee(db, agence, tier_id, produit_credit)

    with pytest.raises(CompteInvalideError, match="dépôt à terme"):
        configurer_prelevement(db, demande, compte.id, par=None)


def test_configurer_prelevement_dossier_non_decaisse_refuse(db: Session) -> None:
    agence = _agence(db, "PRA5")
    tier_id = _tier(db, agence)
    produit_credit = _produit_credit(db)
    produit_epargne = _produit_epargne(db)
    compte = _compte_epargne(db, agence, tier_id, produit_epargne)
    demande = creer_demande(
        db,
        tier_id=tier_id,
        agency_id=agence.id,
        product_id=produit_credit.id,
        montant_demande=100000,
        duree_echeances=4,
        objet="Non décaissée",
        par=None,
    )
    db.commit()

    with pytest.raises(DemandeNonDecaisseeError):
        configurer_prelevement(db, demande, compte.id, par=None)


# --- executer_prelevement : paiement complet, partiel, écriture, idempotence --------------


def test_prelevement_complet_quand_le_solde_suffit(db: Session) -> None:
    agence = _agence(db, "PRB1")
    tier_id = _tier(db, agence)
    produit_credit = _produit_credit(db)
    produit_epargne = _produit_epargne(db)
    compte = _compte_epargne(db, agence, tier_id, produit_epargne)
    demande = _demande_decaissee(db, agence, tier_id, produit_credit, montant=300000)
    echeance = _premiere_echeance(db, demande)

    deposer(db, _caissier(db, agence), compte.id, echeance.total)  # largement de quoi payer
    configurer_prelevement(db, demande, compte.id, par=None)
    db.commit()

    rapport = executer_prelevement(db, date_echeance=echeance.due_date)

    assert rapport.dossiers_evalues == 1
    assert rapport.prelevements == 1
    assert rapport.total_preleve == echeance.total

    echeance_apres = db.get(Installment, echeance.id)
    assert echeance_apres.status == "paye"
    assert echeance_apres.montant_paye == echeance.total

    solde = db.execute(
        text("SELECT balance FROM epargne.accounts WHERE id = :c"), {"c": compte.id}
    ).scalar_one()
    assert solde == 0


def test_prelevement_partiel_quand_le_solde_est_insuffisant(db: Session) -> None:
    """Reproduit le cas réel CR-2026-0000001 : une échéance de 26 655 F, un compte à 20 000 F
    (min_balance=0) — prélève exactement ce qui est disponible, le reliquat reste dû. Preuve
    que le partiel réutilise CR5b (rembourser() en montant partiel), pas une logique séparée :
    l'échéance bascule 'partiellement_paye', exactement comme au guichet."""
    agence = _agence(db, "PRB2")
    tier_id = _tier(db, agence)
    produit_credit = _produit_credit(db)
    produit_epargne = _produit_epargne(db)
    compte = _compte_epargne(db, agence, tier_id, produit_epargne)
    demande = _demande_decaissee(db, agence, tier_id, produit_credit, montant=300000)
    echeance = _premiere_echeance(db, demande)
    assert echeance.total > 20000  # le cas doit être vraiment partiel

    deposer(db, _caissier(db, agence), compte.id, 20000)
    configurer_prelevement(db, demande, compte.id, par=None)
    db.commit()

    rapport = executer_prelevement(db, date_echeance=echeance.due_date)

    assert rapport.prelevements == 1
    assert rapport.total_preleve == 20000

    echeance_apres = db.get(Installment, echeance.id)
    assert echeance_apres.status == "partiellement_paye"
    assert echeance_apres.montant_paye == 20000
    reliquat = echeance.total - 20000
    assert reliquat > 0  # le reliquat reste dû — rien ne l'a effacé

    solde = db.execute(
        text("SELECT balance FROM epargne.accounts WHERE id = :c"), {"c": compte.id}
    ).scalar_one()
    assert solde == 0  # tout ce qui était disponible a été prélevé, pas plus


def test_prelevement_ecriture_debite_epargne_credite_credit_sur_journal_od(db: Session) -> None:
    agence = _agence(db, "PRB3")
    tier_id = _tier(db, agence)
    produit_credit = _produit_credit(db)
    produit_epargne = _produit_epargne(db)
    compte = _compte_epargne(db, agence, tier_id, produit_epargne)
    demande = _demande_decaissee(db, agence, tier_id, produit_credit, montant=300000)
    echeance = _premiere_echeance(db, demande)

    deposer(db, _caissier(db, agence), compte.id, echeance.total)
    configurer_prelevement(db, demande, compte.id, par=None)
    db.commit()

    executer_prelevement(db, date_echeance=echeance.due_date)

    # Description EXACTE (pas un LIKE large) : le décaissement lui-même porte aussi le numéro de
    # dossier dans sa description ("Décaissement crédit ...") — un LIKE % attraperait sa ligne.
    lignes = db.execute(
        text(
            "SELECT j.code, a.account_number, jl.side, jl.amount "
            "FROM comptabilite.journal_lines jl "
            "JOIN comptabilite.journal_entries je ON je.id = jl.entry_id "
            "JOIN comptabilite.journals j ON j.id = je.journal_id "
            "JOIN comptabilite.accounts a ON a.id = jl.account_id "
            "WHERE je.description = :d ORDER BY jl.line_number"
        ),
        {"d": f"Prélèvement automatique crédit {demande.application_number} #{echeance.numero}"},
    ).all()
    assert len(lignes) >= 2
    assert all(ligne.code == "OD" for ligne in lignes)  # jamais la caisse

    collectif_number = db.execute(
        text("SELECT account_number FROM comptabilite.accounts WHERE id = :c"),
        {"c": compte.compte_collectif_id},
    ).scalar_one()
    debit = next(ligne for ligne in lignes if ligne.side == "D")
    assert debit.account_number == collectif_number  # le collectif épargne, jamais la caisse
    assert debit.account_number != "101111"

    # Le dépôt de financement (deposer(), setup) a lui aussi posé un mouvement sur ce compte —
    # on cible spécifiquement celui du prélèvement.
    mouvement = db.execute(
        select(SavingsMovement).where(
            SavingsMovement.account_id == compte.id,
            SavingsMovement.operation_type == "prelevement_credit",
        )
    ).scalar_one()
    assert mouvement.sens == "debit"
    assert demande.application_number in mouvement.label


def test_prelevement_idempotent_meme_jour(db: Session) -> None:
    """La preuve doit être que L'IDEMPOTENCE bloque le second passage — pas l'absence de fonds :
    on dépose EXPRÈS de quoi couvrir le reliquat AVANT le second appel, et on vérifie qu'il n'y
    touche quand même pas."""
    agence = _agence(db, "PRB4")
    tier_id = _tier(db, agence)
    produit_credit = _produit_credit(db)
    produit_epargne = _produit_epargne(db)
    compte = _compte_epargne(db, agence, tier_id, produit_epargne)
    demande = _demande_decaissee(db, agence, tier_id, produit_credit, montant=300000)
    echeance = _premiere_echeance(db, demande)
    premier_depot = echeance.total - 5000  # PARTIEL exprès : l'échéance reste due après coup

    caissier = _caissier(db, agence)
    deposer(db, caissier, compte.id, premier_depot)
    configurer_prelevement(db, demande, compte.id, par=None)
    db.commit()

    premier = executer_prelevement(db, date_echeance=echeance.due_date)
    assert premier.prelevements == 1
    assert premier.total_preleve == premier_depot

    # Des fonds sont maintenant disponibles pour couvrir le reliquat — si l'idempotence ne
    # mordait pas, un second passage le prélèverait.
    deposer(db, caissier, compte.id, 5000)
    second = executer_prelevement(db, date_echeance=echeance.due_date)

    assert second.prelevements == 0
    assert second.ignores_deja_tente == 1

    echeance_apres = db.get(Installment, echeance.id)
    assert echeance_apres.montant_paye == premier_depot  # inchangé malgré les fonds disponibles

    solde = db.execute(
        text("SELECT balance FROM epargne.accounts WHERE id = :c"), {"c": compte.id}
    ).scalar_one()
    assert solde == 5000  # le second dépôt n'a PAS été touché

    tentatives = (
        db.execute(
            select(PrelevementTentative).where(PrelevementTentative.installment_id == echeance.id)
        )
        .scalars()
        .all()
    )
    assert len(tentatives) == 1


def test_prelevement_retentable_le_lendemain(db: Session) -> None:
    """Une échéance encore due reste retentable un autre jour — seule LA MÊME date est bloquée."""
    agence = _agence(db, "PRB5")
    tier_id = _tier(db, agence)
    produit_credit = _produit_credit(db)
    produit_epargne = _produit_epargne(db)
    compte = _compte_epargne(db, agence, tier_id, produit_epargne)
    demande = _demande_decaissee(db, agence, tier_id, produit_credit, montant=300000)
    echeance = _premiere_echeance(db, demande)

    configurer_prelevement(db, demande, compte.id, par=None)
    db.commit()

    jour1 = executer_prelevement(db, date_echeance=echeance.due_date)
    assert jour1.sans_disponible == 1  # rien sur le compte le premier jour

    deposer(db, _caissier(db, agence), compte.id, echeance.total)
    jour2 = executer_prelevement(db, date_echeance=echeance.due_date + timedelta(days=1))

    assert jour2.prelevements == 1
    assert jour2.total_preleve == echeance.total


def test_prelevement_echeance_pas_encore_due_ignoree(db: Session) -> None:
    agence = _agence(db, "PRB6")
    tier_id = _tier(db, agence)
    produit_credit = _produit_credit(db)
    produit_epargne = _produit_epargne(db)
    compte = _compte_epargne(db, agence, tier_id, produit_epargne)
    demande = _demande_decaissee(db, agence, tier_id, produit_credit, montant=300000)
    echeance = _premiere_echeance(db, demande)

    deposer(db, _caissier(db, agence), compte.id, echeance.total)
    configurer_prelevement(db, demande, compte.id, par=None)
    db.commit()

    rapport = executer_prelevement(db, date_echeance=echeance.due_date - timedelta(days=1))

    assert rapport.dossiers_evalues == 0  # le dossier n'est même pas sélectionné

    echeance_apres = db.get(Installment, echeance.id)
    assert echeance_apres.status == "a_echoir"


def test_prelevement_non_configure_ignore(db: Session) -> None:
    agence = _agence(db, "PRB7")
    tier_id = _tier(db, agence)
    produit_credit = _produit_credit(db)
    demande = _demande_decaissee(db, agence, tier_id, produit_credit, montant=300000)
    echeance = _premiere_echeance(db, demande)
    # PAS de configurer_prelevement : compte_prelevement_id reste NULL.

    rapport = executer_prelevement(db, date_echeance=echeance.due_date)

    assert rapport.dossiers_evalues == 0


def test_prelevement_rattachement_manquant_narrete_pas_le_lot(db: Session) -> None:
    """Un dossier sans compte de produits d'intérêts (paramétrage incomplet, part_interets > 0)
    échoue SEUL — le job continue sur le dossier suivant, sain."""
    agence = _agence(db, "PRB8")
    tier_id_casse = _tier(db, agence)
    tier_id_sain = _tier(db, agence)
    produit_casse = _produit_credit(db, avec_compte_interets=False)
    produit_sain = _produit_credit(db)
    produit_epargne = _produit_epargne(db)

    compte_casse = _compte_epargne(db, agence, tier_id_casse, produit_epargne)
    demande_cassee = _demande_decaissee(db, agence, tier_id_casse, produit_casse, montant=300000)
    echeance_cassee = _premiere_echeance(db, demande_cassee)
    assert echeance_cassee.interets > 0  # le paramétrage manquant DOIT mordre

    compte_sain = _compte_epargne(db, agence, tier_id_sain, produit_epargne)
    demande_saine = _demande_decaissee(db, agence, tier_id_sain, produit_sain, montant=300000)
    echeance_saine = _premiere_echeance(db, demande_saine)
    assert echeance_cassee.due_date == echeance_saine.due_date

    deposer(db, _caissier(db, agence), compte_casse.id, echeance_cassee.total)
    deposer(db, _caissier(db, agence), compte_sain.id, echeance_saine.total)
    configurer_prelevement(db, demande_cassee, compte_casse.id, par=None)
    configurer_prelevement(db, demande_saine, compte_sain.id, par=None)
    db.commit()

    rapport = executer_prelevement(db, date_echeance=echeance_cassee.due_date)

    assert rapport.dossiers_evalues == 2
    assert rapport.prelevements == 1
    assert demande_cassee.application_number in rapport.ignores_rattachement_manquant

    # Le dossier sain a bien été payé malgré l'échec de l'autre.
    echeance_saine_apres = db.get(Installment, echeance_saine.id)
    assert echeance_saine_apres.status == "paye"
    # Le dossier cassé n'a RIEN bougé (transaction de CE dossier rollback).
    echeance_cassee_apres = db.get(Installment, echeance_cassee.id)
    assert echeance_cassee_apres.status == "a_echoir"
