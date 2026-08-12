"""Parts sociales (PS1) — souscription / libération, la transaction unique, le marqueur.

  - souscription au comptant : D CAISSE / C 571111, parts libérées, is_member bascule ;
  - souscription engagement (D 571121 / C 571111) puis libération (D CAISSE / C 571121) : équilibrées ;
  - is_member bascule au BON moment selon le paramètre d'institution (libération vs souscription) ;
  - nombre de parts <= 0 / valeur d'une part nulle / libération excessive -> refusés ;
  - opération interrompue -> RIEN à moitié (pas de parts sans écriture, ni membre sans paiement) ;
  - rapprochement du capital : Σ parts libérées x valeur == solde comptable 571111 ;
  - détenir des parts bloque la désactivation (registre d'engagements).
"""

import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import engine
from app.core.engagements import verificateurs_enregistres
from app.modules.audit.service import CONTEXTE_VIDE
from app.modules.caisse.models import Poste, PosteAssignation
from app.modules.caisse.service import ouvrir_session
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.tiers import parts, parts_parametres
from app.modules.tiers.cycle_de_vie import EngagementsOuvertsError, executer_transition
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
    agence = Agency(code=f"AGP-{suffixe}", name="Agence", compte_caisse_id=_cid(db, "101111"))
    db.add(agence)
    db.flush()
    tier_id = db.execute(
        text(
            "INSERT INTO tiers.tiers (tier_number, tier_type, primary_agency_id, status) "
            "VALUES (:n, 'individual', :a, 'actif') RETURNING id"
        ),
        {"n": f"M-PS-{suffixe}", "a": agence.id},
    ).scalar_one()
    # Profil individuel : la désactivation (loop) charge la fiche polymorphe -> enfant requis.
    nat = db.execute(text("SELECT id FROM parameters.countries LIMIT 1")).scalar_one()
    db.execute(
        text(
            "INSERT INTO tiers.individual_profiles "
            "(tier_id, last_name, first_name, birth_date, gender, nationality_id) "
            "VALUES (:t, 'Test', 'Sociétaire', '1990-01-01', 'M', :nat)"
        ),
        {"t": tier_id, "nat": nat},
    )
    db.execute(text("DELETE FROM tiers.share_parameters"))
    db.execute(
        text(
            "INSERT INTO tiers.share_parameters "
            "(unit_value, minimum_shares, is_refundable, membership_on, "
            " compte_parts_liberees_id, compte_parts_non_liberees_id, is_provisional) "
            "VALUES (:u, :m, TRUE, :mo, "
            " (SELECT id FROM comptabilite.accounts WHERE account_number='571111'), "
            " (SELECT id FROM comptabilite.accounts WHERE account_number='571121'), TRUE)"
        ),
        {"u": unit_value, "m": minimum, "mo": membership_on},
    )
    courant = _courant(db, agence.id)
    # Poste + session de caisse OUVERTE pour l'acteur (Bloc C3) : la souscription au comptant
    # exige désormais SA session, plus la seule agence — même compte que l'ancien
    # Agency.compte_caisse_id, miroir du backfill de la migration 0041.
    poste = Poste(
        agency_id=agence.id, code="01", libelle="Caisse principale",
        compte_caisse_id=agence.compte_caisse_id,
    )
    db.add(poste)
    db.flush()
    db.add(PosteAssignation(poste_id=poste.id, user_id=courant.user_id))
    db.flush()
    ouvrir_session(db, courant, poste_id=poste.id, fonds_initial=0)
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


def test_souscription_comptant_credite_57111_et_bascule_membre(db: Session) -> None:
    courant, tier_id = _cadre(db, "C1")
    r = parts.souscrire(db, courant, tier_id, 10, comptant=True)  # 10 x 5000 = 50 000

    assert r.shares_liberees == 10
    assert r.is_member is True
    # Argent qui entre en caisse, capital libéré qui monte : D 101111 / C 571111.
    assert _lignes(db, tier_id, "souscription_comptant") == {
        ("101111", "D", 50000),
        ("571111", "C", 50000),
    }
    assert _est_membre(db, tier_id) is True


def test_engagement_puis_liberation_ecritures_equilibrees(db: Session) -> None:
    courant, tier_id = _cadre(db, "E1")  # membership_on = liberation

    r1 = parts.souscrire(db, courant, tier_id, 4, comptant=False)  # engagement, sans caisse
    assert (r1.shares_non_liberees, r1.shares_liberees) == (4, 0)
    assert r1.is_member is False  # rien de libéré -> pas encore membre
    assert _lignes(db, tier_id, "souscription") == {("571121", "D", 20000), ("571111", "C", 20000)}

    r2 = parts.liberer(db, courant, tier_id, 4)  # paiement en caisse
    assert (r2.shares_liberees, r2.shares_non_liberees) == (4, 0)
    assert r2.is_member is True  # membre à la LIBÉRATION (capital réel)
    assert _lignes(db, tier_id, "liberation") == {("101111", "D", 20000), ("571121", "C", 20000)}


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
    parts.souscrire(db, courant, tier_id, 10, comptant=True)  # 50 000 en 571111

    resultat = rapprocher_capital_libere(db)
    assert resultat.compte_general == "571111"
    assert resultat.concordant is True
    assert resultat.ecart == 0


def test_rapprochement_tient_apres_un_changement_de_rattachement(db: Session) -> None:
    """LE point sensible (Finding 3) : après un reroutage vers d'autres comptes, le
    rapprochement doit continuer à compter l'historique posté sur les ANCIENS comptes — pas
    seulement ce qui se pose désormais sur les nouveaux. Sans tiers.share_account_roles, ce
    test échouerait (le NET ne compterait plus que la seconde souscription)."""
    # Référence AVANT toute action de ce test : l'auxiliaire est une somme GLOBALE (tous les
    # tiers), pas scopée à un seul — la base de dev partagée peut déjà porter d'autres parts
    # réelles. On mesure donc un DELTA, pas un total absolu.
    avant = rapprocher_capital_libere(db)
    assert avant.concordant is True  # la base est déjà cohérente avant qu'on y touche

    courant, tier_id = _cadre(db, "H1")
    parts.souscrire(db, courant, tier_id, 10, comptant=True)  # 50 000 sur 571111 (génération 1)

    # Deux comptes de saisie de test, jouant le rôle de la « génération 2 » (comme le VRAI
    # chantier a introduit 571111/571121 par-dessus 57111/57112), même sens que les comptes
    # qu'ils remplacent.
    db.execute(
        text(
            "INSERT INTO comptabilite.accounts "
            "(account_number, name, account_class, normal_side, is_posting) "
            "VALUES ('6T720', 'Parts libérées (test)', 6, 'C', TRUE), "
            "('6T721', 'Parts non libérées (test)', 6, 'D', TRUE)"
        )
    )
    config = parts_parametres.lire(db)
    parts_parametres.modifier(
        db,
        config,
        unit_value=config.unit_value,
        minimum_shares=config.minimum_shares,
        is_refundable=config.is_refundable,
        membership_on=config.membership_on,
        compte_parts_liberees_number="6T720",
        compte_parts_non_liberees_number="6T721",
        motif="Bascule vers les comptes d'extension (test)",
        par=None,
    )

    parts.souscrire(db, courant, tier_id, 5, comptant=True)  # 25 000 sur 6T720 (génération 2)

    resultat = rapprocher_capital_libere(db)
    # Delta = 15 parts x valeur, quel que soit ce que la base portait déjà avant ce test.
    assert resultat.auxiliaire - avant.auxiliaire == 15 * config.unit_value
    # Général : +50 000 (ancien compte, jamais oublié) +25 000 (nouveau compte) = +75 000.
    assert resultat.general - avant.general == 75000
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


def test_consulter_parts_sans_parts_puis_apres_souscription(db: Session) -> None:
    courant, tier_id = _cadre(db, "CO", unit_value=5000, minimum=1)

    # Avant toute opération : zéros, mais la config provisoire est rendue (pas de crash).
    vide = parts.consulter(db, courant, tier_id)
    assert vide.shares_liberees == 0 and vide.capital_libere == 0
    assert vide.is_member is False
    assert vide.unit_value == 5000 and vide.minimum_shares == 1
    assert vide.is_provisional is True
    assert vide.mouvements == []

    parts.souscrire(db, courant, tier_id, 10, comptant=True)
    fiche = parts.consulter(db, courant, tier_id)
    assert fiche.shares_liberees == 10
    assert fiche.capital_libere == 50000  # 10 x 5000
    assert fiche.is_member is True
    assert len(fiche.mouvements) == 1
    assert fiche.mouvements[0].type == "souscription_comptant"


def test_le_verificateur_parts_est_enregistre_a_lassemblage_de_lapp() -> None:
    import app.main  # noqa: F401  (l'import assemble l'app et enregistre les vérificateurs)

    assert verifier_engagements_parts in verificateurs_enregistres()


# --- PS2 : remboursement / annulation (sortie du sociétariat) -------------------------------


def _responsable(db: Session, agency_id: uuid.UUID) -> UtilisateurCourant:
    """Un responsable : rembourse les parts ET désactive (deux permissions de la sortie)."""
    uid = db.execute(text("SELECT id FROM security.users LIMIT 1")).scalar_one()
    return UtilisateurCourant(
        user_id=uid,
        roles=("RESPONSABLE_AGENCE",),
        permissions=frozenset({"tiers.shares.refund", "tiers.deactivate"}),
        primary_agency_id=agency_id,
        agency_id=agency_id,
        voit_tout=True,
    )


def test_remboursement_total_rend_le_capital_et_repasse_client(db: Session) -> None:
    courant, tier_id = _cadre(db, "RB")
    parts.souscrire(db, courant, tier_id, 10, comptant=True)  # membre, 50 000 en 571111
    assert _est_membre(db, tier_id) is True

    r = parts.rembourser(db, courant, tier_id, 10)  # 10 x 5000 = 50 000
    assert r.shares_liberees == 0
    assert r.is_member is False  # redevient client, DANS la txn du remboursement
    # Le capital sort : D 571111 / C 101111 (l'argent quitte la caisse).
    assert _lignes(db, tier_id, "remboursement") == {("571111", "D", 50000), ("101111", "C", 50000)}
    assert _est_membre(db, tier_id) is False


def test_boucle_desactivation_membre_puis_remboursement(db: Session) -> None:
    courant, tier_id = _cadre(db, "BC")
    resp = _responsable(db, courant.agency_id)
    parts.souscrire(db, courant, tier_id, 10, comptant=True)

    # Membre avec des parts -> désactivation REFUSÉE (engagement).
    with pytest.raises(EngagementsOuvertsError) as exc:
        executer_transition(db, resp, tier_id, "deactivate", CONTEXTE_VIDE, motif="test")
    assert any("part" in e.libelle.lower() for e in exc.value.engagements)

    # Remboursement total -> plus de capital.
    parts.rembourser(db, resp, tier_id, 10)

    # Désactivation désormais AUTORISÉE : la boucle se referme.
    tier = executer_transition(db, resp, tier_id, "deactivate", CONTEXTE_VIDE, motif="test")
    assert tier.status == "desactive"


def test_remboursement_partiel_sous_le_minimum_repasse_client_mais_reste_bloque(
    db: Session,
) -> None:
    courant, tier_id = _cadre(db, "PA", minimum=5)
    parts.souscrire(db, courant, tier_id, 10, comptant=True)  # 10 >= 5 -> membre
    assert _est_membre(db, tier_id) is True

    r = parts.rembourser(db, courant, tier_id, 8)  # reste 2 < minimum 5
    assert r.shares_liberees == 2
    assert r.is_member is False  # le marqueur suit : redevient client
    # Mais détient encore 2 parts -> toujours NON désactivable.
    assert verifier_engagements_parts(db, tier_id) != []


def test_remboursement_excessif_refuse(db: Session) -> None:
    courant, tier_id = _cadre(db, "RX")
    parts.souscrire(db, courant, tier_id, 3, comptant=True)
    with pytest.raises(parts.RemboursementExcessifError):
        parts.rembourser(db, courant, tier_id, 5)


def test_remboursement_refuse_si_parts_non_remboursables(db: Session) -> None:
    courant, tier_id = _cadre(db, "NR")
    parts.souscrire(db, courant, tier_id, 4, comptant=True)
    db.execute(text("UPDATE tiers.share_parameters SET is_refundable = FALSE"))
    with pytest.raises(parts.NonRemboursableError):
        parts.rembourser(db, courant, tier_id, 4)


def test_annulation_non_liberees_solde_lengagement_sans_caisse(db: Session) -> None:
    courant, tier_id = _cadre(db, "AN")
    parts.souscrire(db, courant, tier_id, 6, comptant=False)  # 6 non libérées
    r = parts.annuler_souscription(db, courant, tier_id, 6)
    assert r.shares_non_liberees == 0
    # D 571111 / C 571121, sans caisse (inverse de la souscription-engagement).
    assert _lignes(db, tier_id, "annulation") == {("571111", "D", 30000), ("571121", "C", 30000)}


def test_rapprochement_capital_tient_apres_remboursement(db: Session) -> None:
    courant, tier_id = _cadre(db, "RR")
    parts.souscrire(db, courant, tier_id, 10, comptant=True)
    parts.rembourser(db, courant, tier_id, 4)  # reste 6 libérées
    resultat = rapprocher_capital_libere(db)
    assert resultat.concordant is True
    assert resultat.ecart == 0


def test_remboursement_interrompu_ne_laisse_rien(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    courant, tier_id = _cadre(db, "RI2")
    parts.souscrire(db, courant, tier_id, 10, comptant=True)  # membre, 10 libérées

    def panne(*args: object, **kwargs: object) -> None:
        raise RuntimeError("panne au milieu du remboursement")

    monkeypatch.setattr(parts, "ecrire_audit", panne)
    with pytest.raises(RuntimeError):
        parts.rembourser(db, courant, tier_id, 10)
    db.rollback()

    # RIEN à moitié : le membre garde ses 10 parts, is_member reste True, aucun mouvement de remb.
    liberees = db.execute(
        text("SELECT shares_liberees FROM tiers.member_shares WHERE tier_id = :t"), {"t": tier_id}
    ).scalar_one()
    nb_remb = db.execute(
        text(
            "SELECT count(*) FROM tiers.share_subscriptions "
            "WHERE tier_id = :t AND type = 'remboursement'"
        ),
        {"t": tier_id},
    ).scalar_one()
    assert liberees == 10
    assert nb_remb == 0
    assert _est_membre(db, tier_id) is True
