"""Personnes liées aux PM et groupements, risque hérité, profil PM/groupement (lot PM/groupements).

Ce que ces tests protègent, par ordre d'importance :

  1. LE RISQUE REMONTE. Le niveau d'une PM est le plus haut niveau de ses personnes liées en
     cours ; il suit chaque ajout, retrait et changement de niveau d'une personne liée (une PPE au
     bureau relève la PM sans qu'on touche à la PM).
  2. LE GATE. Une PM n'est activable qu'avec un représentant légal en cours, et quand TOUTES ses
     personnes liées en cours sont actives (chacune nommée).
  3. LES RÈGLES DU LIEN. Personne physique uniquement, bénéficiaire effectif réservé aux PM avec
     pourcentage, somme <= 100, dernier représentant protégé, date de début métier obligatoire.
  4. L'AUTORISATION. Permission absente -> 403 ; fiche ou lien d'une autre fiche/agence -> 404.
  5. LA TRACE. Ajout, modification, retrait et changement de profil sont audités (anciennes et
     nouvelles valeurs), le retrait est logique avec motif.
"""

import uuid
from collections.abc import Generator
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.cli.seed_conformite_lbcft import executer_seed_lbcft
from app.core.database import engine, get_db
from app.main import app
from app.modules.comptabilite import journee
from app.modules.comptabilite.journee import journee_de_reference
from app.modules.comptabilite.models import Exercice
from app.modules.parameters.models import Agency
from app.modules.security.jwt import creer_access_token
from app.modules.security.models import Role, User, UserRole
from app.modules.security.password import hasher_mot_de_passe
from app.modules.tiers.models import (
    GroupProfile,
    IndividualProfile,
    LegalEntityProfile,
    TierRelatedPerson,
)

pytestmark = pytest.mark.integration

DEBUT = "2024-01-15"


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


def _ref(db: Session, sql: str, code: str) -> uuid.UUID:
    return db.execute(text(sql), {"c": code}).scalar_one()


def _pays(db: Session, code: str) -> uuid.UUID:
    return _ref(db, "SELECT id FROM parameters.countries WHERE code = :c", code)


def _agence(db: Session, nom: str = "Agence Centre") -> Agency:
    agence = Agency(code=f"AG-{uuid.uuid4().hex[:6]}", name=nom)
    db.add(agence)
    db.flush()
    return agence


def _utilisateur(db: Session, role_code: str, agence: Agency) -> User:
    role = db.execute(select(Role).where(Role.code == role_code)).scalar_one()
    s = uuid.uuid4().hex[:8]
    user = User(
        matricule=f"MAT-{s}",
        email=f"{s}@example.com",
        username=f"u{s}",
        password_hash=hasher_mot_de_passe("Motdepasse!123"),
        last_name="Test",
        first_name="Agent",
        primary_agency_id=agence.id,
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.flush()
    return user


def _entete(user: User, role_code: str) -> dict[str, str]:
    jeton = creer_access_token(
        user_id=user.id, roles=[role_code], primary_agency_id=user.primary_agency_id
    )
    return {"Authorization": f"Bearer {jeton}"}


def _hier(db: Session) -> date:
    """La veille de la journée comptable de référence (jamais de la date civile)."""
    return journee_de_reference(db) - timedelta(days=1)


ROLE = "RESPONSABLE_AGENCE"  # tiers.read + tiers.update, cloisonné à son agence


class Cadre:
    """Une agence, un agent qui écrit, et des fabriques de fiches."""

    def __init__(self, db: Session, client: TestClient) -> None:
        self.db = db
        self.client = client
        self.agence = _agence(db)
        self.agent = _utilisateur(db, ROLE, self.agence)
        self.entete = _entete(self.agent, ROLE)

    def societe(self, statut: str = "prospect") -> LegalEntityProfile:
        tier = LegalEntityProfile(
            tier_number=f"M-2999-{uuid.uuid4().int % 10_000_000:07d}",
            primary_agency_id=self.agence.id,
            legal_name="SARL Exemple",
            legal_form="SARL",
            constitution_date=date(2020, 1, 1),
            headquarters_country_id=_pays(self.db, "SN"),
            status=statut,
        )
        self.db.add(tier)
        self.db.flush()
        return tier

    def groupement(self) -> GroupProfile:
        tier = GroupProfile(
            tier_number=f"M-2999-{uuid.uuid4().int % 10_000_000:07d}",
            primary_agency_id=self.agence.id,
            group_name="Groupement Exemple",
            group_type="tontine",
            constitution_date=date(2020, 1, 1),
        )
        self.db.add(tier)
        self.db.flush()
        return tier

    def personne(self, nom: str = "Boubacar", *, ppe: bool = False) -> IndividualProfile:
        """Personne physique SCORÉE par le vrai chemin (PATCH /kyc) : faible, ou élevé si PPE."""
        tier = IndividualProfile(
            tier_number=f"M-2999-{uuid.uuid4().int % 10_000_000:07d}",
            primary_agency_id=self.agence.id,
            last_name=nom.upper(),
            first_name="Idrissa",
            birth_date=date(1985, 3, 2),
            gender="M",
            nationality_id=_pays(self.db, "SN"),
        )
        self.db.add(tier)
        self.db.flush()
        reponse = self.kyc(tier.id, ppe=ppe)
        assert reponse.status_code == 200, reponse.text
        return tier

    def kyc(self, tier_id: uuid.UUID, *, ppe: bool):
        secteur = _ref(
            self.db, "SELECT id FROM parameters.secteurs_activite WHERE code = :c", "AGRICULTURE"
        )
        corps = {
            "origine_fonds": "Salaire",
            "secteur_activite_id": str(secteur),
            "ppe_status": ppe,
            "ppe_relation": "direct" if ppe else None,
            "ppe_fonction": "Maire" if ppe else None,
            "mode_entree_relation": "presentiel",
        }
        return self.client.patch(f"/tiers/{tier_id}/kyc", json=corps, headers=self.entete)

    def lier(
        self, tier_id: uuid.UUID, personne_id: uuid.UUID, role: str = "representant_legal", **plus
    ):
        corps = {"individual_tier_id": str(personne_id), "role": role, "date_debut": DEBUT, **plus}
        return self.client.post(
            f"/tiers/{tier_id}/personnes-liees", json=corps, headers=self.entete
        )

    def lier_id(
        self, tier_id: uuid.UUID, personne_id: uuid.UUID, role: str = "representant_legal", **plus
    ) -> str:
        """Ajoute un lien et rend son identifiant (l'ordre des lignes n'est pas garanti)."""
        reponse = self.lier(tier_id, personne_id, role, **plus)
        assert reponse.status_code == 201, reponse.text
        return next(
            i["id"]
            for i in reponse.json()["items"]
            if i["individual_tier_id"] == str(personne_id) and i["role"] == role
        )

    def lire(self, tier_id: uuid.UUID):
        return self.client.get(f"/tiers/{tier_id}/personnes-liees", headers=self.entete)

    def conditions(self, tier_id: uuid.UUID) -> list[str]:
        reponse = self.client.get(f"/tiers/{tier_id}/activation-conditions", headers=self.entete)
        assert reponse.status_code == 200, reponse.text
        return [c["code"] for c in reponse.json()["conditions"]]


@pytest.fixture
def cadre(db: Session, client: TestClient) -> Cadre:
    return Cadre(db, client)


def _audit(db: Session, action: str, ressource: uuid.UUID) -> list:
    return list(
        db.execute(
            text(
                "SELECT old_values, new_values FROM audit.audit_logs "
                "WHERE action = :a AND resource_id = :r"
            ),
            {"a": action, "r": ressource},
        )
    )


# --- 1. les règles du lien ----------------------------------------------------------------


def test_un_representant_legal_s_ajoute_et_laisse_une_trace(cadre: Cadre) -> None:
    societe, personne = cadre.societe(), cadre.personne()

    reponse = cadre.lier(societe.id, personne.id, fonction="Gérant", pouvoir_signature=True)

    assert reponse.status_code == 201, reponse.text
    item = reponse.json()["items"][0]
    assert item["role"] == "representant_legal"
    assert item["nom"] == "BOUBACAR Idrissa"
    assert item["pouvoir_signature"] is True
    assert item["date_debut"] == DEBUT
    assert item["en_cours"] is True
    lien_id = uuid.UUID(item["id"])
    ajout = _audit(cadre.db, "tier.related_person.added", lien_id)
    assert len(ajout) == 1
    assert ajout[0].old_values is None
    assert ajout[0].new_values["role"] == "representant_legal"
    evenements = (
        cadre.db.execute(
            text(
                "SELECT reason FROM tiers.lifecycle_events "
                "WHERE tier_id = :t AND event_type = 'updated'"
            ),
            {"t": societe.id},
        )
        .scalars()
        .all()
    )
    assert "Personne liée ajoutée" in evenements


def test_la_date_de_debut_est_obligatoire_et_jamais_la_date_de_saisie(cadre: Cadre) -> None:
    societe, personne = cadre.societe(), cadre.personne()

    reponse = cadre.client.post(
        f"/tiers/{societe.id}/personnes-liees",
        json={"individual_tier_id": str(personne.id), "role": "representant_legal"},
        headers=cadre.entete,
    )

    assert reponse.status_code == 422
    assert cadre.lire(societe.id).json()["items"] == []  # rien n'a été écrit


def test_un_beneficiaire_effectif_exige_un_pourcentage(cadre: Cadre) -> None:
    societe, personne = cadre.societe(), cadre.personne()

    sans = cadre.lier(societe.id, personne.id, "beneficiaire_effectif")
    avec = cadre.lier(societe.id, personne.id, "beneficiaire_effectif", pourcentage_detention="30")
    autre_role = cadre.lier(societe.id, personne.id, "dirigeant", pourcentage_detention="30")

    assert sans.status_code == 422
    assert avec.status_code == 201, avec.text
    assert autre_role.status_code == 422  # un pourcentage ne se pose que sur un bénéficiaire


def test_un_groupement_n_a_pas_de_beneficiaire_effectif(cadre: Cadre) -> None:
    groupement, personne = cadre.groupement(), cadre.personne()

    reponse = cadre.lier(
        groupement.id, personne.id, "beneficiaire_effectif", pourcentage_detention="10"
    )
    bureau = cadre.lier(groupement.id, personne.id, "membre_bureau")

    assert reponse.status_code == 422
    assert reponse.json()["detail"] == "Un groupement n'a pas de capital : utilisez membre_bureau."
    assert bureau.status_code == 201, bureau.text


def test_une_personne_physique_n_a_pas_de_personnes_liees(cadre: Cadre) -> None:
    une, autre = cadre.personne("Une"), cadre.personne("Autre")

    reponse = cadre.lier(une.id, autre.id)

    assert reponse.status_code == 422
    assert reponse.json()["detail"] == "Une personne physique n'a pas de personnes liées."
    assert cadre.lire(une.id).status_code == 422


def test_la_personne_liee_doit_etre_une_personne_physique(cadre: Cadre) -> None:
    societe, autre_societe = cadre.societe(), cadre.societe()

    reponse = cadre.lier(societe.id, autre_societe.id)

    assert reponse.status_code == 422
    assert reponse.json()["detail"] == "La personne liée doit être une personne physique."


def test_la_somme_des_pourcentages_ne_depasse_pas_100(cadre: Cadre) -> None:
    societe = cadre.societe()
    a, b, c = cadre.personne("Aa"), cadre.personne("Bb"), cadre.personne("Cc")

    assert (
        cadre.lier(
            societe.id, a.id, "beneficiaire_effectif", pourcentage_detention="60"
        ).status_code
        == 201
    )
    refus = cadre.lier(societe.id, b.id, "beneficiaire_effectif", pourcentage_detention="50")
    ok = cadre.lier(societe.id, c.id, "beneficiaire_effectif", pourcentage_detention="40")

    assert refus.status_code == 422
    assert "dépasse 100" in refus.json()["detail"]
    assert ok.status_code == 201, ok.text
    assert Decimal(cadre.lire(societe.id).json()["total_beneficiaires"]) == Decimal(100)


def test_modifier_un_pourcentage_respecte_aussi_le_plafond(cadre: Cadre) -> None:
    societe = cadre.societe()
    a, b = cadre.personne("Aa"), cadre.personne("Bb")
    cadre.lier(societe.id, a.id, "beneficiaire_effectif", pourcentage_detention="60")
    lien_b = cadre.lier_id(societe.id, b.id, "beneficiaire_effectif", pourcentage_detention="30")

    refus = cadre.client.patch(
        f"/tiers/{societe.id}/personnes-liees/{lien_b}",
        json={"pourcentage_detention": "45"},
        headers=cadre.entete,
    )
    ok = cadre.client.patch(
        f"/tiers/{societe.id}/personnes-liees/{lien_b}",
        json={"pourcentage_detention": "40"},
        headers=cadre.entete,
    )

    assert refus.status_code == 422
    assert ok.status_code == 200, ok.text


def test_un_doublon_de_role_est_refuse_mais_un_lien_termine_n_empeche_rien(cadre: Cadre) -> None:
    societe, personne = cadre.societe(), cadre.personne()
    lien = cadre.lier_id(societe.id, personne.id, "dirigeant")

    doublon = cadre.lier(societe.id, personne.id, "dirigeant")
    fin = cadre.client.patch(
        f"/tiers/{societe.id}/personnes-liees/{lien}",
        json={"date_fin": str(_hier(cadre.db))},
        headers=cadre.entete,
    )
    retour = cadre.lier(
        societe.id, personne.id, "dirigeant", date_debut=str(journee_de_reference(cadre.db))
    )

    assert doublon.status_code == 422
    assert fin.status_code == 200, fin.text
    assert retour.status_code == 201, retour.text


def test_la_periode_doit_etre_coherente(cadre: Cadre) -> None:
    societe, personne = cadre.societe(), cadre.personne()

    reponse = cadre.lier(societe.id, personne.id, "dirigeant", date_fin="2023-01-01")

    assert reponse.status_code == 422


def test_la_base_impose_ses_contraintes_meme_hors_service(cadre: Cadre) -> None:
    societe, personne = cadre.societe(), cadre.personne()
    db = cadre.db

    def inserer(**champs: object) -> None:
        base = {
            "tier_id": societe.id,
            "individual_tier_id": personne.id,
            "role": "dirigeant",
            "date_debut": date(2024, 1, 1),
        }
        with db.begin_nested():
            db.add(TierRelatedPerson(**{**base, **champs}))
            db.flush()

    for invalide in (
        {"role": "gardien"},
        {"pourcentage_detention": Decimal(101), "role": "beneficiaire_effectif"},
        {"role": "beneficiaire_effectif", "pourcentage_detention": None},
        {"individual_tier_id": societe.id},
        {"date_fin": date(2023, 1, 1)},
        {"date_debut": None},
    ):
        with pytest.raises(IntegrityError):
            inserer(**invalide)

    inserer()  # un lien valide passe...
    with pytest.raises(IntegrityError):
        inserer()  # ...et son jumeau ouvert est refusé par l'index unique partiel


def test_en_cours_se_juge_sur_la_journee_comptable_pas_sur_la_date_civile(
    cadre: Cadre,
) -> None:
    """Mandat du 01/01 au 05/01/2024 : en cours tant que la journée ouverte est avant le 05/01,
    terminé dès que la journée comptable l'atteint — quelle que soit la date civile (2026)."""
    cadre.db.add(
        Exercice(
            code="PL24",
            label="Exercice 2024",
            date_debut=date(2024, 1, 1),
            date_fin=date(2024, 12, 31),
        )
    )
    cadre.db.flush()
    societe, personne = cadre.societe(), cadre.personne()
    lien = cadre.lier_id(
        societe.id, personne.id, "dirigeant", date_debut="2024-01-01", date_fin="2024-01-05"
    )

    def etat() -> dict:
        items = cadre.lire(societe.id).json()["items"]
        return next(i for i in items if i["id"] == lien)

    journee.ouvrir_journee(cadre.db, date(2024, 1, 2), None)
    pendant = etat()["en_cours"]
    risque_pendant = cadre.lire(societe.id).json()["risque"]["niveau"]
    journee.cloturer_journee(cadre.db, None)
    # Sans journée ouverte : la dernière journée clôturée sert de référence (lecture, pas de refus).
    sans_journee_ouverte = etat()["en_cours"]
    journee.ouvrir_journee(cadre.db, date(2024, 1, 8), None)
    apres = etat()["en_cours"]
    risque_apres = cadre.lire(societe.id).json()["risque"]["niveau"]

    assert pendant is True
    assert risque_pendant == "faible"
    assert sans_journee_ouverte is True
    assert apres is False  # le 08/01 est après la fin du mandat (05/01)
    assert risque_apres is None  # plus aucune personne liée en cours : pas de score


# --- 2. le dernier représentant légal -----------------------------------------------------


def test_le_dernier_representant_d_une_fiche_active_est_protege(cadre: Cadre) -> None:
    societe = cadre.societe(statut="actif")
    a, b = cadre.personne("Aa"), cadre.personne("Bb")
    lien_a = cadre.lier_id(societe.id, a.id)

    retrait = cadre.client.request(
        "DELETE",
        f"/tiers/{societe.id}/personnes-liees/{lien_a}",
        json={"motif": "Démission"},
        headers=cadre.entete,
    )
    fin = cadre.client.patch(
        f"/tiers/{societe.id}/personnes-liees/{lien_a}",
        json={"date_fin": str(_hier(cadre.db))},
        headers=cadre.entete,
    )
    cadre.lier(societe.id, b.id)  # un second représentant arrive
    apres = cadre.client.request(
        "DELETE",
        f"/tiers/{societe.id}/personnes-liees/{lien_a}",
        json={"motif": "Démission"},
        headers=cadre.entete,
    )

    assert retrait.status_code == 422
    assert "représentant légal" in retrait.json()["detail"]
    assert fin.status_code == 422
    assert apres.status_code == 200, apres.text


def test_un_prospect_peut_encore_retirer_son_representant(cadre: Cadre) -> None:
    societe, personne = cadre.societe(), cadre.personne()
    lien = cadre.lier_id(societe.id, personne.id)

    reponse = cadre.client.request(
        "DELETE",
        f"/tiers/{societe.id}/personnes-liees/{lien}",
        json={"motif": "Saisie erronée"},
        headers=cadre.entete,
    )

    assert reponse.status_code == 200, reponse.text


# --- 3. modification et retrait ----------------------------------------------------------


def test_la_modification_garde_l_ancienne_et_la_nouvelle_valeur(cadre: Cadre) -> None:
    societe, personne = cadre.societe(), cadre.personne()
    lien = {"id": cadre.lier_id(societe.id, personne.id, "dirigeant", fonction="Adjoint")}

    reponse = cadre.client.patch(
        f"/tiers/{societe.id}/personnes-liees/{lien['id']}",
        json={"fonction": "Directeur", "pouvoir_signature": True},
        headers=cadre.entete,
    )

    assert reponse.status_code == 200, reponse.text
    trace = _audit(cadre.db, "tier.related_person.updated", uuid.UUID(lien["id"]))
    assert trace[0].old_values["fonction"] == "Adjoint"
    assert trace[0].new_values["fonction"] == "Directeur"
    assert trace[0].new_values["pouvoir_signature"] is True


def test_le_retrait_est_logique_avec_motif_obligatoire(cadre: Cadre) -> None:
    societe, personne = cadre.societe(), cadre.personne()
    lien = cadre.lier_id(societe.id, personne.id, "signataire")
    url = f"/tiers/{societe.id}/personnes-liees/{lien}"

    sans_motif = cadre.client.request("DELETE", url, json={}, headers=cadre.entete)
    reponse = cadre.client.request(
        "DELETE", url, json={"motif": "Fin de mandat"}, headers=cadre.entete
    )

    assert sans_motif.status_code == 422
    assert reponse.status_code == 200, reponse.text
    assert reponse.json()["items"] == []
    ligne = cadre.db.execute(
        text(
            "SELECT deleted_at, deleted_by, deletion_reason FROM tiers.tier_related_persons "
            "WHERE id = :i"
        ),
        {"i": lien},
    ).one()
    assert ligne.deleted_at is not None
    assert ligne.deleted_by == cadre.agent.id
    assert ligne.deletion_reason == "Fin de mandat"
    assert len(_audit(cadre.db, "tier.related_person.removed", uuid.UUID(lien))) == 1


# --- 4. autorisation ---------------------------------------------------------------------


def test_sans_permission_la_lecture_et_les_ecritures_sont_refusees(cadre: Cadre) -> None:
    societe, personne = cadre.societe(), cadre.personne()
    lien = cadre.lier_id(societe.id, personne.id)
    caissier = _utilisateur(cadre.db, "CAISSIER", cadre.agence)  # tiers.read.basic seulement
    auditeur = _utilisateur(cadre.db, "AUDITEUR_INTERNE", cadre.agence)  # lit, n'écrit pas
    base = f"/tiers/{societe.id}/personnes-liees"
    corps = {"individual_tier_id": str(personne.id), "role": "dirigeant", "date_debut": DEBUT}

    h_caissier, h_auditeur = _entete(caissier, "CAISSIER"), _entete(auditeur, "AUDITEUR_INTERNE")

    assert cadre.client.get(base, headers=h_caissier).status_code == 403
    assert cadre.client.get(base).status_code == 401
    assert cadre.client.get(base, headers=h_auditeur).status_code == 200  # lecture permise
    assert cadre.client.post(base, json=corps, headers=h_auditeur).status_code == 403
    assert (
        cadre.client.patch(f"{base}/{lien}", json={"fonction": "X"}, headers=h_auditeur).status_code
        == 403
    )
    suppression = cadre.client.request(
        "DELETE", f"{base}/{lien}", json={"motif": "xxx"}, headers=h_auditeur
    )
    assert suppression.status_code == 403
    assert len(cadre.lire(societe.id).json()["items"]) == 1  # rien n'a bougé


def test_une_fiche_d_une_autre_agence_est_introuvable(cadre: Cadre) -> None:
    autre = Cadre(cadre.db, cadre.client)  # autre agence, autre agent
    societe_etrangere, personne_etrangere = autre.societe(), autre.personne()
    ma_personne = cadre.personne()

    lecture = cadre.lire(societe_etrangere.id)
    ecriture = cadre.lier(societe_etrangere.id, ma_personne.id)
    ma_societe = cadre.societe()
    personne_hors_perimetre = cadre.lier(ma_societe.id, personne_etrangere.id)

    assert lecture.status_code == 404
    assert ecriture.status_code == 404
    assert personne_hors_perimetre.status_code == 404  # n'existe pas de mon point de vue


def test_un_lien_d_une_autre_fiche_est_introuvable(cadre: Cadre) -> None:
    """IDOR : l'identifiant d'un lien n'a de sens que sous SA fiche."""
    a, b = cadre.societe(), cadre.societe()
    personne = cadre.personne()
    lien_de_a = cadre.lier_id(a.id, personne.id, "dirigeant")

    modifier = cadre.client.patch(
        f"/tiers/{b.id}/personnes-liees/{lien_de_a}", json={"fonction": "X"}, headers=cadre.entete
    )
    retirer = cadre.client.request(
        "DELETE",
        f"/tiers/{b.id}/personnes-liees/{lien_de_a}",
        json={"motif": "xxx"},
        headers=cadre.entete,
    )

    assert modifier.status_code == 404
    assert retirer.status_code == 404
    assert len(cadre.lire(a.id).json()["items"]) == 1  # le lien de A est intact


def test_la_fiche_est_verrouillee_avant_de_verifier_la_somme(cadre: Cadre) -> None:
    """La somme des pourcentages se vérifie sous FOR UPDATE sur la fiche : deux ajouts
    simultanés ne peuvent pas dépasser 100 % ensemble. Vérifié sur le SQL réellement émis."""
    societe, personne = cadre.societe(), cadre.personne()
    requetes: list[str] = []

    def ecouter(conn, cursor, statement, parameters, context, executemany):
        requetes.append(statement)

    event.listen(engine, "before_cursor_execute", ecouter)
    try:
        reponse = cadre.lier(
            societe.id, personne.id, "beneficiaire_effectif", pourcentage_detention="10"
        )
    finally:
        event.remove(engine, "before_cursor_execute", ecouter)

    assert reponse.status_code == 201, reponse.text
    verrou = next(i for i, s in enumerate(requetes) if "FOR UPDATE" in s and "tiers.tiers" in s)
    somme = next(i for i, s in enumerate(requetes) if "sum(" in s.lower() and "tier_related" in s)
    assert verrou < somme


# --- 5. le gate d'activation -------------------------------------------------------------


def test_sans_representant_legal_la_fiche_n_est_pas_activable(cadre: Cadre) -> None:
    societe = cadre.societe()

    assert "REPRESENTANT_LEGAL_MANQUANT" in cadre.conditions(societe.id)


def test_un_representant_non_actif_bloque_et_est_nomme(cadre: Cadre) -> None:
    societe, personne = cadre.societe(), cadre.personne("Boubacar")
    cadre.lier(societe.id, personne.id)

    reponse = cadre.client.get(f"/tiers/{societe.id}/activation-conditions", headers=cadre.entete)
    conditions = {c["code"]: c["libelle"] for c in reponse.json()["conditions"]}

    assert "REPRESENTANT_LEGAL_MANQUANT" not in conditions
    assert "BOUBACAR Idrissa" in conditions["PERSONNE_LIEE_NON_ACTIVE"]


def test_toutes_les_personnes_liees_en_cours_doivent_etre_actives(cadre: Cadre) -> None:
    societe = cadre.societe()
    active, en_attente = cadre.personne("Active"), cadre.personne("Attente")
    cadre.lier(societe.id, active.id)
    cadre.lier(societe.id, en_attente.id, "membre_bureau")
    active.status = "actif"
    cadre.db.flush()

    libelles = [
        c["libelle"]
        for c in cadre.client.get(
            f"/tiers/{societe.id}/activation-conditions", headers=cadre.entete
        ).json()["conditions"]
        if c["code"] == "PERSONNE_LIEE_NON_ACTIVE"
    ]

    assert len(libelles) == 1
    assert "ATTENTE" in libelles[0]  # seule la personne non active est nommée


def test_une_personne_liee_terminee_ne_bloque_plus(cadre: Cadre) -> None:
    societe = cadre.societe()
    active, ancienne = cadre.personne("Active"), cadre.personne("Ancienne")
    cadre.lier(societe.id, active.id)
    lien = cadre.lier_id(societe.id, ancienne.id, "dirigeant")
    active.status = "actif"
    cadre.db.flush()
    cadre.client.patch(
        f"/tiers/{societe.id}/personnes-liees/{lien}",
        json={"date_fin": str(_hier(cadre.db))},
        headers=cadre.entete,
    )

    assert "PERSONNE_LIEE_NON_ACTIVE" not in cadre.conditions(societe.id)


# --- 6. le risque hérité -----------------------------------------------------------------


def test_le_risque_d_une_pm_est_le_plus_haut_niveau_de_ses_personnes_liees(cadre: Cadre) -> None:
    societe = cadre.societe()
    sage, exposee = cadre.personne("Sage"), cadre.personne("Exposee", ppe=True)

    cadre.lier(societe.id, sage.id)
    seul = cadre.lire(societe.id).json()["risque"]
    cadre.lier(societe.id, exposee.id, "membre_bureau")
    avec = cadre.lire(societe.id).json()["risque"]

    assert seul["niveau"] == "faible"
    assert avec["niveau"] == "eleve"
    assert avec["origine"]["nom"] == "EXPOSEE Idrissa"
    assert avec["origine"]["role"] == "membre_bureau"
    assert avec["explication"] == "Élevé — hérité de EXPOSEE Idrissa, membre du bureau"
    assert avec["provisoire"] is True
    # (les archives d'un même test partagent l'horodatage de la transaction : on les cherche
    # par contenu, pas par « la dernière »)
    archive = cadre.db.execute(
        text(
            "SELECT is_provisional FROM tiers.risk_assessments WHERE tier_id = :t "
            "AND trigger_event = 'personne_liee_ajoutee' AND niveau_effectif = 'eleve'"
        ),
        {"t": societe.id},
    ).one()
    assert archive.is_provisional is True
    assert cadre.db.get(LegalEntityProfile, societe.id).risk_level == "eleve"


def test_le_risque_suit_le_niveau_d_une_personne_liee(cadre: Cadre) -> None:
    """Une PPE déclarée sur un dirigeant relève la PM, sans qu'on touche à la PM."""
    societe, personne = cadre.societe(), cadre.personne("Dirigeant")
    cadre.lier(societe.id, personne.id)
    assert cadre.lire(societe.id).json()["risque"]["niveau"] == "faible"

    assert cadre.kyc(personne.id, ppe=True).status_code == 200
    releve = cadre.lire(societe.id).json()["risque"]["niveau"]
    archive = cadre.db.execute(
        text(
            "SELECT count(*) FROM tiers.risk_assessments WHERE tier_id = :t "
            "AND trigger_event = 'personne_liee_modifiee' AND niveau_effectif = 'eleve'"
        ),
        {"t": societe.id},
    ).scalar_one()
    assert cadre.db.get(LegalEntityProfile, societe.id).risk_level == "eleve"  # reflet recalculé

    assert cadre.kyc(personne.id, ppe=False).status_code == 200
    redescendu = cadre.lire(societe.id).json()["risque"]["niveau"]

    assert releve == "eleve"
    assert archive == 1  # l'archive porte le déclencheur : on sait POURQUOI la PM a changé
    assert redescendu == "faible"
    assert cadre.db.get(LegalEntityProfile, societe.id).risk_level == "faible"


def test_le_retrait_d_une_personne_a_risque_fait_redescendre_la_pm(cadre: Cadre) -> None:
    societe = cadre.societe()
    sage, exposee = cadre.personne("Sage"), cadre.personne("Exposee", ppe=True)
    cadre.lier(societe.id, sage.id)
    lien = cadre.lier_id(societe.id, exposee.id, "membre_bureau")
    assert cadre.lire(societe.id).json()["risque"]["niveau"] == "eleve"

    cadre.client.request(
        "DELETE",
        f"/tiers/{societe.id}/personnes-liees/{lien}",
        json={"motif": "Départ du bureau"},
        headers=cadre.entete,
    )

    assert cadre.lire(societe.id).json()["risque"]["niveau"] == "faible"
    assert cadre.db.get(LegalEntityProfile, societe.id).risk_level == "faible"


def test_un_groupement_herite_aussi_et_sans_personne_il_n_a_pas_de_score(cadre: Cadre) -> None:
    groupement, exposee = cadre.groupement(), cadre.personne("Exposee", ppe=True)

    vide = cadre.lire(groupement.id).json()["risque"]
    cadre.lier(groupement.id, exposee.id, "membre_bureau")
    rempli = cadre.lire(groupement.id).json()["risque"]

    assert vide["niveau"] is None  # pas de score : on n'invente rien
    assert vide["origine"] is None
    assert rempli["niveau"] == "eleve"


def test_une_pm_a_risque_eleve_exige_le_responsable_lbcft(cadre: Cadre) -> None:
    """Le niveau hérité alimente le gate existant : seul LBC/FT active un profil élevé."""
    societe, exposee = cadre.societe(), cadre.personne("Exposee", ppe=True)
    cadre.lier(societe.id, exposee.id)

    reponse = cadre.client.post(f"/tiers/{societe.id}/activate", headers=cadre.entete)

    assert reponse.status_code == 412
    codes = [c["code"] for c in reponse.json()["detail"]["conditions_manquantes"]]
    assert "VALIDEUR_INSUFFISANT" in codes


def test_le_badge_de_la_fiche_lit_le_reflet_et_non_la_derniere_archive(cadre: Cadre) -> None:
    """Une PM dont la dernière personne liée évaluée est retirée n'a plus de niveau : le badge
    affiche « non évalué » (nul), même s'il reste une archive plus ancienne."""
    societe, personne = cadre.societe(), cadre.personne()
    lien = cadre.lier_id(societe.id, personne.id)
    avant = cadre.client.get(f"/tiers/{societe.id}", headers=cadre.entete).json()["risk_level"]

    cadre.client.request(
        "DELETE",
        f"/tiers/{societe.id}/personnes-liees/{lien}",
        json={"motif": "Saisie erronée"},
        headers=cadre.entete,
    )
    apres = cadre.client.get(f"/tiers/{societe.id}", headers=cadre.entete).json()

    archives = cadre.db.execute(
        text("SELECT count(*) FROM tiers.risk_assessments WHERE tier_id = :t"), {"t": societe.id}
    ).scalar_one()
    assert avant == "faible"
    assert archives >= 1  # l'archive ancienne existe toujours
    assert apres["risk_level"] is None
    assert apres["risk_provisional"] is False


def test_deux_archives_d_une_meme_transaction_ont_des_horodatages_croissants(
    cadre: Cadre,
) -> None:
    """`assessed_at` vaut l'instant réel de l'insertion (clock_timestamp), pas le début de la
    transaction : « la dernière archive » est donc déterministe."""
    personne = cadre.personne("Horloge")
    assert cadre.kyc(personne.id, ppe=True).status_code == 200
    assert cadre.kyc(personne.id, ppe=False).status_code == 200

    lignes = cadre.db.execute(
        text(
            "SELECT assessed_at, niveau_effectif FROM tiers.risk_assessments "
            "WHERE tier_id = :t ORDER BY assessed_at"
        ),
        {"t": personne.id},
    ).all()

    horodatages = [ligne.assessed_at for ligne in lignes]
    assert len(horodatages) == 3  # création du score, PPE, retour à la normale
    assert horodatages == sorted(set(horodatages))  # tous distincts, donc ordonnés
    assert [ligne.niveau_effectif for ligne in lignes] == ["faible", "eleve", "faible"]


def test_la_creation_accepte_les_champs_du_profil_complet(cadre: Cadre) -> None:
    pays = _pays(cadre.db, "NE")
    societe = cadre.client.post(
        "/tiers/legal-entities",
        json={
            "legal_name": "TANDA SERVICES",
            "commercial_name": "Tanda",
            "legal_form": "SARL",
            "rccm_number": "NI-NIA-2020-B-0042",
            "nif_number": "NIF-123",
            "constitution_date": "2020-03-15",
            "business_purpose": "Négoce de céréales",
            "headquarters_country_id": str(pays),
        },
        headers=cadre.entete,
    )
    groupement = cadre.client.post(
        "/tiers/groups",
        json={
            "group_name": "HASKE",
            "group_type": "tontine",
            "constitution_date": "2019-06-01",
            "intervention_zone": "Commune de Tahoua",
            "group_purpose": "Épargne et entraide",
            "expected_member_count": 25,
        },
        headers=cadre.entete,
    )

    assert societe.status_code == 201, societe.text
    pm = societe.json()["personne_morale"]
    assert (pm["commercial_name"], pm["rccm_number"], pm["nif_number"]) == (
        "Tanda",
        "NI-NIA-2020-B-0042",
        "NIF-123",
    )
    assert pm["business_purpose"] == "Négoce de céréales"
    assert groupement.status_code == 201, groupement.text
    g = groupement.json()["groupement"]
    assert (g["intervention_zone"], g["group_purpose"], g["expected_member_count"]) == (
        "Commune de Tahoua",
        "Épargne et entraide",
        25,
    )


# --- 7. le seuil LBC/FT ------------------------------------------------------------------


def test_sans_parametre_aucun_indicateur_de_seuil(cadre: Cadre) -> None:
    societe, personne = cadre.societe(), cadre.personne()
    cadre.db.execute(text("DELETE FROM tiers.parametres_lbcft"))
    cadre.lier(societe.id, personne.id, "beneficiaire_effectif", pourcentage_detention="80")

    vue = cadre.lire(societe.id).json()

    assert vue["seuil"] is None
    assert vue["items"][0]["atteint_le_seuil"] is False


def test_le_seuil_provisoire_signale_sans_bloquer(cadre: Cadre) -> None:
    societe = cadre.societe()
    gros, petit = cadre.personne("Gros"), cadre.personne("Petit")
    cadre.db.execute(text("DELETE FROM tiers.parametres_lbcft"))
    assert executer_seed_lbcft(cadre.db) is True
    cadre.lier(societe.id, gros.id, "beneficiaire_effectif", pourcentage_detention="25")
    cadre.lier(societe.id, petit.id, "beneficiaire_effectif", pourcentage_detention="24.99")

    vue = cadre.lire(societe.id).json()
    par_nom = {i["nom"]: i for i in vue["items"]}

    assert Decimal(vue["seuil"]) == Decimal(25)
    assert vue["seuil_provisoire"] is True
    assert par_nom["GROS Idrissa"]["atteint_le_seuil"] is True
    assert par_nom["PETIT Idrissa"]["atteint_le_seuil"] is False  # informatif : rien n'est refusé


def test_le_seed_du_seuil_est_idempotent_et_ne_reecrit_jamais(db: Session) -> None:
    db.execute(text("DELETE FROM tiers.parametres_lbcft"))

    premier = executer_seed_lbcft(db)
    db.execute(
        text(
            "UPDATE tiers.parametres_lbcft "
            "SET seuil_beneficiaire_effectif = 33, is_provisional = FALSE"
        )
    )
    second = executer_seed_lbcft(db)
    ligne = db.execute(
        text("SELECT seuil_beneficiaire_effectif, is_provisional FROM tiers.parametres_lbcft")
    ).one()

    assert premier is True
    assert second is False
    assert ligne.seuil_beneficiaire_effectif == Decimal(33)  # la valeur corrigée survit
    assert ligne.is_provisional is False


def test_la_base_impose_une_seule_ligne_et_un_seuil_valide(db: Session) -> None:
    db.execute(text("DELETE FROM tiers.parametres_lbcft"))
    for sql in (
        "INSERT INTO tiers.parametres_lbcft (id, seuil_beneficiaire_effectif) VALUES (2, 25)",
        "INSERT INTO tiers.parametres_lbcft (id, seuil_beneficiaire_effectif) VALUES (1, 0)",
        "INSERT INTO tiers.parametres_lbcft (id, seuil_beneficiaire_effectif) VALUES (1, 101)",
    ):
        with pytest.raises(IntegrityError), db.begin_nested():
            db.execute(text(sql))


# --- 8. le profil d'une PM ou d'un groupement --------------------------------------------


def test_le_profil_d_une_pm_se_modifie_avec_audit(cadre: Cadre) -> None:
    societe = cadre.societe()

    reponse = cadre.client.patch(
        f"/tiers/{societe.id}/legal-entity",
        json={
            "legal_name": "SARL Nouvelle",
            "rccm_number": " NI-NIA-2020-B-0042 ",
            "capital_amount": "1000000",
        },
        headers=cadre.entete,
    )

    assert reponse.status_code == 200, reponse.text
    pm = reponse.json()["personne_morale"]
    assert pm["legal_name"] == "SARL Nouvelle"
    assert pm["rccm_number"] == "NI-NIA-2020-B-0042"
    assert pm["legal_form"] == "SARL"  # ce qui n'est pas envoyé ne bouge pas
    trace = _audit(cadre.db, "tier.profile.updated", societe.id)
    assert trace[0].old_values == {
        "legal_name": "SARL Exemple",
        "rccm_number": None,
        "capital_amount": None,
    }
    assert trace[0].new_values["legal_name"] == "SARL Nouvelle"
    assert trace[0].new_values["rccm_number"] == "NI-NIA-2020-B-0042"


def test_un_profil_inchange_n_ecrit_aucune_trace(cadre: Cadre) -> None:
    societe = cadre.societe()

    reponse = cadre.client.patch(
        f"/tiers/{societe.id}/legal-entity", json={"legal_form": "SARL"}, headers=cadre.entete
    )

    assert reponse.status_code == 200
    assert _audit(cadre.db, "tier.profile.updated", societe.id) == []


def test_un_rccm_ou_un_nif_deja_pris_est_signale_clairement(cadre: Cadre) -> None:
    premiere, seconde = cadre.societe(), cadre.societe()
    ok = cadre.client.patch(
        f"/tiers/{premiere.id}/legal-entity",
        json={"rccm_number": "NI-NIA-1", "nif_number": "NIF-1"},
        headers=cadre.entete,
    )

    rccm = cadre.client.patch(
        f"/tiers/{seconde.id}/legal-entity", json={"rccm_number": "NI-NIA-1"}, headers=cadre.entete
    )
    nif = cadre.client.patch(
        f"/tiers/{seconde.id}/legal-entity", json={"nif_number": "NIF-1"}, headers=cadre.entete
    )

    assert ok.status_code == 200, ok.text
    assert rccm.status_code == 422
    assert rccm.json()["detail"] == "Ce numéro RCCM est déjà rattaché à une autre fiche."
    assert nif.status_code == 422
    assert nif.json()["detail"] == "Ce NIF est déjà rattaché à une autre fiche."
    assert cadre.db.get(LegalEntityProfile, seconde.id).rccm_number is None  # rien d'écrit


def test_un_champ_obligatoire_ne_se_vide_pas_et_un_champ_inconnu_est_rejete(cadre: Cadre) -> None:
    societe = cadre.societe()
    url = f"/tiers/{societe.id}/legal-entity"

    assert (
        cadre.client.patch(url, json={"legal_name": None}, headers=cadre.entete).status_code == 422
    )
    assert (
        cadre.client.patch(url, json={"tier_type": "group"}, headers=cadre.entete).status_code
        == 422
    )
    assert (
        cadre.client.patch(url, json={"legal_form": "SCI"}, headers=cadre.entete).status_code == 422
    )
    vider = cadre.client.patch(url, json={"commercial_name": None}, headers=cadre.entete)
    assert vider.status_code == 200  # un champ facultatif, lui, peut être vidé


def test_un_pays_inexistant_est_refuse(cadre: Cadre) -> None:
    societe = cadre.societe()

    reponse = cadre.client.patch(
        f"/tiers/{societe.id}/legal-entity",
        json={"headquarters_country_id": str(uuid.uuid4())},
        headers=cadre.entete,
    )

    assert reponse.status_code == 422
    assert "pays ou devise" in reponse.json()["detail"]


def test_le_profil_d_un_groupement_se_modifie_et_le_type_est_controle(cadre: Cadre) -> None:
    groupement, societe = cadre.groupement(), cadre.societe()

    reponse = cadre.client.patch(
        f"/tiers/{groupement.id}/group",
        json={"group_name": "Tontine des femmes", "expected_member_count": 25},
        headers=cadre.entete,
    )
    mauvais_type = cadre.client.patch(
        f"/tiers/{societe.id}/group", json={"group_name": "X"}, headers=cadre.entete
    )
    mauvais_type_pm = cadre.client.patch(
        f"/tiers/{groupement.id}/legal-entity", json={"legal_name": "X"}, headers=cadre.entete
    )

    assert reponse.status_code == 200, reponse.text
    assert reponse.json()["groupement"]["group_name"] == "Tontine des femmes"
    assert reponse.json()["groupement"]["expected_member_count"] == 25
    assert mauvais_type.status_code == 422
    assert mauvais_type.json()["detail"] == "Cette fiche n'est pas un groupement."
    assert mauvais_type_pm.status_code == 422


def test_le_profil_exige_la_permission_et_le_perimetre(cadre: Cadre) -> None:
    societe = cadre.societe()
    auditeur = _utilisateur(cadre.db, "AUDITEUR_INTERNE", cadre.agence)
    autre = Cadre(cadre.db, cadre.client)
    corps = {"legal_name": "Pirate"}

    sans_droit = cadre.client.patch(
        f"/tiers/{societe.id}/legal-entity",
        json=corps,
        headers=_entete(auditeur, "AUDITEUR_INTERNE"),
    )
    hors_agence = cadre.client.patch(
        f"/tiers/{societe.id}/legal-entity", json=corps, headers=autre.entete
    )
    anonyme = cadre.client.patch(f"/tiers/{societe.id}/legal-entity", json=corps)

    assert sans_droit.status_code == 403
    assert hors_agence.status_code == 404
    assert anonyme.status_code == 401
    assert cadre.db.get(LegalEntityProfile, societe.id).legal_name == "SARL Exemple"
