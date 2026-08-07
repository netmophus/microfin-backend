"""API/service Crédit CR5c — reclassification automatique (encours + provisionnement).

  - rien ne change si le palier calculé est identique à l'actuel (idempotent, aucun événement) ;
  - un crédit qui franchit un seuil est reclassé : encours déplacé vers le compte du palier,
    provision dotée sur le compte du palier ;
  - RÈGLE CENTRALE : un crédit classé en souffrance qui devient intégralement SOLDÉ redevient
    sain (delinquency_tier_id -> NULL), sa provision est reprise en ENTIER, et rien ne plante
    sur l'écriture d'encours qui n'a plus lieu d'être (montant nul, rembourser() a déjà tout
    déplacé en temps réel) ;
  - un palier terminal (is_terminal) ne se reclasse jamais automatiquement vers un palier
    meilleur — gel, radiation manuelle hors périmètre ;
  - l'endpoint d'exécution est réservé à credit.delinquency.executer (DIRECTION).
"""

import uuid
from collections.abc import Generator
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.comptabilite.models import Account
from app.modules.credit import reclassification as reclassification_module
from app.modules.credit.decaissement import decaisser
from app.modules.credit.demandes import creer_demande, decider
from app.modules.credit.models import Application, DelinquencyTier, Installment
from app.modules.credit.remboursement import rembourser
from app.modules.parameters.models import Agency
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
        {"n": f"M-CRC-{uuid.uuid4().hex[:6]}", "a": agence.id},
    ).scalar_one()
    nat = db.execute(text("SELECT id FROM parameters.countries LIMIT 1")).scalar_one()
    db.execute(
        text(
            "INSERT INTO tiers.individual_profiles "
            "(tier_id, last_name, first_name, birth_date, gender, nationality_id) "
            "VALUES (:t, 'Coulibaly', 'Fatou', '1985-01-01', 'F', :nat)"
        ),
        {"t": tier_id, "nat": nat},
    )
    return tier_id


def _produit(db: Session, *, taux_bp: int = 0) -> object:
    from app.modules.credit.models import Product

    produit = Product(
        code=f"CRC{uuid.uuid4().hex[:5]}",
        name="Crédit test reclassification",
        compte_credit_membre_id=_cid(db, "202211"),
        compte_credit_client_id=_cid(db, "202221"),
        compte_produits_interets_id=_cid(db, "7021"),
        taux_bp=taux_bp,
        methode_amortissement="capital_constant",
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


def _demande_decaissee_un_versement(
    db: Session,
    agence: Agency,
    tier_id: uuid.UUID,
    produit: object,
    *,
    montant: int = 100000,
    entry_date: date = date(2026, 1, 5),
) -> Application:
    """Un crédit à UNE SEULE échéance — isole la mécanique de reclassification du reste de
    l'échéancier, même discipline que test_credit_engagements.py."""
    demande = creer_demande(
        db, tier_id=tier_id, agency_id=agence.id, product_id=produit.id,
        montant_demande=montant, duree_echeances=1, objet="Test", par=None,
    )
    decider(db, demande, decision="approuve", montant_decide=montant, motif="OK", par=None)
    decaisser(db, demande, par=None, entry_date=entry_date)
    db.commit()
    return demande


def _palier(
    db: Session,
    code: str,
    seuil_jours: int,
    taux_provision_bp: int,
    suffixe: str,
    **overrides: object,
) -> DelinquencyTier:
    valeurs = {
        "code": code,
        "libelle": f"Palier {code}",
        "seuil_jours": seuil_jours,
        "taux_provision_bp": taux_provision_bp,
        "compte_encours_id": _compte(db, f"29{suffixe}").id,
        "compte_dotation_id": _compte(db, f"664{suffixe}").id,
        "compte_provision_id": _compte(db, f"299{suffixe}").id,
        "compte_reprise_id": _compte(db, f"764{suffixe}").id,
        "is_terminal": False,
        **overrides,
    }
    palier = DelinquencyTier(**valeurs)
    db.add(palier)
    db.flush()
    return palier


def _seule_echeance(db: Session, demande: Application) -> Installment:
    return db.execute(
        select(Installment)
        .where(Installment.application_id == demande.id)
        .order_by(Installment.numero)
        .limit(1)
    ).scalar_one()


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


# --- Rien ne change si le palier calculé est identique -----------------------------------


def test_credit_a_jour_nest_pas_reclasse(db: Session) -> None:
    agence = _agence(db, "RCA1")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande = _demande_decaissee_un_versement(db, agence, tier_id, produit)

    event = reclassification_module.reclasser_un_credit(
        db, demande, aujourdhui=date(2026, 1, 10), par=None
    )

    assert event is None
    assert demande.delinquency_tier_id is None


# --- Un crédit qui franchit un seuil est reclassé -----------------------------------------


def test_credit_en_souffrance_reclasse_encours_et_provision(db: Session) -> None:
    # seuil_jours=35 : entre les vrais paliers seedés SOUFFRANCE(30) et DOUTEUX(180) — jamais
    # en collision, et garanti d'être le palier retenu pour jours_retard=40 (base de dev
    # partagée, voir [[blast-radius-comptes-partages]]).
    agence = _agence(db, "RCA2")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    # 202221 est PARTAGÉ par tout dossier crédit non-membre de la base de dev (voir
    # [[blast-radius-comptes-partages]]) : delta AVANT/APRÈS, jamais une valeur absolue.
    solde_202221_avant = _solde_compte(db, _cid(db, "202221"))
    demande = _demande_decaissee_un_versement(db, agence, tier_id, produit, montant=100000)
    souffrance = _palier(db, "SOUFFRANCE-A", 35, 1000, "SA")  # 10%
    echeance = _seule_echeance(db, demande)

    event = reclassification_module.reclasser_un_credit(
        db, demande, aujourdhui=echeance.due_date + timedelta(days=40), par=None
    )
    db.commit()

    assert event is not None
    assert event.jours_retard == 40
    assert event.tier_avant_id is None
    assert event.tier_apres_id == souffrance.id
    assert event.encours_actuel == 100000
    assert event.montant_encours_reclasse == 100000
    assert event.provision_avant == 0
    assert event.provision_apres == 10000  # 100000 * 10%
    assert event.entry_id_encours is not None
    assert event.entry_id_dotation is not None
    assert event.entry_id_reprise is None

    demande_rechargee = db.get(Application, demande.id)
    assert demande_rechargee.delinquency_tier_id == souffrance.id

    # L'encours a quitté 202221 (client) pour le compte du palier — RETOMBE à son niveau
    # d'avant CE test (delta nul), pas forcément à zéro sur une base partagée.
    assert _solde_compte(db, _cid(db, "202221")) == solde_202221_avant
    assert _solde_compte(db, souffrance.compte_encours_id) == 100000
    # Compte de provision, normalement créditeur (contra-actif) : _solde_compte compte le
    # débit en positif, donc une provision de 10000 s'y lit -10000.
    assert _solde_compte(db, souffrance.compte_provision_id) == -10000


# --- LE test central : SOUFFRANCE -> soldé -> sain, provision reprise en entier ----------


def test_credit_souffrance_puis_solde_redevient_sain_provision_reprise(db: Session) -> None:
    agence = _agence(db, "RCA3")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    # 202221 est PARTAGÉ (voir [[blast-radius-comptes-partages]]) : delta AVANT/APRÈS.
    solde_202221_avant = _solde_compte(db, _cid(db, "202221"))
    demande = _demande_decaissee_un_versement(db, agence, tier_id, produit, montant=100000)
    souffrance = _palier(db, "SOUFFRANCE-B", 35, 1000, "SB")  # 10%
    echeance = _seule_echeance(db, demande)

    # 1) Le crédit tombe en souffrance : classé, encours déplacé, provision dotée.
    reclassification_module.reclasser_un_credit(
        db, demande, aujourdhui=echeance.due_date + timedelta(days=40), par=None
    )
    db.commit()
    demande = db.get(Application, demande.id)
    assert demande.delinquency_tier_id == souffrance.id
    assert _solde_compte(db, souffrance.compte_encours_id) == 100000

    # 2) Le client rembourse intégralement — rembourser() crédite le compte COURANT de
    #    l'encours (le palier, PAS l'ancrage 202221), donc 292SB s'apure tout seul ici.
    resultat = rembourser(db, demande, montant=100000, par=None)
    db.commit()
    assert resultat.echeance_soldee is True
    assert _solde_compte(db, souffrance.compte_encours_id) == 0  # déjà vidé par le remboursement
    # Jamais recrédité : bon compte visé — retombé à son niveau d'avant CE test.
    assert _solde_compte(db, _cid(db, "202221")) == solde_202221_avant

    # 3) Reclassification : le crédit est maintenant soldé (jours_retard=0, aucun palier ne
    #    correspond) -> redevient sain. RIEN à déplacer sur l'encours (déjà à 0), mais la
    #    provision doit être reprise en ENTIER. Aucun plantage sur un montant nul.
    event = reclassification_module.reclasser_un_credit(
        db, demande, aujourdhui=date(2026, 6, 1), par=None
    )
    db.commit()

    assert event is not None
    assert event.jours_retard == 0
    assert event.tier_avant_id == souffrance.id
    assert event.tier_apres_id is None  # sain
    assert event.encours_actuel == 0
    assert event.montant_encours_reclasse == 0
    assert event.entry_id_encours is None  # rien à poser : montant nul, sauté proprement
    assert event.provision_avant == 10000
    assert event.provision_apres == 0
    assert event.entry_id_reprise is not None  # la provision, elle, est bien reprise
    assert event.entry_id_dotation is None  # rien à doter : cible = sain

    demande_finale = db.get(Application, demande.id)
    assert demande_finale.delinquency_tier_id is None

    # La provision est retombée à zéro sur le compte du palier quitté.
    assert _solde_compte(db, souffrance.compte_provision_id) == 0


# --- Palier terminal : jamais reclassé automatiquement -----------------------------------


def test_palier_terminal_nest_jamais_reclasse_automatiquement(db: Session) -> None:
    agence = _agence(db, "RCA4")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande = _demande_decaissee_un_versement(db, agence, tier_id, produit, montant=50000)
    irrecouvrable = _palier(db, "IRRECOUVRABLE-A", 900, 10000, "IA", is_terminal=True)
    demande.delinquency_tier_id = irrecouvrable.id
    db.commit()

    # Même en repassant "à jour" (aujourd'hui proche du décaissement), le gel tient : la
    # sortie d'un palier terminal est une radiation manuelle, pas ce job.
    event = reclassification_module.reclasser_un_credit(
        db, demande, aujourdhui=date(2026, 1, 6), par=None
    )

    assert event is None
    demande_rechargee = db.get(Application, demande.id)
    assert demande_rechargee.delinquency_tier_id == irrecouvrable.id


# --- L'endpoint d'exécution -----------------------------------------------------------------


def test_endpoint_executer_refuse_sans_la_permission(db: Session, client: TestClient) -> None:
    agence = _agence(db, "RCA5")
    caissier = _entete(db, agence, "CAISSIER")  # n'a PAS credit.delinquency.executer

    reponse = client.post("/credit/delinquency/executer", headers=caissier)

    assert reponse.status_code == 403


def test_endpoint_executer_reclasse_et_rapporte(db: Session, client: TestClient) -> None:
    # L'endpoint utilise CURRENT_DATE (pas de date injectable) : l'entry_date du décaissement
    # est calculée RELATIVEMENT à aujourd'hui (pas une date en dur) pour rester ~100 jours de
    # retard quel que soit le jour réel d'exécution de la suite — entre les vrais paliers
    # seedés SOUFFRANCE(30j) et DOUTEUX(180j, comptes encore NULL sur cette base de dev : y
    # tomber ferait échouer ce dossier en RattachementManquantError, pas ce qu'on veut prouver
    # ici). Voir [[blast-radius-comptes-partages]].
    agence = _agence(db, "RCA6")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    aujourdhui = db.execute(text("SELECT CURRENT_DATE")).scalar_one()
    demande = _demande_decaissee_un_versement(
        db, agence, tier_id, produit, montant=80000, entry_date=aujourdhui - timedelta(days=130)
    )
    _palier(db, "SOUFFRANCE-C", 50, 500, "SC")
    direction = _entete(db, agence, "DIRECTION_GENERALE")

    reponse = client.post("/credit/delinquency/executer", headers=direction)

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["dossiers_evalues"] >= 1
    assert corps["reclasses"] >= 1
    assert corps["ignores_rattachement_manquant"] == []

    demande_rechargee = db.get(Application, demande.id)
    assert demande_rechargee.delinquency_tier_id is not None


# --- Aperçu (dry-run) : voir ce qui SERAIT reclassé, sans rien écrire ----------------------


def test_apercu_ne_liste_que_ce_qui_changerait_reellement(db: Session) -> None:
    """Un dossier sain (aucun palier ne s'applique) n'apparaît PAS dans l'aperçu — pas de
    bruit sur les dossiers qui n'ont rien d'utile à dire."""
    agence = _agence(db, "RCB1")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande = _demande_decaissee_un_versement(db, agence, tier_id, produit, montant=60000)
    echeance = _seule_echeance(db, demande)

    apercu = reclassification_module.previsualiser_reclassement(
        db, aujourdhui=echeance.due_date  # 0 jour de retard : sain, rien ne changerait
    )

    assert demande.application_number not in {x.application_number for x in apercu.lignes}


def test_apercu_narrete_rien_narrete_pas_decrit_exactement_ce_que_executer_ferait(
    db: Session,
) -> None:
    """Le point central de l'aperçu : mêmes chiffres que l'exécution réelle, mais RIEN écrit —
    ni palier changé, ni écriture posée, ni DelinquencyEvent créé."""
    agence = _agence(db, "RCB2")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande = _demande_decaissee_un_versement(db, agence, tier_id, produit, montant=100000)
    souffrance = _palier(db, "SOUFFRANCE-D", 35, 1000, "SD")  # 10%
    echeance = _seule_echeance(db, demande)
    aujourdhui = echeance.due_date + timedelta(days=40)

    entries_avant = db.execute(
        text("SELECT count(*) FROM comptabilite.journal_entries")
    ).scalar_one()

    apercu = reclassification_module.previsualiser_reclassement(db, aujourdhui=aujourdhui)

    ligne = next(x for x in apercu.lignes if x.application_number == demande.application_number)
    assert ligne.tier_avant_code is None
    assert ligne.tier_apres_code == "SOUFFRANCE-D"
    assert ligne.jours_retard == 40
    assert ligne.encours_actuel == 100000
    assert ligne.provision_avant == 0
    assert ligne.provision_apres == 10000
    assert ligne.rattachement_manquant is None

    # RIEN n'a été écrit : ni palier, ni pièce, ni DelinquencyEvent.
    demande_rechargee = db.get(Application, demande.id)
    assert demande_rechargee.delinquency_tier_id is None
    entries_apres = db.execute(
        text("SELECT count(*) FROM comptabilite.journal_entries")
    ).scalar_one()
    assert entries_apres == entries_avant
    aucun_evenement = db.execute(
        text(
            "SELECT 1 FROM credit.delinquency_events WHERE application_id = :a"
        ),
        {"a": demande.id},
    ).first()
    assert aucun_evenement is None
    assert _solde_compte(db, souffrance.compte_encours_id) == 0  # jamais déplacé

    # Puis l'EXÉCUTION réelle doit retomber EXACTEMENT sur les mêmes chiffres — l'aperçu ne
    # ment pas.
    event = reclassification_module.reclasser_un_credit(
        db, demande, aujourdhui=aujourdhui, par=None
    )
    db.commit()
    assert event is not None
    assert event.encours_actuel == ligne.encours_actuel
    assert event.provision_apres == ligne.provision_apres
    assert event.tier_apres_id == souffrance.id


def test_apercu_detecte_un_rattachement_manquant_sans_lever(db: Session) -> None:
    """Le palier cible n'a PAS de compte d'encours rattaché — l'aperçu le DIT (rattachement_
    manquant), il ne lève rien et n'écrit rien (contrairement à reclasser_un_credit, qui
    lèverait RattachementManquantError sur ce même dossier)."""
    agence = _agence(db, "RCB3")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande = _demande_decaissee_un_versement(db, agence, tier_id, produit, montant=70000)
    _palier(
        db, "SOUFFRANCE-E", 35, 1000, "SE", compte_encours_id=None
    )  # PAS rattaché — exprès
    echeance = _seule_echeance(db, demande)
    aujourdhui = echeance.due_date + timedelta(days=40)

    apercu = reclassification_module.previsualiser_reclassement(db, aujourdhui=aujourdhui)

    ligne = next(x for x in apercu.lignes if x.application_number == demande.application_number)
    assert ligne.rattachement_manquant is not None
    assert "compte d'encours" in ligne.rattachement_manquant
    # >= 1, pas ==1 : base de dev partagée, d'autres dossiers décaissés existent déjà et
    # peuvent eux aussi tomber sous ce même `aujourdhui` hypothétique (voir
    # [[blast-radius-comptes-partages]]) — seule LA ligne de CE dossier fait foi ci-dessus.
    assert apercu.rattachements_manquants >= 1
    assert apercu.a_reclasser >= 1  # compté quand même : c'est CE qui changerait, en théorie

    # La preuve que l'aperçu a raison : l'exécution réelle échoue bien sur CE dossier.
    with pytest.raises(reclassification_module.RattachementManquantError):
        reclassification_module.reclasser_un_credit(db, demande, aujourdhui=aujourdhui, par=None)


# --- Rapport détaillé de l'exécution : palier avant/après par dossier ----------------------


def test_rapport_execution_detaille_le_palier_avant_apres_par_dossier(db: Session) -> None:
    agence = _agence(db, "RCB4")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    demande = _demande_decaissee_un_versement(db, agence, tier_id, produit, montant=100000)
    souffrance = _palier(db, "SOUFFRANCE-F", 35, 1000, "SF")
    echeance = _seule_echeance(db, demande)

    rapport = reclassification_module.executer_reclassification(
        db, aujourdhui=echeance.due_date + timedelta(days=40), par=None
    )

    ligne = next(
        x for x in rapport.lignes if x.application_number == demande.application_number
    )
    assert ligne.tier_avant_code is None
    assert ligne.tier_avant_libelle is None
    assert ligne.tier_apres_code == "SOUFFRANCE-F"
    assert ligne.tier_apres_libelle == souffrance.libelle
    assert ligne.jours_retard == 40
    assert ligne.provision_apres == 10000


def test_endpoint_apercu_refuse_sans_la_permission(db: Session, client: TestClient) -> None:
    agence = _agence(db, "RCB5")
    caissier = _entete(db, agence, "CAISSIER")

    reponse = client.post("/credit/delinquency/apercu", headers=caissier)

    assert reponse.status_code == 403


def test_endpoint_apercu_repond_sans_rien_ecrire(db: Session, client: TestClient) -> None:
    agence = _agence(db, "RCB6")
    tier_id = _tier(db, agence)
    produit = _produit(db)
    # Date relative à CURRENT_DATE (pas de date en dur) : doit tomber dans l'exercice OUVERT,
    # quel que soit le jour réel d'exécution — même patron que test_endpoint_executer_reclasse_
    # et_rapporte.
    aujourdhui = db.execute(text("SELECT CURRENT_DATE")).scalar_one()
    demande = _demande_decaissee_un_versement(
        db, agence, tier_id, produit, montant=90000, entry_date=aujourdhui - timedelta(days=130)
    )
    _palier(db, "SOUFFRANCE-G", 45, 1000, "SG")
    direction = _entete(db, agence, "DIRECTION_GENERALE")

    reponse = client.post("/credit/delinquency/apercu", headers=direction)

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["dossiers_evalues"] >= 1
    ligne = next(
        x for x in corps["lignes"] if x["application_number"] == demande.application_number
    )
    assert ligne["tier_apres_code"] == "SOUFFRANCE-G"

    demande_rechargee = db.get(Application, demande.id)
    assert demande_rechargee.delinquency_tier_id is None  # rien écrit
