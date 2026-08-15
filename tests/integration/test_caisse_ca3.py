"""Caisse CA3 — rattachement comptable de l'écart, écriture posée À LA VALIDATION.

DEUX comptes distincts (jamais un signe négatif sur un seul) : manquant -> D ECART / C CAISSE
(le théorique baisse jusqu'au réel compté) ; excédent -> D CAISSE / C ECART (l'inverse).
Journal OD : une RÉGULARISATION, pas un mouvement de caisse d'un client.

LE GARDE-FOU CENTRAL DE CE BLOC : si le comptable n'a pas encore rattaché le compte de l'écart,
la validation est REFUSÉE PROPREMENT (422, RattachementEcartManquantError) — jamais une
exception non gérée, et surtout jamais une session marquée validée SANS son écriture posée
(transaction unique : la pièce d'abord, la trace de validation ensuite, tout ou rien)."""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db
from app.main import app
from app.modules.caisse.models import CaisseParametres, Poste, PosteAssignation
from app.modules.caisse.service import (
    RattachementEcartManquantError,
    fermer_session,
    ouvrir_session,
    valider_ecart,
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


def _agence(db: Session, code: str) -> Agency:
    compte_id = _cid(db, "101111")
    agence = Agency(code=code, name=f"Agence {code}", compte_caisse_id=compte_id)
    db.add(agence)
    db.flush()
    db.add(
        Poste(
            agency_id=agence.id, code="01", libelle="Caisse principale",
            compte_caisse_id=compte_id,
        )
    )
    db.flush()
    return agence


def _utilisateur(db: Session, agence: Agency, suffixe: str, role_code: str = "CAISSIER") -> User:
    role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
    user = User(
        matricule=f"MAT-CA3-{suffixe}", email=f"ca3{suffixe}@ex.com", username=f"ca3{suffixe}",
        password_hash=hasher_mot_de_passe("Motdepasse!123"),
        last_name="Test", first_name=suffixe,
        primary_agency_id=agence.id,
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.flush()
    return user


def _poste_principal(db: Session, agence: Agency) -> Poste:
    return db.execute(
        select(Poste).where(Poste.agency_id == agence.id, Poste.code == "01")
    ).scalar_one()


def _courant(user: User, agence: Agency, role_code: str = "CAISSIER") -> UtilisateurCourant:
    permissions = {
        "CAISSIER": frozenset(
            {"caisse.session.open", "caisse.session.close", "caisse.session.read"}
        ),
        "RESPONSABLE_AGENCE": frozenset(
            {"caisse.session.read.autres", "caisse.session.valider", "compta.plan.manage"}
        ),
    }[role_code]
    return UtilisateurCourant(
        user_id=user.id, roles=(role_code,), permissions=permissions,
        primary_agency_id=agence.id, agency_id=agence.id, voit_tout=False,
    )


def _entete(user: User, agence: Agency, role_code: str) -> dict[str, str]:
    jeton = creer_access_token(
        user_id=user.id, roles=[role_code], primary_agency_id=agence.id, agency_id=agence.id
    )
    return {"Authorization": f"Bearer {jeton}"}


def _ouvrir(db: Session, courant: UtilisateurCourant, agence: Agency, *, fonds_initial: int = 0):
    poste = _poste_principal(db, agence)
    db.add(PosteAssignation(poste_id=poste.id, user_id=courant.user_id))
    db.flush()
    return ouvrir_session(db, courant, poste_id=poste.id, fonds_initial=fonds_initial)


def _lignes_piece(db: Session, description_like: str) -> set[tuple[str, str, int]]:
    return {
        (n, s, m)
        for n, s, m in db.execute(
            text(
                "SELECT a.account_number, jl.side, jl.amount "
                "FROM comptabilite.journal_lines jl "
                "JOIN comptabilite.journal_entries je ON je.id = jl.entry_id "
                "JOIN comptabilite.accounts a ON a.id = jl.account_id "
                "WHERE je.description LIKE :d"
            ),
            {"d": f"%{description_like}%"},
        )
    }


# --- L'écriture : D/C selon manquant ou excédent, jamais de ligne à montant nul --------------


def test_validation_manquant_pose_d_ecart_c_caisse(db: Session) -> None:
    agence = _agence(db, "CA3A1")
    caissier = _courant(_utilisateur(db, agence, "1"), agence)
    responsable = _courant(
        _utilisateur(db, agence, "2", "RESPONSABLE_AGENCE"), agence, "RESPONSABLE_AGENCE"
    )
    session = _ouvrir(db, caissier, agence, fonds_initial=10_000)
    db.flush()
    fermer_session(db, caissier, session.id, montant_reel=9_000, motif="Test manquant")

    valider_ecart(db, responsable, session.id)

    lignes = _lignes_piece(db, str(session.id))
    assert ("6099", "D", 1_000) in lignes
    assert ("101111", "C", 1_000) in lignes
    assert len(lignes) == 2  # rien d'autre, montant identique des deux côtés


def test_validation_excedent_pose_d_caisse_c_ecart(db: Session) -> None:
    agence = _agence(db, "CA3A2")
    caissier = _courant(_utilisateur(db, agence, "3"), agence)
    responsable = _courant(
        _utilisateur(db, agence, "4", "RESPONSABLE_AGENCE"), agence, "RESPONSABLE_AGENCE"
    )
    session = _ouvrir(db, caissier, agence, fonds_initial=10_000)
    db.flush()
    fermer_session(db, caissier, session.id, montant_reel=11_000, motif="Test excédent")

    valider_ecart(db, responsable, session.id)

    lignes = _lignes_piece(db, str(session.id))
    assert ("101111", "D", 1_000) in lignes
    assert ("6099", "D", 1_000) not in lignes  # jamais le compte du manquant pour un excédent
    manquant_compte = db.execute(
        text(
            "SELECT a.account_number FROM comptabilite.journal_lines jl "
            "JOIN comptabilite.journal_entries je ON je.id = jl.entry_id "
            "JOIN comptabilite.accounts a ON a.id = jl.account_id "
            "WHERE je.description LIKE :d AND jl.side = 'C'"
        ),
        {"d": f"%{session.id}%"},
    ).scalar_one()
    assert manquant_compte == "7099"


def test_validation_pose_lecriture_sur_le_journal_od(db: Session) -> None:
    agence = _agence(db, "CA3A3")
    caissier = _courant(_utilisateur(db, agence, "5"), agence)
    responsable = _courant(
        _utilisateur(db, agence, "6", "RESPONSABLE_AGENCE"), agence, "RESPONSABLE_AGENCE"
    )
    session = _ouvrir(db, caissier, agence, fonds_initial=10_000)
    db.flush()
    fermer_session(db, caissier, session.id, montant_reel=9_000, motif="Test")

    valider_ecart(db, responsable, session.id)

    journal = db.execute(
        text(
            "SELECT j.code FROM comptabilite.journal_entries je "
            "JOIN comptabilite.journals j ON j.id = je.journal_id "
            "WHERE je.description LIKE :d"
        ),
        {"d": f"%{session.id}%"},
    ).scalar_one()
    assert journal == "OD"  # régularisation, jamais le journal CA (pas un mouvement client)


def test_poser_ecriture_ecart_rend_none_si_ecart_nul() -> None:
    """Défensif : même si `valider_ecart` ne peut structurellement pas atteindre ce cas (un
    écart nul n'est jamais « à valider »), la fonction elle-même ne pose jamais de ligne à
    montant nul — vérifié directement, sans passer par le service."""
    from app.modules.caisse.ecart_operations import poser_ecriture_ecart
    from app.modules.caisse.models import CaisseSession

    session = CaisseSession(
        agency_id=uuid.uuid4(), caissier_id=uuid.uuid4(), poste_id=uuid.uuid4(),
        compte_caisse_id=uuid.uuid4(), fonds_initial=0, status="fermee", ecart=0,
    )
    config = CaisseParametres(seuil_tolerance=500)
    # `db` n'est jamais interrogée avant le contrôle ecart == 0 -> None immédiat, aucune requête.
    assert poser_ecriture_ecart(None, session, config, par=None) is None  # type: ignore[arg-type]


# --- LE garde-fou central : rattachement manquant, refus propre, pas un plantage ------------


def test_validation_refusee_si_compte_manquant_non_rattache(db: Session) -> None:
    """LE test le plus important de ce bloc : le comptable n'a pas encore rattaché le compte du
    manquant — la validation doit être REFUSÉE proprement (422, RattachementEcartManquantError),
    jamais une exception non gérée, et la session ne doit PAS être marquée validée."""
    agence = _agence(db, "CA3B1")
    caissier = _courant(_utilisateur(db, agence, "10"), agence)
    responsable = _courant(
        _utilisateur(db, agence, "11", "RESPONSABLE_AGENCE"), agence, "RESPONSABLE_AGENCE"
    )
    session = _ouvrir(db, caissier, agence, fonds_initial=10_000)
    db.flush()
    fermer_session(db, caissier, session.id, montant_reel=9_000, motif="Test")

    # Dérattache le compte du MANQUANT — dans la transaction du test seulement (savepoint,
    # jamais commité), la config partagée par le reste de la base de dev n'est pas touchée.
    db.execute(text("UPDATE caisse.parametres SET compte_ecart_manquant_id = NULL"))
    db.flush()

    with pytest.raises(RattachementEcartManquantError, match="manquant"):
        valider_ecart(db, responsable, session.id)

    # RIEN n'a bougé : ni la trace de validation, ni une écriture orpheline.
    db.refresh(session)
    assert session.valide_le is None
    assert session.valide_par is None
    assert _lignes_piece(db, str(session.id)) == set()


def test_validation_refusee_si_compte_excedent_non_rattache(db: Session) -> None:
    """Miroir du test précédent, côté excédent — les deux comptes sont vérifiés
    indépendamment, pas un seul contrôle générique qui masquerait lequel manque."""
    agence = _agence(db, "CA3B2")
    caissier = _courant(_utilisateur(db, agence, "12"), agence)
    responsable = _courant(
        _utilisateur(db, agence, "13", "RESPONSABLE_AGENCE"), agence, "RESPONSABLE_AGENCE"
    )
    session = _ouvrir(db, caissier, agence, fonds_initial=10_000)
    db.flush()
    fermer_session(db, caissier, session.id, montant_reel=11_000, motif="Test")

    db.execute(text("UPDATE caisse.parametres SET compte_ecart_excedent_id = NULL"))
    db.flush()

    with pytest.raises(RattachementEcartManquantError, match="excédent"):
        valider_ecart(db, responsable, session.id)

    db.refresh(session)
    assert session.valide_le is None


def test_validation_reussit_apres_rattachement_tardif(db: Session) -> None:
    """La suite logique du garde-fou : une fois le compte rattaché par le comptable, la MÊME
    session, jamais marquée validée par la tentative refusée, peut être validée normalement."""
    agence = _agence(db, "CA3B3")
    caissier = _courant(_utilisateur(db, agence, "14"), agence)
    responsable = _courant(
        _utilisateur(db, agence, "15", "RESPONSABLE_AGENCE"), agence, "RESPONSABLE_AGENCE"
    )
    session = _ouvrir(db, caissier, agence, fonds_initial=10_000)
    db.flush()
    fermer_session(db, caissier, session.id, montant_reel=9_000, motif="Test")

    db.execute(text("UPDATE caisse.parametres SET compte_ecart_manquant_id = NULL"))
    db.flush()
    with pytest.raises(RattachementEcartManquantError):
        valider_ecart(db, responsable, session.id)

    # Le comptable rattache (COALESCE : remet 6099).
    db.execute(
        text(
            "UPDATE caisse.parametres SET compte_ecart_manquant_id = "
            "(SELECT id FROM comptabilite.accounts WHERE account_number = '6099')"
        )
    )
    db.flush()

    resultat = valider_ecart(db, responsable, session.id)
    assert resultat.valide_le is not None
    assert ("6099", "D", 1_000) in _lignes_piece(db, str(session.id))


def test_validation_refusee_si_aucun_parametrage(db: Session) -> None:
    """Cas extrême : `caisse.parametres` elle-même n'a aucune ligne (installation jamais
    seedée). Refus propre aussi, pas une exception SQL brute (scalar_one_or_none, pas
    scalar_one)."""
    agence = _agence(db, "CA3B4")
    caissier = _courant(_utilisateur(db, agence, "16"), agence)
    responsable = _courant(
        _utilisateur(db, agence, "17", "RESPONSABLE_AGENCE"), agence, "RESPONSABLE_AGENCE"
    )
    session = _ouvrir(db, caissier, agence, fonds_initial=10_000)
    db.flush()
    fermer_session(db, caissier, session.id, montant_reel=9_000, motif="Test")

    db.execute(text("DELETE FROM caisse.parametres"))
    db.flush()

    with pytest.raises(RattachementEcartManquantError):
        valider_ecart(db, responsable, session.id)


# --- API : le refus remonte en 422, jamais un 500 -------------------------------------------


def test_api_validation_422_si_compte_non_rattache(client: TestClient, db: Session) -> None:
    agence = _agence(db, "CA3C1")
    caissier = _courant(_utilisateur(db, agence, "20"), agence)
    responsable_user = _utilisateur(db, agence, "21", "RESPONSABLE_AGENCE")
    session = _ouvrir(db, caissier, agence, fonds_initial=10_000)
    db.flush()
    fermer_session(db, caissier, session.id, montant_reel=9_000, motif="Test")
    db.execute(text("UPDATE caisse.parametres SET compte_ecart_manquant_id = NULL"))
    db.commit()

    reponse = client.post(
        f"/caisse/sessions/{session.id}/validation-ecart",
        headers=_entete(responsable_user, agence, "RESPONSABLE_AGENCE"),
    )

    assert reponse.status_code == 422
    assert "comptable" in reponse.json()["detail"].lower()
