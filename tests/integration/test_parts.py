"""Parts sociales (PS1) — souscription / libération, la transaction unique, le marqueur.

  - souscription au comptant : D CAISSE / C 1021, parts libérées, is_member bascule ;
  - souscription engagement (D 1022 / C 1021) puis libération (D CAISSE / C 1022) : équilibrées ;
  - is_member bascule au BON moment selon le paramètre d'institution (libération vs souscription) ;
  - nombre de parts <= 0 / valeur d'une part nulle / libération excessive -> refusés ;
  - opération interrompue -> RIEN à moitié (pas de parts sans écriture, ni membre sans paiement) ;
  - rapprochement du capital : Σ parts libérées x valeur == solde comptable 1021 ;
  - détenir des parts bloque la désactivation (registre d'engagements).
"""

import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import engine
from app.core.engagements import verificateurs_enregistres
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.tiers import parts
from app.modules.tiers.parts_engagements import verifier_engagements_parts
from app.modules.tiers.parts_rapprochement import rapprocher_capital_libere

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


def _courant(db: Session, agency_id: uuid.UUID) -> UtilisateurCourant:
    uid = db.execute(text("SELECT id FROM security.users LIMIT 1")).scalar_one()
    return UtilisateurCourant(
        user_id=uid,
        roles=("CAISSIER",),
        permissions=frozenset({"tiers.shares.subscribe"}),
        primary_agency_id=agency_id,
        agency_id=agency_id,
        voit_tout=True,
    )


def _cadre(
    db: Session,
    suffixe: str,
    *,
    unit_value: int = 5000,
    minimum: int = 1,
    membership_on: str = "liberation",
) -> tuple[UtilisateurCourant, uuid.UUID]:
    """Agence + tier ACTIF + config de parts (provisoire, valeurs de test). Committé (au savepoint)
    pour que le test d'interruption puisse rollback la SEULE opération, pas le décor."""
    agence = Agency(code=f"AGP-{suffixe}", name="Agence", compte_caisse_id=_cid(db, "5721"))
    db.add(agence)
    db.flush()
    tier_id = db.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES (:n, 'individual', :a, 'actif') RETURNING id"
        ),
        {"n": f"M-PS-{suffixe}", "a": agence.id},
    ).scalar_one()
    db.execute(text("DELETE FROM tiers.share_parameters"))
    db.execute(
        text(
            "INSERT INTO tiers.share_parameters "
            "(unit_value, minimum_shares, is_refundable, membership_on, "
            " compte_parts_liberees_id, compte_parts_non_liberees_id, is_provisional) "
            "VALUES (:u, :m, TRUE, :mo, "
            " (SELECT id FROM comptabilite.accounts WHERE account_number='1021'), "
            " (SELECT id FROM comptabilite.accounts WHERE account_number='1022'), TRUE)"
        ),
        {"u": unit_value, "m": minimum, "mo": membership_on},
    )
    courant = _courant(db, agence.id)
    db.commit()
    return courant, tier_id


def _lignes(db: Session, tier_id: uuid.UUID, type_: str) -> set[tuple[str, str, int]]:
    """Les lignes comptables de la pièce liée au mouvement de parts (numéro, sens, montant)."""
    return {
        (n, s, m)
        for n, s, m in db.execute(
            text(
                "SELECT a.account_number, l.side, l.amount FROM comptabilite.journal_lines l "
                "JOIN comptabilite.accounts a ON a.id = l.account_id "
                "JOIN tiers.share_subscriptions ss ON ss.journal_entry_id = l.entry_id "
                "WHERE ss.tier_id = :t AND ss.type = :ty"
            ),
            {"t": tier_id, "ty": type_},
        )
    }


def _est_membre(db: Session, tier_id: uuid.UUID) -> bool:
    return db.execute(
        text("SELECT is_member FROM tiers.tiers WHERE id = :t"), {"t": tier_id}
    ).scalar_one()


def test_souscription_comptant_credite_1021_et_bascule_membre(db: Session) -> None:
    courant, tier_id = _cadre(db, "C1")
    r = parts.souscrire(db, courant, tier_id, 10, comptant=True)  # 10 x 5000 = 50 000

    assert r.shares_liberees == 10
    assert r.is_member is True
    # Argent qui entre en caisse, capital libéré qui monte : D 5721 / C 1021.
    assert _lignes(db, tier_id, "souscription_comptant") == {
        ("5721", "D", 50000),
        ("1021", "C", 50000),
    }
    assert _est_membre(db, tier_id) is True


def test_engagement_puis_liberation_ecritures_equilibrees(db: Session) -> None:
    courant, tier_id = _cadre(db, "E1")  # membership_on = liberation

    r1 = parts.souscrire(db, courant, tier_id, 4, comptant=False)  # engagement, sans caisse
    assert (r1.shares_non_liberees, r1.shares_liberees) == (4, 0)
    assert r1.is_member is False  # rien de libéré -> pas encore membre
    assert _lignes(db, tier_id, "souscription") == {("1022", "D", 20000), ("1021", "C", 20000)}

    r2 = parts.liberer(db, courant, tier_id, 4)  # paiement en caisse
    assert (r2.shares_liberees, r2.shares_non_liberees) == (4, 0)
    assert r2.is_member is True  # membre à la LIBÉRATION (capital réel)
    assert _lignes(db, tier_id, "liberation") == {("5721", "D", 20000), ("1022", "C", 20000)}


def test_membre_a_la_souscription_si_le_parametre_le_dit(db: Session) -> None:
    courant, tier_id = _cadre(db, "MS", membership_on="souscription")
    # Engagement sans paiement, mais le paramètre confère la qualité de membre dès la souscription.
    r = parts.souscrire(db, courant, tier_id, 2, comptant=False)
    assert r.shares_liberees == 0
    assert r.is_member is True


def test_refus_nombre_de_parts_invalide(db: Session) -> None:
    courant, tier_id = _cadre(db, "RI")
    with pytest.raises(parts.PartsInvalidesError):
        parts.souscrire(db, courant, tier_id, 0, comptant=True)


def test_refus_valeur_de_part_nulle(db: Session) -> None:
    courant, tier_id = _cadre(db, "V0", unit_value=0)
    with pytest.raises(parts.MontantInvalideError):
        parts.souscrire(db, courant, tier_id, 5, comptant=True)


def test_liberation_excessive_refusee(db: Session) -> None:
    courant, tier_id = _cadre(db, "LE")
    parts.souscrire(db, courant, tier_id, 3, comptant=False)  # 3 non libérées
    with pytest.raises(parts.LiberationExcessiveError):
        parts.liberer(db, courant, tier_id, 5)  # plus que souscrit


def test_operation_interrompue_ne_laisse_rien(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    courant, tier_id = _cadre(db, "INT")

    # Panne injectée APRÈS pièce + mouvement + cache + marqueur (à l'audit), avant le commit.
    def panne(*args: object, **kwargs: object) -> None:
        raise RuntimeError("panne au milieu de l'opération")

    monkeypatch.setattr(parts, "ecrire_audit", panne)
    with pytest.raises(RuntimeError):
        parts.souscrire(db, courant, tier_id, 10, comptant=True)
    db.rollback()

    # RIEN à moitié : aucun mouvement de parts, is_member intact (False), aucun solde de parts.
    nb_mvt = db.execute(
        text("SELECT count(*) FROM tiers.share_subscriptions WHERE tier_id = :t"), {"t": tier_id}
    ).scalar_one()
    nb_solde = db.execute(
        text("SELECT count(*) FROM tiers.member_shares WHERE tier_id = :t"), {"t": tier_id}
    ).scalar_one()
    assert nb_mvt == 0
    assert nb_solde == 0
    assert _est_membre(db, tier_id) is False


def test_rapprochement_capital_concorde_apres_liberation(db: Session) -> None:
    courant, tier_id = _cadre(db, "RA")
    parts.souscrire(db, courant, tier_id, 10, comptant=True)  # 50 000 en 1021

    resultat = rapprocher_capital_libere(db)
    assert resultat.compte_general == "1021"
    assert resultat.concordant is True
    assert resultat.ecart == 0


def test_detenir_des_parts_bloque_la_desactivation(db: Session) -> None:
    courant, tier_id = _cadre(db, "BD")
    # Sans parts : aucun engagement.
    assert verifier_engagements_parts(db, tier_id) == []

    parts.souscrire(db, courant, tier_id, 10, comptant=True)
    engagements = verifier_engagements_parts(db, tier_id)
    assert len(engagements) == 1
    assert "part" in engagements[0].libelle.lower()


def test_le_verificateur_parts_est_enregistre_a_lassemblage_de_lapp() -> None:
    import app.main  # noqa: F401  (l'import assemble l'app et enregistre les vérificateurs)

    assert verifier_engagements_parts in verificateurs_enregistres()
