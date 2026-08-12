"""Routage comptable membre/client (PS3) — l'épargne d'un membre va sur 251111, celle d'un
simple client sur 251121, ANCRÉ à l'ouverture du compte.

  - membre ouvre -> ancré 251111, dépôt -> écriture C 251111 ;
  - client ouvre -> ancré 251121, dépôt -> écriture C 251121 ;
  - client devient membre -> ses comptes existants RESTENT sur 251121 (aucun re-routage, aucun
    transfert automatique — option B, à valider expert) ; un NOUVEAU compte part sur 251111 ;
  - repli : produit sans rattachement client -> tout sur 251111 (comportement historique intact) ;
  - rapprochement PAR GROUPE : Σ soldes ancrés 251111 == 251111 ET
    Σ soldes ancrés 251121 == 251121 ;
  - les intérêts suivent l'ancrage (un compte client est crédité sur 251121).

Le basculement is_member est posé en SQL ici : COMMENT on devient membre (parts sociales) est
prouvé dans test_parts.py ; ce fichier ne teste que le ROUTAGE qui en découle.
"""

import uuid
from collections.abc import Generator
from datetime import date

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine
from app.modules.caisse.models import CaisseSession, Poste, PosteAssignation
from app.modules.caisse.service import ouvrir_session
from app.modules.epargne import service
from app.modules.epargne.guichet import deposer
from app.modules.epargne.interets import verser_interets
from app.modules.epargne.models import Product
from app.modules.epargne.rapprochement import rapprocher
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


def _caissier(db: Session, agency_id: uuid.UUID) -> UtilisateurCourant:
    """Idempotent : `security.users LIMIT 1` renvoie TOUJOURS le même uid — n'ouvre qu'UNE
    session de caisse (Bloc C4) pour ce caissier fictif, la réutilise sinon."""
    uid = db.execute(text("SELECT id FROM security.users LIMIT 1")).scalar_one()
    courant = UtilisateurCourant(
        user_id=uid,
        roles=("CAISSIER",),
        permissions=frozenset({"epargne.operation.deposit"}),
        primary_agency_id=agency_id,
        agency_id=agency_id,
        voit_tout=True,
    )
    deja_ouverte = db.execute(
        select(CaisseSession.id).where(
            CaisseSession.caissier_id == uid, CaisseSession.status == "ouverte"
        )
    ).first()
    if deja_ouverte is None:
        compte_caisse_id = db.execute(
            select(Agency.compte_caisse_id).where(Agency.id == agency_id)
        ).scalar_one()
        poste = Poste(
            agency_id=agency_id, code="01", libelle="Caisse principale",
            compte_caisse_id=compte_caisse_id,
        )
        db.add(poste)
        db.flush()
        db.add(PosteAssignation(poste_id=poste.id, user_id=uid))
        db.flush()
        ouvrir_session(db, courant, poste_id=poste.id, fonds_initial=0)
    return courant


def _tier(db: Session, agency_id: uuid.UUID, suffixe: str, *, membre: bool) -> uuid.UUID:
    return db.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status, "
            "is_member) VALUES (:n, 'individual', :a, 'actif', :m) RETURNING id"
        ),
        {"n": f"M-RT-{suffixe}", "a": agency_id, "m": membre},
    ).scalar_one()


def _cadre(db: Session, suffixe: str, *, avec_client: bool = True, taux_bp: int = 0):
    """Agence + produit rattaché 251111 (membre) et, si demandé, 251121 (client)."""
    agence = Agency(code=f"AGR-{suffixe}", name="Agence", compte_caisse_id=_cid(db, "101111"))
    produit = Product(
        code=f"PR{suffixe}", name="Épargne", type="a_vue",
        compte_epargne_id=_cid(db, "251111"),
        compte_epargne_client_id=_cid(db, "251121") if avec_client else None,
        compte_charge_interet_id=_cid(db, "602511"),
        taux_bp=taux_bp, base_jours=365, methode_calcul_solde="fin_periode",
    )
    db.add_all([agence, produit])
    db.flush()
    return agence, produit


def _ancre(db: Session, account_id: uuid.UUID) -> str | None:
    return db.execute(
        text(
            "SELECT c.account_number FROM epargne.accounts a "
            "LEFT JOIN comptabilite.accounts c ON c.id = a.compte_collectif_id WHERE a.id = :a"
        ),
        {"a": account_id},
    ).scalar_one()


def _lignes_epargne(db: Session, account_id: uuid.UUID) -> set[tuple[str, str, int]]:
    """Les lignes comptables (compte, sens, montant) des pièces liées aux mouvements du compte."""
    return {
        (n, s, m)
        for n, s, m in db.execute(
            text(
                "SELECT ca.account_number, l.side, l.amount FROM comptabilite.journal_lines l "
                "JOIN comptabilite.accounts ca ON ca.id = l.account_id "
                "JOIN epargne.movements mv ON mv.journal_entry_id = l.entry_id "
                "WHERE mv.account_id = :a"
            ),
            {"a": account_id},
        )
    }


def _ouvrir(db: Session, produit: Product, agence: Agency, tier_id: uuid.UUID):
    return service.ouvrir_compte(
        db, tier_id=tier_id, product_id=produit.id, agency_id=agence.id, par=None
    )


def test_membre_ouvre_ancre_251111_et_depose_sur_251111(db: Session) -> None:
    agence, produit = _cadre(db, "M1")
    tier_id = _tier(db, agence.id, "M1", membre=True)
    compte = _ouvrir(db, produit, agence, tier_id)

    assert _ancre(db, compte.id) == "251111"
    deposer(db, _caissier(db, agence.id), compte.id, 10000)
    assert ("251111", "C", 10000) in _lignes_epargne(db, compte.id)


def test_client_ouvre_ancre_251121_et_depose_sur_251121(db: Session) -> None:
    agence, produit = _cadre(db, "C1")
    tier_id = _tier(db, agence.id, "C1", membre=False)
    compte = _ouvrir(db, produit, agence, tier_id)

    assert _ancre(db, compte.id) == "251121"
    deposer(db, _caissier(db, agence.id), compte.id, 8000)
    lignes = _lignes_epargne(db, compte.id)
    assert ("251121", "C", 8000) in lignes
    assert not any(n == "251111" for n, _s, _m in lignes)  # rien ne fuit vers le compte membre


def test_client_devenu_membre_ses_comptes_restent_sur_251121(db: Session) -> None:
    agence, produit = _cadre(db, "D1")
    tier_id = _tier(db, agence.id, "D1", membre=False)
    caissier = _caissier(db, agence.id)
    ancien = _ouvrir(db, produit, agence, tier_id)  # ouvert en CLIENT -> 251121
    deposer(db, caissier, ancien.id, 5000)

    # Le client devient membre (le COMMENT — souscription de parts — est prouvé dans test_parts).
    db.execute(text("UPDATE tiers.tiers SET is_member = TRUE WHERE id = :t"), {"t": tier_id})

    # Son compte EXISTANT reste ancré 251121 : les nouveaux mouvements y vont toujours.
    deposer(db, caissier, ancien.id, 3000)
    assert _ancre(db, ancien.id) == "251121"
    lignes = _lignes_epargne(db, ancien.id)
    assert ("251121", "C", 5000) in lignes and ("251121", "C", 3000) in lignes
    assert not any(n == "251111" for n, _s, _m in lignes)  # aucun re-routage, aucun transfert

    # Un NOUVEAU compte, ouvert membre, part sur 251111.
    nouveau = _ouvrir(db, produit, agence, tier_id)
    assert _ancre(db, nouveau.id) == "251111"


def test_repli_produit_sans_rattachement_client(db: Session) -> None:
    # Produit sans compte client (NULL) : un client est ancré sur le compte MEMBRE — le
    # comportement historique, intact tant que 251121 n'est pas configuré.
    agence, produit = _cadre(db, "R1", avec_client=False)
    tier_id = _tier(db, agence.id, "R1", membre=False)
    compte = _ouvrir(db, produit, agence, tier_id)

    assert _ancre(db, compte.id) == "251111"
    deposer(db, _caissier(db, agence.id), compte.id, 2000)
    assert ("251111", "C", 2000) in _lignes_epargne(db, compte.id)


def test_rapprochement_concorde_par_groupe(db: Session) -> None:
    agence, produit = _cadre(db, "G1")
    caissier = _caissier(db, agence.id)
    base_m = rapprocher(db, _cid(db, "251111"))  # la base de dev vit : deltas, pas d'absolus
    base_c = rapprocher(db, _cid(db, "251121"))

    membre = _ouvrir(db, produit, agence, _tier(db, agence.id, "G1M", membre=True))
    client = _ouvrir(db, produit, agence, _tier(db, agence.id, "G1C", membre=False))
    deposer(db, caissier, membre.id, 12000)
    deposer(db, caissier, client.id, 7000)

    apres_m = rapprocher(db, _cid(db, "251111"))
    apres_c = rapprocher(db, _cid(db, "251121"))
    # Chaque groupe bouge du montant de SON dépôt, et chacun concorde face à SON compte.
    assert apres_m.auxiliaire - base_m.auxiliaire == 12000
    assert apres_c.auxiliaire - base_c.auxiliaire == 7000
    assert apres_m.concordant is True
    assert apres_c.concordant is True


def test_les_interets_d_un_client_suivent_l_ancrage_251121(db: Session) -> None:
    # 10 % l'an base 365 : 100 000 déposés toute l'année -> 10 000 d'intérêts, crédités sur 251121.
    agence, produit = _cadre(db, "I1", taux_bp=1000)
    tier_id = _tier(db, agence.id, "I1", membre=False)
    compte = _ouvrir(db, produit, agence, tier_id)
    deposer(db, _caissier(db, agence.id), compte.id, 100000)

    verser_interets(db, periode="2026-RT", debut=date(2026, 1, 1), fin=date(2026, 12, 31))

    lignes = _lignes_epargne(db, compte.id)
    assert ("251121", "C", 10000) in lignes  # la dette envers le CLIENT monte sur SON collectif
    assert ("602511", "D", 10000) in lignes
