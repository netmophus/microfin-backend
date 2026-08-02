"""Guichet — concurrence et atomicité, prouvées de façon DÉTERMINISTE (pas de threads).

  - deux retraits simultanés, un seul passe : le verrou FOR UPDATE sérialise ; le second, prouvé
    par NOWAIT, ne peut pas décider tant que le premier tient le verrou, puis voit le solde RÉEL ;
  - opération interrompue au milieu (après la pièce, avant le mouvement) : RIEN ne subsiste — ni
    pièce sans mouvement, ni solde bougé sans écriture.

Vraies connexions (deux transactions concurrentes), données committées et nettoyées en fin.
"""

import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.core.database import SessionLocal, engine
from app.modules.epargne import guichet
from app.modules.epargne.guichet import SoldeInsuffisantError
from app.modules.security.autorisation import UtilisateurCourant

pytestmark = pytest.mark.integration


@pytest.fixture
def donnees() -> Generator[dict[str, uuid.UUID], None, None]:
    """Compte d'épargne à solde 1000, committé (deux connexions doivent le voir). Nettoyé après."""
    s = SessionLocal()
    caisse = s.execute(
        text("SELECT id FROM comptabilite.accounts WHERE account_number = '1011'")
    ).scalar_one()
    epargne = s.execute(
        text("SELECT id FROM comptabilite.accounts WHERE account_number = '251111'")
    ).scalar_one()
    ag = s.execute(
        text(
            "INSERT INTO parameters.agencies (code, name, compte_caisse_id) "
            "VALUES ('AG-CONC', 'Conc', :caisse) RETURNING id"
        ),
        {"caisse": caisse},
    ).scalar_one()
    prod = s.execute(
        text(
            "INSERT INTO epargne.products (code, name, type, compte_epargne_id) "
            "VALUES ('PCONC', 'E', 'a_vue', :epargne) RETURNING id"
        ),
        {"epargne": epargne},
    ).scalar_one()
    tid = s.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES ('M-CONC', 'individual', :a, 'actif') RETURNING id"
        ),
        {"a": ag},
    ).scalar_one()
    acc = s.execute(
        text(
            "INSERT INTO epargne.accounts "
            "(account_number, product_id, tier_id, agency_id, balance) "
            "VALUES ('EP-CONC-1', :p, :t, :a, 1000) RETURNING id"
        ),
        {"p": prod, "t": tid, "a": ag},
    ).scalar_one()
    uid = s.execute(text("SELECT id FROM security.users LIMIT 1")).scalar_one()
    s.commit()
    s.close()
    try:
        yield {"agence": ag, "produit": prod, "tier": tid, "compte": acc, "user": uid}
    finally:
        c = SessionLocal()
        c.execute(text("DELETE FROM epargne.movements WHERE account_id = :a"), {"a": acc})
        c.execute(text("DELETE FROM epargne.accounts WHERE id = :a"), {"a": acc})
        c.execute(text("DELETE FROM tiers.tiers WHERE id = :t"), {"t": tid})
        c.execute(text("DELETE FROM epargne.products WHERE id = :p"), {"p": prod})
        c.execute(text("DELETE FROM parameters.agencies WHERE id = :a"), {"a": ag})
        c.commit()
        c.close()


def _caissier(donnees: dict[str, uuid.UUID]) -> UtilisateurCourant:
    return UtilisateurCourant(
        user_id=donnees["user"],
        roles=("CAISSIER",),
        permissions=frozenset({"epargne.operation.deposit", "epargne.operation.withdraw"}),
        primary_agency_id=donnees["agence"],
        agency_id=donnees["agence"],
        voit_tout=False,
    )


def test_deux_retraits_simultanes_un_seul_passe(donnees: dict[str, uuid.UUID]) -> None:
    compte = donnees["compte"]
    caissier = _caissier(donnees)

    # A prend le verrou de la ligne (comme le fait retirer via FOR UPDATE), sans committer.
    conn_a = engine.connect()
    tx_a = conn_a.begin()
    conn_a.execute(text("SELECT id FROM epargne.accounts WHERE id = :a FOR UPDATE"), {"a": compte})

    # B tente le même verrou SANS attendre : il LÈVE — B ne peut pas décider pendant que A tient.
    conn_b = engine.connect()
    tx_b = conn_b.begin()
    with pytest.raises(OperationalError):
        conn_b.execute(
            text("SELECT id FROM epargne.accounts WHERE id = :a FOR UPDATE NOWAIT"), {"a": compte}
        )
    tx_b.rollback()
    conn_b.close()

    # A effectue son retrait (solde 1000 -> 0) et COMMIT : le verrou tombe.
    conn_a.execute(text("UPDATE epargne.accounts SET balance = 0 WHERE id = :a"), {"a": compte})
    tx_a.commit()
    conn_a.close()

    # B, le second, lit maintenant le solde RÉEL (0) et est refusé — il ne passe pas deux fois.
    db_b = SessionLocal()
    try:
        with pytest.raises(SoldeInsuffisantError):
            guichet.retirer(db_b, caissier, compte, 1000)
    finally:
        db_b.rollback()
        db_b.close()


def test_operation_interrompue_ne_laisse_rien(
    donnees: dict[str, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    compte = donnees["compte"]
    caissier = _caissier(donnees)

    # Panne injectée APRÈS la pièce (étape 4), au moment d'écrire le mouvement (étape 5).
    def panne(*args: object, **kwargs: object) -> None:
        raise RuntimeError("panne au milieu de l'opération")

    monkeypatch.setattr(guichet, "_enregistrer_mouvement", panne)

    mesure = SessionLocal()
    entrees_avant = mesure.execute(
        text("SELECT count(*) FROM comptabilite.journal_entries")
    ).scalar_one()
    mesure.close()

    db = SessionLocal()
    try:
        with pytest.raises(RuntimeError):
            guichet.deposer(db, caissier, compte, 500)
        db.rollback()
    finally:
        db.close()

    verif = SessionLocal()
    try:
        solde = verif.execute(
            text("SELECT balance FROM epargne.accounts WHERE id = :a"), {"a": compte}
        ).scalar_one()
        nb_mouvements = verif.execute(
            text("SELECT count(*) FROM epargne.movements WHERE account_id = :a"), {"a": compte}
        ).scalar_one()
        entrees_apres = verif.execute(
            text("SELECT count(*) FROM comptabilite.journal_entries")
        ).scalar_one()
    finally:
        verif.close()

    # RIEN à moitié : solde intact, aucun mouvement, aucune pièce (l'écriture a roulé en arrière).
    assert solde == 1000
    assert nb_mouvements == 0
    assert entrees_apres == entrees_avant
