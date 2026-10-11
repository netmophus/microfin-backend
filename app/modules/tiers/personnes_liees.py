"""Personnes liées aux personnes morales et groupements (lot PM/groupements).

Une PM ou un groupement agit par des personnes physiques : représentant légal, dirigeants, membres
du bureau, bénéficiaires effectifs, signataires. Ce module tient ces liens (table
`tiers.tier_related_persons`) et leurs règles :

  - le LIEN part d'une PM/d'un groupement vers une PERSONNE PHYSIQUE (le typage est contrôlé ici,
    un CHECK ne lit pas une autre table) ; une personne physique n'a pas de personnes liées ;
  - `beneficiaire_effectif` est réservé aux PM (un groupement n'a pas de capital) et exige un
    pourcentage ; la somme des pourcentages en cours ne dépasse pas 100 (fiche verrouillée) ;
  - une PM/un groupement déjà sorti du statut prospect garde toujours un représentant légal en
    cours : on refuse de terminer ou retirer le dernier ;
  - le seuil de détention qui signale un bénéficiaire effectif est un PARAMÈTRE provisoire
    (`tiers.parametres_lbcft`), jamais une constante ; sans ligne, aucun indicateur ;
  - chaque ajout, modification ou retrait RECALCULE le risque hérité de la PM (cf. `risque.py`),
    est tracé (LifecycleEvent + audit avec anciennes et nouvelles valeurs) puis committé.

Périmètre : fiche hors périmètre ou inconnue -> 404 (jamais 403) ; lien d'une AUTRE fiche -> 404
(la route porte les deux identifiants, aucun ne se devine seul).
"""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.audit.service import ContexteRequete, ecrire_audit
from app.modules.comptabilite.journee import journee_de_reference
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.tiers.models import (
    IndividualProfile,
    LifecycleEvent,
    ParametresLbcft,
    Tier,
    TierRelatedPerson,
    liens_en_cours,
)
from app.modules.tiers.risque import evaluer_et_enregistrer, evaluer_pm

RESSOURCE = "tier_related_person"
_AGENCE = Tier.__table__.c.primary_agency_id
PLAFOND_POURCENTAGE = Decimal(100)
STATUT_PROSPECT = "prospect"

MESSAGE_PAS_DE_LIEN_PP = "Une personne physique n'a pas de personnes liées."
MESSAGE_BE_GROUPEMENT = "Un groupement n'a pas de capital : utilisez membre_bureau."
MESSAGE_PERSONNE_PHYSIQUE = "La personne liée doit être une personne physique."
MESSAGE_DOUBLON = "Cette personne a déjà ce rôle sur cette fiche."
MESSAGE_SOMME = "La somme des pourcentages de détention dépasse 100 %."
MESSAGE_DERNIER_REPRESENTANT = (
    "Une fiche active doit garder un représentant légal : ajoutez-en un autre avant de "
    "terminer ou retirer celui-ci."
)
MESSAGE_PERIODE = "La date de fin ne peut pas précéder la date de début."
MESSAGE_BE_SANS_POURCENTAGE = "Un bénéficiaire effectif doit avoir un pourcentage de détention."
MESSAGE_POURCENTAGE_BE_SEUL = (
    "Le pourcentage de détention ne concerne que le bénéficiaire effectif."
)


class TierIntrouvableError(Exception):
    """Fiche inexistante, supprimée, ou hors périmètre. -> 404."""


class PersonneIntrouvableError(Exception):
    """Personne physique à lier inexistante, supprimée, ou hors périmètre. -> 404."""


class LienIntrouvableError(Exception):
    """Lien inexistant, supprimé, ou rattaché à une AUTRE fiche. -> 404."""


class LienInvalideError(Exception):
    """Règle métier violée ; le message pour l'utilisateur est porté par l'exception. -> 422."""


@dataclass(frozen=True)
class DonneesLien:
    individual_tier_id: uuid.UUID
    role: str
    date_debut: date
    fonction: str | None = None
    pourcentage_detention: Decimal | None = None
    pouvoir_signature: bool = False
    date_fin: date | None = None


def _jour_de_reference(db: Session) -> date:
    """« En cours » se juge sur la journée comptable (ouverte, sinon dernière clôturée), pas sur
    la date civile : même règle que tout le métier."""
    return journee_de_reference(db)


def _est_en_cours(lien: TierRelatedPerson, aujourdhui: date) -> bool:
    return (
        lien.deleted_at is None
        and lien.date_debut <= aujourdhui
        and (lien.date_fin is None or lien.date_fin > aujourdhui)
    )


def _charger_fiche(db: Session, courant: UtilisateurCourant, tier_id: uuid.UUID) -> Tier:
    """Charge la fiche (PM ou groupement) SOUS VERROU : la somme des pourcentages et la garde du
    dernier représentant se vérifient donc sans concurrence."""
    tier = db.execute(
        select(Tier)
        .where(Tier.id == tier_id, courant.condition_perimetre(_AGENCE), Tier.deleted_at.is_(None))
        .with_for_update()
    ).scalar_one_or_none()
    if tier is None:
        raise TierIntrouvableError()
    if tier.tier_type == "individual":
        raise LienInvalideError(MESSAGE_PAS_DE_LIEN_PP)
    return tier


def _charger_lien(db: Session, tier_id: uuid.UUID, lien_id: uuid.UUID) -> TierRelatedPerson:
    lien = db.execute(
        select(TierRelatedPerson).where(
            TierRelatedPerson.id == lien_id,
            TierRelatedPerson.tier_id == tier_id,
            TierRelatedPerson.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if lien is None:
        raise LienIntrouvableError()
    return lien


def _charger_personne(
    db: Session, courant: UtilisateurCourant, individual_tier_id: uuid.UUID
) -> Tier:
    personne = db.execute(
        select(Tier).where(
            Tier.id == individual_tier_id,
            courant.condition_perimetre(_AGENCE),
            Tier.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if personne is None:
        raise PersonneIntrouvableError()
    if personne.tier_type != "individual":
        raise LienInvalideError(MESSAGE_PERSONNE_PHYSIQUE)
    return personne


def _somme_beneficiaires(
    db: Session, tier_id: uuid.UUID, aujourdhui: date, *, sauf: uuid.UUID | None = None
) -> Decimal:
    requete = select(func.coalesce(func.sum(TierRelatedPerson.pourcentage_detention), 0)).where(
        TierRelatedPerson.tier_id == tier_id,
        TierRelatedPerson.role == "beneficiaire_effectif",
        liens_en_cours(aujourdhui),
    )
    if sauf is not None:
        requete = requete.where(TierRelatedPerson.id != sauf)
    return Decimal(db.execute(requete).scalar_one() or 0)


def _controler_somme(
    db: Session,
    tier_id: uuid.UUID,
    aujourdhui: date,
    pourcentage: Decimal | None,
    *,
    sauf: uuid.UUID | None = None,
) -> None:
    if pourcentage is None:
        return
    if _somme_beneficiaires(db, tier_id, aujourdhui, sauf=sauf) + pourcentage > PLAFOND_POURCENTAGE:
        raise LienInvalideError(MESSAGE_SOMME)


def _controler_dernier_representant(
    db: Session, tier: Tier, lien: TierRelatedPerson, aujourdhui: date, *, resterait_en_cours: bool
) -> None:
    """Une fiche sortie du statut prospect garde un représentant légal en cours."""
    if lien.role != "representant_legal" or tier.status == STATUT_PROSPECT:
        return
    if resterait_en_cours:
        return
    autres = db.execute(
        select(func.count()).where(
            TierRelatedPerson.tier_id == tier.id,
            TierRelatedPerson.role == "representant_legal",
            TierRelatedPerson.id != lien.id,
            liens_en_cours(aujourdhui),
        )
    ).scalar_one()
    if autres == 0:
        raise LienInvalideError(MESSAGE_DERNIER_REPRESENTANT)


def _valeurs(lien: TierRelatedPerson) -> dict[str, Any]:
    return {
        "tier_id": str(lien.tier_id),
        "individual_tier_id": str(lien.individual_tier_id),
        "role": lien.role,
        "fonction": lien.fonction,
        "pourcentage_detention": (
            str(lien.pourcentage_detention) if lien.pourcentage_detention is not None else None
        ),
        "pouvoir_signature": lien.pouvoir_signature,
        "date_debut": lien.date_debut.isoformat(),
        "date_fin": lien.date_fin.isoformat() if lien.date_fin else None,
    }


def _tracer_et_auditer(
    db: Session,
    courant: UtilisateurCourant,
    tier: Tier,
    lien: TierRelatedPerson,
    action: str,
    description: str,
    contexte: ContexteRequete,
    *,
    avant: dict[str, Any] | None,
    apres: dict[str, Any] | None,
) -> None:
    db.add(
        LifecycleEvent(
            tier_id=tier.id,
            event_type="updated",
            previous_status=tier.status,
            new_status=tier.status,
            reason=description,
            performed_by=courant.user_id,
        )
    )
    db.flush()
    ecrire_audit(
        db,
        action=action,
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=lien.id,
        agency_id=courant.agency_id,
        old_values=avant,
        new_values=apres,
    )
    db.commit()


# --- écritures -------------------------------------------------------------------------


def ajouter_lien(
    db: Session,
    courant: UtilisateurCourant,
    tier_id: uuid.UUID,
    donnees: DonneesLien,
    contexte: ContexteRequete,
) -> TierRelatedPerson:
    aujourdhui = _jour_de_reference(db)
    tier = _charger_fiche(db, courant, tier_id)
    if donnees.role == "beneficiaire_effectif" and tier.tier_type == "group":
        raise LienInvalideError(MESSAGE_BE_GROUPEMENT)
    _charger_personne(db, courant, donnees.individual_tier_id)

    deja = db.execute(
        select(TierRelatedPerson.id).where(
            TierRelatedPerson.tier_id == tier_id,
            TierRelatedPerson.individual_tier_id == donnees.individual_tier_id,
            TierRelatedPerson.role == donnees.role,
            TierRelatedPerson.deleted_at.is_(None),
            TierRelatedPerson.date_fin.is_(None),
        )
    ).first()
    if deja is not None:
        raise LienInvalideError(MESSAGE_DOUBLON)
    _controler_somme(db, tier_id, aujourdhui, donnees.pourcentage_detention)

    lien = TierRelatedPerson(
        tier_id=tier_id,
        individual_tier_id=donnees.individual_tier_id,
        role=donnees.role,
        fonction=donnees.fonction,
        pourcentage_detention=donnees.pourcentage_detention,
        pouvoir_signature=donnees.pouvoir_signature,
        date_debut=donnees.date_debut,
        date_fin=donnees.date_fin,
        created_by=courant.user_id,
        updated_by=courant.user_id,
    )
    db.add(lien)
    db.flush()
    evaluer_et_enregistrer(db, courant, tier, "personne_liee_ajoutee")
    _tracer_et_auditer(
        db, courant, tier, lien, "tier.related_person.added", "Personne liée ajoutée", contexte,
        avant=None, apres=_valeurs(lien),
    )
    return lien


def modifier_lien(
    db: Session,
    courant: UtilisateurCourant,
    tier_id: uuid.UUID,
    lien_id: uuid.UUID,
    changements: dict[str, Any],
    contexte: ContexteRequete,
) -> TierRelatedPerson:
    """`changements` ne contient QUE les champs envoyés (exclude_unset). Rôle et personne ne se
    modifient pas : on retire le lien et on en crée un autre (la trace reste lisible)."""
    aujourdhui = _jour_de_reference(db)
    tier = _charger_fiche(db, courant, tier_id)
    lien = _charger_lien(db, tier_id, lien_id)
    avant = _valeurs(lien)

    nouveau_debut = changements.get("date_debut", lien.date_debut)
    nouvelle_fin = changements.get("date_fin", lien.date_fin)
    if nouvelle_fin is not None and nouvelle_fin < nouveau_debut:
        raise LienInvalideError(MESSAGE_PERIODE)
    pourcentage = changements.get("pourcentage_detention", lien.pourcentage_detention)
    if lien.role == "beneficiaire_effectif":
        if pourcentage is None:
            raise LienInvalideError(MESSAGE_BE_SANS_POURCENTAGE)
        _controler_somme(db, tier_id, aujourdhui, pourcentage, sauf=lien.id)
    elif pourcentage is not None:
        raise LienInvalideError(MESSAGE_POURCENTAGE_BE_SEUL)

    resterait = nouveau_debut <= aujourdhui and (nouvelle_fin is None or nouvelle_fin > aujourdhui)
    _controler_dernier_representant(db, tier, lien, aujourdhui, resterait_en_cours=resterait)

    for champ, valeur in changements.items():
        setattr(lien, champ, valeur)
    lien.updated_by = courant.user_id
    lien.updated_at = func.now()
    db.flush()
    db.refresh(lien)
    evaluer_et_enregistrer(db, courant, tier, "personne_liee_modifiee")
    _tracer_et_auditer(
        db, courant, tier, lien, "tier.related_person.updated", "Personne liée modifiée",
        contexte, avant=avant, apres=_valeurs(lien),
    )
    return lien


def retirer_lien(
    db: Session,
    courant: UtilisateurCourant,
    tier_id: uuid.UUID,
    lien_id: uuid.UUID,
    motif: str,
    contexte: ContexteRequete,
) -> None:
    """Suppression LOGIQUE avec motif (jamais de DELETE physique sur une donnée métier)."""
    aujourdhui = _jour_de_reference(db)
    tier = _charger_fiche(db, courant, tier_id)
    lien = _charger_lien(db, tier_id, lien_id)
    _controler_dernier_representant(db, tier, lien, aujourdhui, resterait_en_cours=False)
    avant = _valeurs(lien)

    lien.deleted_at = func.now()
    lien.deleted_by = courant.user_id
    lien.deletion_reason = motif
    lien.updated_by = courant.user_id
    db.flush()
    evaluer_et_enregistrer(db, courant, tier, "personne_liee_retiree")
    _tracer_et_auditer(
        db, courant, tier, lien, "tier.related_person.removed", f"Personne liée retirée : {motif}",
        contexte, avant=avant, apres={"motif": motif},
    )


# --- lecture ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LigneLien:
    lien: TierRelatedPerson
    nom: str
    numero: str
    statut: str
    niveau_risque: str | None
    en_cours: bool


@dataclass(frozen=True)
class VueLiens:
    lignes: list[LigneLien]
    total_beneficiaires: Decimal
    seuil: Decimal | None
    seuil_provisoire: bool
    niveau_herite: str | None
    explication_risque: str
    origine: dict[str, Any] | None


def lire_seuil(db: Session) -> ParametresLbcft | None:
    """Seuil de détention LBC/FT ; None si le seed n'est pas passé (aucun indicateur alors)."""
    return db.get(ParametresLbcft, 1)


def lister_liens(db: Session, courant: UtilisateurCourant, tier_id: uuid.UUID) -> VueLiens:
    """Liens non supprimés (en cours ET terminés, `en_cours` les distingue) + risque hérité."""
    aujourdhui = _jour_de_reference(db)
    tier = db.execute(
        select(Tier).where(
            Tier.id == tier_id, courant.condition_perimetre(_AGENCE), Tier.deleted_at.is_(None)
        )
    ).scalar_one_or_none()
    if tier is None:
        raise TierIntrouvableError()
    if tier.tier_type == "individual":
        raise LienInvalideError(MESSAGE_PAS_DE_LIEN_PP)

    rangees = db.execute(
        select(TierRelatedPerson, IndividualProfile)
        .join(IndividualProfile, IndividualProfile.id == TierRelatedPerson.individual_tier_id)
        .where(TierRelatedPerson.tier_id == tier_id, TierRelatedPerson.deleted_at.is_(None))
        .order_by(TierRelatedPerson.date_debut, TierRelatedPerson.created_at, TierRelatedPerson.id)
    ).all()
    lignes = [
        LigneLien(
            lien=lien,
            nom=f"{personne.last_name} {personne.first_name}",
            numero=personne.tier_number,
            statut=personne.status,
            niveau_risque=personne.risk_level,
            en_cours=_est_en_cours(lien, aujourdhui),
        )
        for lien, personne in rangees
    ]
    parametres = lire_seuil(db)
    resultat = evaluer_pm(db, tier, "consultation")
    sans = bool(resultat.detail.get("sans_evaluation"))
    return VueLiens(
        lignes=lignes,
        total_beneficiaires=_somme_beneficiaires(db, tier_id, aujourdhui),
        seuil=parametres.seuil_beneficiaire_effectif if parametres else None,
        seuil_provisoire=parametres.is_provisional if parametres else False,
        niveau_herite=None if sans else resultat.niveau_effectif,
        explication_risque=str(resultat.detail.get("explication", "")),
        origine=resultat.detail.get("origine"),
    )


# --- conditions d'activation -----------------------------------------------------------


def representants_et_non_actifs(db: Session, tier_id: uuid.UUID) -> tuple[int, list[str]]:
    """(nombre de représentants légaux en cours, noms des personnes liées en cours NON actives)."""
    aujourdhui = _jour_de_reference(db)
    rangees = db.execute(
        select(TierRelatedPerson.role, IndividualProfile)
        .join(IndividualProfile, IndividualProfile.id == TierRelatedPerson.individual_tier_id)
        .where(TierRelatedPerson.tier_id == tier_id, liens_en_cours(aujourdhui))
    ).all()
    representants = sum(1 for role, _ in rangees if role == "representant_legal")
    non_actives: list[str] = []
    for _, personne in rangees:
        nom = f"{personne.last_name} {personne.first_name}"
        if personne.status != "actif" and nom not in non_actives:
            non_actives.append(nom)
    return representants, non_actives
