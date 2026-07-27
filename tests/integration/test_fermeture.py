"""Fermeture de compte (E4) — chaque garde-fou VU MORDRE, et LA BOUCLE complète Épargne↔Tiers.

  - solde > 0 : restitution (D 3111 / C 5721) + état fermé, en une transaction ;
  - solde = 0 : fermeture directe, aucune écriture ;
  - solde < 0 : refusée (débiteur) ;
  - compte fermé : aucune opération (garde-fou de E3 réutilisé) ;
  - compte fermé : non réouvrable, prouvé au niveau BASE (trigger 0022) ;
  - atomicité : panne au milieu -> rien ;
  - LA BOUCLE : membre à compte ouvert non désactivable -> on ferme -> désactivable.
"""

import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, InternalError
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, engine
from app.modules.audit.service import CONTEXTE_VIDE
from app.modules.epargne import service
from app.modules.epargne.engagements import enregistrer
from app.modules.epargne.guichet import (
    CompteClotureError,
    CompteIntrouvableError,
    deposer,
    fermer_compte,
)
from app.modules.epargne.models import Product, SavingsAccount
from app.modules.epargne.service import CompteDebiteurError
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.tiers.cycle_de_vie import EngagementsOuvertsError, executer_transition

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _brancher_verificateur() -> None:
    enregistrer()  # pour la boucle : la désactivation doit voir l'épargne


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


def _acteur(db: Session, agency_id: uuid.UUID, *, voit_tout: bool = True) -> UtilisateurCourant:
    uid = db.execute(text("SELECT id FROM security.users LIMIT 1")).scalar_one()
    return UtilisateurCourant(
        user_id=uid,
        roles=("RESPONSABLE_AGENCE",),
        permissions=frozenset(
            {"epargne.account.close", "epargne.operation.deposit", "tiers.deactivate"}
        ),
        primary_agency_id=agency_id,
        agency_id=agency_id,
        voit_tout=voit_tout,
    )


def _membre_actif(db: Session, agency_id: uuid.UUID, suffixe: str) -> uuid.UUID:
    """Membre actif BIEN FORMÉ (parent + profil), pour supporter la désactivation polymorphe."""
    tier_id = db.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES (:num, 'individual', :ag, 'actif') RETURNING id"
        ),
        {"num": f"M-FERM-{suffixe}", "ag": agency_id},
    ).scalar_one()
    nat = db.execute(text("SELECT id FROM parameters.countries LIMIT 1")).scalar_one()
    db.execute(
        text(
            "INSERT INTO tiers.individual_profiles "
            "(tier_id, last_name, first_name, birth_date, gender, nationality_id) "
            "VALUES (:t, 'Test', 'Membre', '1990-01-01', 'M', :nat)"
        ),
        {"t": tier_id, "nat": nat},
    )
    return tier_id


def _cadre(db: Session, suffixe: str) -> tuple[SavingsAccount, UtilisateurCourant]:
    agence = db.execute(
        text(
            "INSERT INTO parameters.agencies (code, name, compte_caisse_id) "
            "VALUES (:c, 'Agence', :caisse) RETURNING id"
        ),
        {"c": f"AGF-{suffixe}", "caisse": _cid(db, "5721")},
    ).scalar_one()
    produit = Product(
        code=f"PF{suffixe}", name="Épargne", type="a_vue", compte_epargne_id=_cid(db, "3111")
    )
    db.add(produit)
    db.flush()
    tier_id = _membre_actif(db, agence, suffixe)
    compte = service.ouvrir_compte(
        db, tier_id=tier_id, product_id=produit.id, agency_id=agence, par=None
    )
    return compte, _acteur(db, agence)


# --- Les trois cas de solde ---------------------------------------------------------


def test_fermeture_avec_solde_restitue_et_ferme(db: Session) -> None:
    compte, resp = _cadre(db, "R1")
    deposer(db, resp, compte.id, 5000)

    fermer_compte(db, resp, compte.id)

    db.refresh(compte)
    assert compte.status == "cloture"
    assert compte.balance == 0
    # un mouvement de clôture (debit) relié à une pièce de restitution D 3111 / C 5721
    mvt = db.execute(
        text(
            "SELECT operation_type, sens, amount FROM epargne.movements "
            "WHERE account_id = :a AND operation_type = 'cloture'"
        ),
        {"a": compte.id},
    ).one()
    assert mvt == ("cloture", "debit", 5000)


def test_fermeture_solde_nul_sans_ecriture(db: Session) -> None:
    compte, resp = _cadre(db, "R2")
    avant = db.execute(text("SELECT count(*) FROM comptabilite.journal_entries")).scalar_one()

    fermer_compte(db, resp, compte.id)

    db.refresh(compte)
    assert compte.status == "cloture"
    # aucune écriture : rien ne bouge sur un compte déjà vide
    apres = db.execute(text("SELECT count(*) FROM comptabilite.journal_entries")).scalar_one()
    assert apres == avant


def test_fermeture_solde_negatif_refusee(db: Session) -> None:
    compte, resp = _cadre(db, "R3")
    db.execute(
        text("UPDATE epargne.accounts SET balance = -100 WHERE id = :a"), {"a": compte.id}
    )
    with pytest.raises(CompteDebiteurError):
        fermer_compte(db, resp, compte.id)


# --- Fermé : aucune opération, non réouvrable ---------------------------------------


def test_compte_ferme_aucune_operation(db: Session) -> None:
    compte, resp = _cadre(db, "O1")
    fermer_compte(db, resp, compte.id)
    with pytest.raises(CompteClotureError):
        deposer(db, resp, compte.id, 1000)


def test_compte_ferme_non_reouvrable_par_la_base(db: Session) -> None:
    compte, resp = _cadre(db, "O2")
    fermer_compte(db, resp, compte.id)
    # SQL brut : tenter la réouverture cloture -> actif ; le trigger 0022 doit mordre.
    with pytest.raises((IntegrityError, InternalError)) as exc:
        db.execute(
            text("UPDATE epargne.accounts SET status = 'actif' WHERE id = :a"), {"a": compte.id}
        )
    assert "reouvrable" in str(exc.value).lower() or "definitif" in str(exc.value).lower()


def test_fermeture_hors_perimetre_est_introuvable(db: Session) -> None:
    compte, _resp = _cadre(db, "C1")
    autre = db.execute(
        text("INSERT INTO parameters.agencies (code, name) VALUES ('AGF-X', 'X') RETURNING id")
    ).scalar_one()
    resp_autre = _acteur(db, autre, voit_tout=False)
    with pytest.raises(CompteIntrouvableError):
        fermer_compte(db, resp_autre, compte.id)


# --- LA BOUCLE COMPLÈTE Épargne ↔ Tiers ---------------------------------------------


def test_boucle_complete_compte_ouvert_bloque_puis_fermeture_libere(db: Session) -> None:
    compte, resp = _cadre(db, "B1")
    deposer(db, resp, compte.id, 5000)  # le membre a un compte OUVERT et approvisionné

    # 1) compte ouvert -> désactivation REFUSÉE
    with pytest.raises(EngagementsOuvertsError):
        executer_transition(db, resp, compte.tier_id, "deactivate", CONTEXTE_VIDE, motif="essai")

    # 2) on ferme le compte (restitution + clôture)
    fermer_compte(db, resp, compte.id)

    # 3) plus de compte ouvert -> désactivation AUTORISÉE
    tier = executer_transition(db, resp, compte.tier_id, "deactivate", CONTEXTE_VIDE, motif="ok")
    assert tier.status == "desactive"


# --- Atomicité (vraie connexion) : panne au milieu -> rien --------------------------


def test_fermeture_interrompue_ne_laisse_rien(monkeypatch: pytest.MonkeyPatch) -> None:
    s = SessionLocal()
    caisse = s.execute(
        text("SELECT id FROM comptabilite.accounts WHERE account_number = '5721'")
    ).scalar_one()
    epargne_c = s.execute(
        text("SELECT id FROM comptabilite.accounts WHERE account_number = '3111'")
    ).scalar_one()
    ag = s.execute(
        text(
            "INSERT INTO parameters.agencies (code, name, compte_caisse_id) "
            "VALUES ('AGF-ATO', 'Ato', :c) RETURNING id"
        ),
        {"c": caisse},
    ).scalar_one()
    prod = s.execute(
        text(
            "INSERT INTO epargne.products (code, name, type, compte_epargne_id) "
            "VALUES ('PFATO', 'E', 'a_vue', :e) RETURNING id"
        ),
        {"e": epargne_c},
    ).scalar_one()
    tid = s.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES ('M-FERM-ATO', 'individual', :a, 'actif') RETURNING id"
        ),
        {"a": ag},
    ).scalar_one()
    acc = s.execute(
        text(
            "INSERT INTO epargne.accounts "
            "(account_number, product_id, tier_id, agency_id, balance) "
            "VALUES ('EP-ATO-1', :p, :t, :a, 5000) RETURNING id"
        ),
        {"p": prod, "t": tid, "a": ag},
    ).scalar_one()
    uid = s.execute(text("SELECT id FROM security.users LIMIT 1")).scalar_one()
    s.commit()
    s.close()

    resp = UtilisateurCourant(
        user_id=uid, roles=("RESPONSABLE_AGENCE",),
        permissions=frozenset({"epargne.account.close"}),
        primary_agency_id=ag, agency_id=ag, voit_tout=False,
    )

    def panne(*args: object, **kwargs: object) -> None:
        raise RuntimeError("panne au milieu de la clôture")

    monkeypatch.setattr(service, "_mouvement_restitution", panne)

    try:
        db = SessionLocal()
        try:
            with pytest.raises(RuntimeError):
                fermer_compte(db, resp, acc)
            db.rollback()
        finally:
            db.close()

        verif = SessionLocal()
        try:
            solde, statut = verif.execute(
                text("SELECT balance, status FROM epargne.accounts WHERE id = :a"), {"a": acc}
            ).one()
            nb_mvt = verif.execute(
                text("SELECT count(*) FROM epargne.movements WHERE account_id = :a"), {"a": acc}
            ).scalar_one()
        finally:
            verif.close()
        # RIEN à moitié : compte toujours ouvert, solde intact, aucun mouvement.
        assert solde == 5000
        assert statut == "actif"
        assert nb_mvt == 0
    finally:
        c = SessionLocal()
        c.execute(text("DELETE FROM epargne.movements WHERE account_id = :a"), {"a": acc})
        c.execute(text("DELETE FROM epargne.accounts WHERE id = :a"), {"a": acc})
        c.execute(text("DELETE FROM tiers.tiers WHERE id = :t"), {"t": tid})
        c.execute(text("DELETE FROM epargne.products WHERE id = :p"), {"p": prod})
        c.execute(text("DELETE FROM parameters.agencies WHERE id = :a"), {"a": ag})
        c.commit()
        c.close()
