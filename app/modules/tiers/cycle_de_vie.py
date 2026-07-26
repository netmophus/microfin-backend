"""Cycle de vie des tiers (T1e) — transitions de statut et activation (stub).

LA MACHINE À ÉTATS EST UNE DONNÉE, pas du code éparpillé : la table TRANSITIONS ci-dessous est
la source unique de vérité. Chaque transition connaît ses statuts source, sa cible, et son
éventuelle contrainte de type.

DEUX NIVEAUX, LE BON MESSAGE AU BON ENDROIT. La contrainte de type des transitions (decede
réservé aux personnes physiques, dissous aux morales/groupements) est la COPIE EXACTE des CHECK
de la migration 0008 (ck_tiers_deces_pp, ck_tiers_dissolution_pm). Le service la vérifie AVANT
d'écrire -> 409 propre au lieu d'un 500 opaque. La base reste le dernier rempart si un chemin de
code l'oubliait un jour.

SOFT DELETE UNIQUEMENT. deactivate pose deleted_at + status='desactive' ; jamais de DELETE.
La fiche sort alors des lectures normales. decede/dissous, eux, RESTENT visibles (pas de
deleted_at) : un membre décédé ne s'efface pas de l'annuaire — il y a une succession.

DOUBLE TRACE (D5). Chaque transition écrit un lifecycle_event DANS la transaction, puis
ecrire_audit() EN DERNIER (verrou consultatif du chaînage).

PÉRIMÈTRE À L'ÉCRITURE. On ne pilote pas une fiche hors de son agence : _charger_pour_ecriture
applique condition_perimetre + FOR UPDATE. Hors périmètre -> 404 (n'existe pas de mon point de
vue), jamais 403. Le FOR UPDATE sérialise deux transitions concurrentes sur la même fiche.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.service import ContexteRequete, ecrire_audit
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.tiers.models import (
    Contact,
    IdentityDocument,
    IndividualProfile,
    LifecycleEvent,
    Tier,
)
from app.modules.tiers.pieces import controler_unicite_fiche, etat_validite
from app.modules.tiers.risque import (
    GrilleSnapshot,
    ResultatRisque,
    charger_grille_active,
    enregistrer,
    evaluer,
    extraire_entrees,
)

# Capacité de valider un profil à RISQUE ÉLEVÉ — réservée au LBC/FT (le contrôle du blanchiment
# est son métier). Marqueur d'autorisation naturel : une permission, pas une heuristique de rôle.
PERMISSION_RISQUE_ELEVE = "tiers.validate.high_risk"

RESSOURCE = "tier"
_AGENCE = Tier.__table__.c.primary_agency_id  # colonne Core (le périmètre attend un optionnel)

# Rendu lisible d'un statut pour les messages d'erreur (français, comme le reste des messages
# backend). Minimal, propre au module ; la table de traduction riche vit côté frontend.
_STATUT_LISIBLE: dict[str, str] = {
    "prospect": "prospect",
    "actif": "actif",
    "suspendu_temporaire": "suspendu",
    "suspendu_lcb": "suspendu (LBC/FT)",
    "desactive": "désactivé",
    "decede": "décédé",
    "dissous": "dissous",
    "fusionne": "fusionné",
}


# --- erreurs ---------------------------------------------------------------------------


class CibleIntrouvableError(Exception):
    """Fiche inexistante, supprimée, OU hors périmètre. Indistinctement -> 404."""


class TransitionIllegaleError(Exception):
    """Le statut courant n'autorise pas cette action. -> 409, en nommant le statut."""

    def __init__(self, statut: str) -> None:
        self.statut = statut
        super().__init__(statut)


class TypeIncompatibleError(Exception):
    """L'action ne s'applique pas à ce type de tiers (miroir du CHECK D4). -> 409."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class ActivationImpossibleError(Exception):
    """L'activation requiert des conditions non remplies. -> 412, avec TOUTES les manquantes."""

    def __init__(self, conditions: list["ConditionActivation"]) -> None:
        self.conditions = conditions
        super().__init__("activation impossible")


# --- conditions d'activation (stub T1e, point de greffe T3) ----------------------------


@dataclass(frozen=True)
class ConditionActivation:
    code: str
    libelle: str


_C_PIECE_MANQUANTE = ConditionActivation(
    "PIECE_PRINCIPALE_MANQUANTE", "Enregistrez une pièce d'identité principale."
)
_C_PIECE_NON_VERIFIEE = ConditionActivation(
    "PIECE_NON_VERIFIEE", "La pièce principale doit être vérifiée par un responsable."
)
_C_PIECE_PERIMEE = ConditionActivation(
    "PIECE_PERIMEE", "La pièce principale est périmée : enregistrez une pièce valide."
)
_C_TELEPHONE = ConditionActivation("TELEPHONE_MANQUANT", "Renseignez au moins un téléphone.")
_C_ADRESSE = ConditionActivation("ADRESSE_MANQUANTE", "Renseignez au moins une adresse.")
_C_ORIGINE_FONDS = ConditionActivation("ORIGINE_FONDS_MANQUANTE", "Renseignez l'origine des fonds.")
_C_COUPERET = ConditionActivation(
    "COUPERET_ACTIF", "Activation bloquée : correspondance liste de sanctions à lever."
)
_C_VALIDEUR = ConditionActivation(
    "VALIDEUR_INSUFFISANT", "Profil à risque élevé : seul le responsable LBC/FT peut activer."
)
_C_AUTO_VALIDATION = ConditionActivation(
    "AUTO_VALIDATION_INTERDITE",
    "Double regard requis : un autre agent que celui qui a vérifié la pièce doit activer.",
)
# Données de risque manquantes -> condition dédiée par champ (le moteur T3b désigne le champ).
_C_DONNEE_RISQUE = {
    "secteur_activite": ConditionActivation(
        "SECTEUR_MANQUANT", "Renseignez le secteur d'activité."
    ),
    "mode_entree_relation": ConditionActivation(
        "MODE_ENTREE_MANQUANT", "Renseignez le mode d'entrée en relation."
    ),
    "nationalite": ConditionActivation("NATIONALITE_MANQUANTE", "Renseignez la nationalité."),
}


def _piece_principale(db: Session, tier_id: uuid.UUID) -> IdentityDocument | None:
    return db.execute(
        select(IdentityDocument).where(
            IdentityDocument.tier_id == tier_id,
            IdentityDocument.is_primary.is_(True),
            IdentityDocument.deleted_at.is_(None),
        )
    ).scalar_one_or_none()


def _auto_validation(db: Session, courant: UtilisateurCourant, tier_id: uuid.UUID) -> bool:
    """L'activateur est-il celui qui a vérifié la pièce principale ? (le quatre-yeux à trancher)."""
    piece = _piece_principale(db, tier_id)
    return piece is not None and piece.is_verified and piece.verified_by == courant.user_id


def _agence_exige_double(db: Session, tier: Tier) -> bool:
    agence = db.get(Agency, tier.primary_agency_id)
    return agence.double_validation_kyc if agence is not None else True


def conditions_dossier(
    db: Session, tier: Tier
) -> tuple[list[ConditionActivation], ResultatRisque, GrilleSnapshot]:
    """Conditions de COMPLÉTUDE du dossier — indépendantes de QUI regarde (pièce, contacts, KYC,
    couperet). Recalcule un score FRAIS en mémoire (non archivé). Le bandeau de la fiche s'en sert :
    elles disent quoi COMPLÉTER, avant même de cliquer « Activer ». « KYC complet » est DÉLÉGUÉ au
    moteur T3b (donnees_manquantes) — une seule définition partagée."""
    grille = charger_grille_active(db)
    resultat = evaluer(extraire_entrees(db, tier), grille, "activation")

    manquantes: list[ConditionActivation] = []

    piece = _piece_principale(db, tier.id)
    if piece is None:
        manquantes.append(_C_PIECE_MANQUANTE)
    else:
        if not piece.is_verified:
            manquantes.append(_C_PIECE_NON_VERIFIEE)
        if etat_validite(piece.expiry_date) == "perimee":
            manquantes.append(_C_PIECE_PERIMEE)

    types_contacts = set(
        db.execute(
            select(Contact.contact_type).where(
                Contact.tier_id == tier.id, Contact.deleted_at.is_(None)
            )
        ).scalars()
    )
    if "phone" not in types_contacts:
        manquantes.append(_C_TELEPHONE)
    if "address" not in types_contacts:
        manquantes.append(_C_ADRESSE)

    if isinstance(tier, IndividualProfile) and not (tier.origine_fonds or "").strip():
        manquantes.append(_C_ORIGINE_FONDS)

    for champ in resultat.donnees_manquantes:
        manquantes.append(
            _C_DONNEE_RISQUE.get(champ, ConditionActivation(f"DONNEE_{champ.upper()}", champ))
        )

    if resultat.couperet_declenche:
        manquantes.append(_C_COUPERET)

    return manquantes, resultat, grille


def evaluer_activation(
    db: Session, courant: UtilisateurCourant, tier: Tier
) -> tuple[list[ConditionActivation], ResultatRisque, GrilleSnapshot]:
    """TOUTES les conditions non remplies = dossier + conditions propres à L'ACTIVATEUR (niveau de
    risque exigeant le LBC/FT, quatre-yeux)."""
    manquantes, resultat, grille = conditions_dossier(db, tier)
    if resultat.niveau_effectif == "eleve" and PERMISSION_RISQUE_ELEVE not in courant.permissions:
        manquantes.append(_C_VALIDEUR)
    if _agence_exige_double(db, tier) and _auto_validation(db, courant, tier.id):
        manquantes.append(_C_AUTO_VALIDATION)
    return manquantes, resultat, grille


def conditions_activation_manquantes(
    db: Session, courant: UtilisateurCourant, tier: Tier
) -> list[ConditionActivation]:
    """Toutes les conditions NON remplies, d'un bloc (le valideur voit tout ce qui manque)."""
    return evaluer_activation(db, courant, tier)[0]


# --- machine à états -------------------------------------------------------------------


@dataclass(frozen=True)
class Transition:
    event_type: str
    sources: frozenset[str]
    cible: str
    types: frozenset[str] | None  # contrainte de tier_type (miroir du CHECK D4), ou None
    message_type: str | None  # message si le type ne convient pas
    soft_delete: bool = False
    restore: bool = False  # lève le soft delete (charge un désactivé, remet deleted_at à NULL)


TRANSITIONS: dict[str, Transition] = {
    "suspend": Transition("suspended", frozenset({"actif"}), "suspendu_temporaire", None, None),
    "reactivate": Transition(
        "reactivated", frozenset({"suspendu_temporaire"}), "actif", None, None
    ),
    "deactivate": Transition(
        "deactivated",
        frozenset({"prospect", "actif", "suspendu_temporaire", "suspendu_lcb"}),
        "desactive",
        None,
        None,
        soft_delete=True,
    ),
    # Retour d'une fiche désactivée : vers PROSPECT, jamais actif — un ancien membre qui revient
    # repasse la validation KYC (identité re-vérifiée, relation ré-instruite). La moitié manquante
    # du soft delete : la donnée était là, ce chemin lui rend un retour.
    "restore": Transition(
        "restored", frozenset({"desactive"}), "prospect", None, None, restore=True
    ),
    "mark_deceased": Transition(
        "marked_deceased",
        frozenset({"prospect", "actif", "suspendu_temporaire"}),
        "decede",
        frozenset({"individual"}),
        "Un décès ne peut être enregistré que sur une personne physique.",
    ),
    "mark_dissolved": Transition(
        "marked_dissolved",
        frozenset({"prospect", "actif", "suspendu_temporaire"}),
        "dissous",
        frozenset({"legal_entity", "group"}),
        "Une dissolution ne concerne qu'une personne morale ou un groupement.",
    ),
}


def _charger_pour_ecriture(
    db: Session,
    courant: UtilisateurCourant,
    tier_id: uuid.UUID,
    inclure_desactive: bool = False,
) -> Tier:
    """Charge la fiche SOUS VERROU, dans le périmètre de l'acteur. Sinon CibleIntrouvable (404).

    inclure_desactive n'est vrai QUE pour la restauration : c'est le seul cas où l'on doit charger
    une fiche `deleted_at IS NOT NULL`. Toute autre transition reste bloquée sur les désactivés.
    """
    stmt = select(Tier).where(
        Tier.id == tier_id,
        courant.condition_perimetre(_AGENCE),
    )
    if not inclure_desactive:
        stmt = stmt.where(Tier.deleted_at.is_(None))
    tier = db.execute(stmt.with_for_update()).scalar_one_or_none()
    if tier is None:
        raise CibleIntrouvableError()
    return tier


def _tracer_et_auditer(
    db: Session,
    courant: UtilisateurCourant,
    tier: Tier,
    event_type: str,
    ancien: str,
    nouveau: str,
    motif: str | None,
    contexte: ContexteRequete,
    extra: dict[str, Any] | None = None,
) -> None:
    """lifecycle_event DANS la transaction, puis audit EN DERNIER, puis commit.

    extra : faits supplémentaires portés à l'audit (ex. à l'activation : l'évaluation KYC de
    référence, le niveau de risque, l'auto-validation tolérée)."""
    db.add(
        LifecycleEvent(
            tier_id=tier.id,
            event_type=event_type,
            previous_status=ancien,
            new_status=nouveau,
            reason=motif,
            performed_by=courant.user_id,
        )
    )
    db.flush()

    valeurs: dict[str, Any] = {"status": nouveau, "tier_number": tier.tier_number}
    if motif:
        valeurs["motif"] = motif
    if extra:
        valeurs.update(extra)
    ecrire_audit(
        db,
        action=f"tier.{event_type}",
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=tier.id,
        agency_id=courant.agency_id,
        old_values={"status": ancien},
        new_values=valeurs,
    )
    db.commit()


def executer_transition(
    db: Session,
    courant: UtilisateurCourant,
    tier_id: uuid.UUID,
    nom: str,
    contexte: ContexteRequete,
    motif: str | None = None,
) -> Tier:
    """Applique une transition de la table TRANSITIONS. 409 si statut/type incompatible."""
    transition = TRANSITIONS[nom]
    tier = _charger_pour_ecriture(db, courant, tier_id, inclure_desactive=transition.restore)

    if tier.status not in transition.sources:
        raise TransitionIllegaleError(tier.status)
    # Miroir du CHECK D4 : arrêté ici en 409, jamais laissé filer jusqu'au 500 de la base.
    if transition.types is not None and tier.tier_type not in transition.types:
        assert transition.message_type is not None
        raise TypeIncompatibleError(transition.message_type)

    # Garde d'unicité À LA RESTAURATION, AVANT toute mutation : pendant l'absence de la fiche,
    # un numéro de pièce unique a pu être repris ailleurs (une pièce de désactivé est exclue du
    # contrôle T2c). On refait le contrôle ; en cas de collision -> DoublonPieceError (422),
    # nommée si dans le périmètre. La fiche reste désactivée, le responsable résout d'abord.
    if transition.restore:
        controler_unicite_fiche(db, courant, tier.id, contexte)

    ancien = tier.status
    maintenant = datetime.now(UTC)
    tier.status = transition.cible
    tier.updated_by = courant.user_id
    if nom == "suspend":
        tier.suspended_at = maintenant
        tier.suspended_by = courant.user_id
        tier.suspension_reason = motif
    elif nom == "reactivate":
        tier.suspended_at = None
        tier.suspended_by = None
        tier.suspension_reason = None
    elif transition.restore:
        # La fiche revient dans l'annuaire, en PROSPECT non validé : on efface l'activation.
        tier.deleted_at = None
        tier.activated_at = None
        tier.activated_by = None
    elif transition.soft_delete:
        tier.deleted_at = maintenant  # soft delete : jamais de suppression physique

    _tracer_et_auditer(
        db, courant, tier, transition.event_type, ancien, transition.cible, motif, contexte
    )
    return tier


def activer(
    db: Session, courant: UtilisateurCourant, tier_id: uuid.UUID, contexte: ContexteRequete
) -> Tier:
    """Active une fiche prospect POUR DE VRAI (T3c) : passe à 'actif' quand TOUTES les conditions
    sont réunies ; sinon 412 avec la liste complète de ce qui manque.

    Sur succès : archive l'évaluation KYC de référence (declencheur='activation'), y lie la fiche
    (activation_assessment_id) — un inspecteur remonte de l'activation à l'évaluation exacte. Une
    auto-validation tolérée (agence assouplie, même personne) est TRACÉE (auto_validation=true)."""
    tier = _charger_pour_ecriture(db, courant, tier_id)
    if tier.status != "prospect":
        raise TransitionIllegaleError(tier.status)

    manquantes, resultat, grille = evaluer_activation(db, courant, tier)
    if manquantes:
        raise ActivationImpossibleError(manquantes)

    # Archive l'évaluation exacte sur laquelle s'appuie l'activation (met aussi à jour le reflet).
    evaluation = enregistrer(db, tier, resultat, grille, "activation", courant.user_id)
    # Vrai seulement si l'agence a assoupli le quatre-yeux (sinon la condition aurait bloqué).
    auto = _auto_validation(db, courant, tier.id)

    ancien = tier.status
    tier.status = "actif"
    tier.activated_at = datetime.now(UTC)
    tier.activated_by = courant.user_id
    tier.activation_assessment_id = evaluation.id
    tier.updated_by = courant.user_id
    _tracer_et_auditer(
        db,
        courant,
        tier,
        "activated",
        ancien,
        "actif",
        "Auto-validation (double regard assoupli)" if auto else None,
        contexte,
        extra={
            "activation_assessment_id": str(evaluation.id),
            "niveau_risque": resultat.niveau_effectif,
            "grille_provisoire": grille.is_provisional,
            "auto_validation": auto,
        },
    )
    return tier


def message_transition_illegale(statut: str) -> str:
    """Message 409 clair, nommant le statut courant en français."""
    return f"Action impossible : la fiche est « {_STATUT_LISIBLE.get(statut, statut)} »."
