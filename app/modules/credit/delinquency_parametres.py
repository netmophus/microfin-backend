"""Paramètres des paliers de souffrance (CR5a, Bloc 5 du paramétrage comptable ; comptes
`compte_provision_id`/`compte_reprise_id` ajoutés en CR5c, migration 0038).

Distinct du calcul automatique (CR5c, `reclassification.py`) : ici, c'est la LECTURE et
l'ÉCRITURE des paliers eux-mêmes, depuis l'écran du comptable — seuil (jours), taux de
provision, rattachement encours/dotation/provision/reprise. Rien ici ne reclasse un crédit ; ce
module ne touche que la configuration.

GARDE-FOU sur les comptes : chaque numéro soumis passe par `comptes.compte_saisie_actif`
(comptabilite), qui refuse tout compte de regroupement ou désactivé — même soumis directement
à l'API, en contournant le sélecteur. Même discipline que les rattachements produit/agence/
parts déjà en place.

CONTRAIREMENT à `share_parameters` (singleton), cette table a PLUSIEURS lignes : ce module
sait donc CRÉER et SUPPRIMER un palier, pas seulement en modifier un existant — le nombre de
paliers est une donnée, pas une structure figée (voir migration 0036).

`seuil_jours` sert LUI-MÊME de clé de tri (pas de colonne `ordre` séparée) : `lister` trie
dessus, jamais sur autre chose.

SUPPRESSION : refuse si un dossier référence encore ce palier (`applications.delinquency_tier_id`,
CR5c) — exactement le garde-fou annoncé dans la docstring d'origine de `supprimer`, maintenant
que la contrainte existe réellement.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.comptabilite.comptes import compte_saisie_actif
from app.modules.comptabilite.models import Account
from app.modules.credit.demandes import CreditError
from app.modules.credit.models import Application, DelinquencyTier

RESSOURCE = "credit.delinquency_tier"


class SeuilDejaUtiliseError(CreditError):
    """Un autre palier utilise déjà ce seuil_jours — le message nomme le palier en conflit."""


class CodeDejaUtiliseError(CreditError):
    """Un autre palier utilise déjà ce code."""


class PalierEnUsageError(CreditError):
    """Un ou plusieurs dossiers de crédit sont actuellement classés dans ce palier (CR5c) — le
    supprimer les laisserait sans classification cohérente."""


def lister(db: Session) -> Sequence[DelinquencyTier]:
    """Tous les paliers, triés sur `seuil_jours` — l'ordre EST le seuil, pas une colonne à part."""
    return (
        db.execute(select(DelinquencyTier).order_by(DelinquencyTier.seuil_jours))
        .scalars()
        .all()
    )


def _verifier_unicite(
    db: Session, *, code: str, seuil_jours: int, exclure_id: uuid.UUID | None = None
) -> None:
    conflit_code = db.execute(
        select(DelinquencyTier).where(DelinquencyTier.code == code)
    ).scalar_one_or_none()
    if conflit_code is not None and conflit_code.id != exclure_id:
        raise CodeDejaUtiliseError(f"Le code « {code} » est déjà utilisé par un autre palier.")

    conflit_seuil = db.execute(
        select(DelinquencyTier).where(DelinquencyTier.seuil_jours == seuil_jours)
    ).scalar_one_or_none()
    if conflit_seuil is not None and conflit_seuil.id != exclure_id:
        raise SeuilDejaUtiliseError(
            f"Le seuil de {seuil_jours} jour(s) est déjà utilisé par le palier "
            f"« {conflit_seuil.libelle} »."
        )


def creer(
    db: Session,
    *,
    code: str,
    libelle: str,
    seuil_jours: int,
    taux_provision_bp: int,
    compte_encours_number: str | None,
    compte_dotation_number: str | None,
    compte_provision_number: str | None,
    compte_reprise_number: str | None,
    is_terminal: bool,
    motif: str,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> DelinquencyTier:
    """Ajoute UN palier — aucune migration requise, le nombre de paliers est une donnée.
    MOTIF obligatoire même à la création : une trace de QUI a ajouté quoi, et pourquoi."""
    _verifier_unicite(db, code=code, seuil_jours=seuil_jours)
    compte_encours = (
        compte_saisie_actif(db, compte_encours_number) if compte_encours_number else None
    )
    compte_dotation = (
        compte_saisie_actif(db, compte_dotation_number) if compte_dotation_number else None
    )
    compte_provision = (
        compte_saisie_actif(db, compte_provision_number) if compte_provision_number else None
    )
    compte_reprise = (
        compte_saisie_actif(db, compte_reprise_number) if compte_reprise_number else None
    )

    palier = DelinquencyTier(
        code=code,
        libelle=libelle,
        seuil_jours=seuil_jours,
        taux_provision_bp=taux_provision_bp,
        compte_encours_id=compte_encours.id if compte_encours else None,
        compte_dotation_id=compte_dotation.id if compte_dotation else None,
        compte_provision_id=compte_provision.id if compte_provision else None,
        compte_reprise_id=compte_reprise.id if compte_reprise else None,
        is_terminal=is_terminal,
        created_by=par,
        updated_by=par,
    )
    db.add(palier)
    db.flush()

    ecrire_audit(
        db,
        action="credit.delinquency_tier.created",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=palier.id,
        new_values={
            "code": code,
            "libelle": libelle,
            "seuil_jours": seuil_jours,
            "taux_provision_bp": taux_provision_bp,
            "compte_encours": compte_encours_number,
            "compte_dotation": compte_dotation_number,
            "compte_provision": compte_provision_number,
            "compte_reprise": compte_reprise_number,
            "is_terminal": is_terminal,
            "motif": motif,
        },
    )
    return palier


def modifier(
    db: Session,
    palier: DelinquencyTier,
    *,
    code: str,
    libelle: str,
    seuil_jours: int,
    taux_provision_bp: int,
    compte_encours_number: str | None,
    compte_dotation_number: str | None,
    compte_provision_number: str | None,
    compte_reprise_number: str | None,
    is_terminal: bool,
    motif: str,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> DelinquencyTier:
    """Remplace l'ÉTAT COMPLET du palier — l'écran soumet tous les champs à chaque
    enregistrement, comme les rattachements produit (pas un PATCH partiel)."""
    _verifier_unicite(db, code=code, seuil_jours=seuil_jours, exclure_id=palier.id)
    compte_encours = (
        compte_saisie_actif(db, compte_encours_number) if compte_encours_number else None
    )
    compte_dotation = (
        compte_saisie_actif(db, compte_dotation_number) if compte_dotation_number else None
    )
    compte_provision = (
        compte_saisie_actif(db, compte_provision_number) if compte_provision_number else None
    )
    compte_reprise = (
        compte_saisie_actif(db, compte_reprise_number) if compte_reprise_number else None
    )

    def _numero(account_id: uuid.UUID | None) -> str | None:
        if account_id is None:
            return None
        compte = db.get(Account, account_id)
        return compte.account_number if compte else None

    avant = {
        "code": palier.code,
        "libelle": palier.libelle,
        "seuil_jours": palier.seuil_jours,
        "taux_provision_bp": palier.taux_provision_bp,
        "compte_encours": _numero(palier.compte_encours_id),
        "compte_dotation": _numero(palier.compte_dotation_id),
        "compte_provision": _numero(palier.compte_provision_id),
        "compte_reprise": _numero(palier.compte_reprise_id),
        "is_terminal": palier.is_terminal,
    }

    palier.code = code
    palier.libelle = libelle
    palier.seuil_jours = seuil_jours
    palier.taux_provision_bp = taux_provision_bp
    palier.compte_encours_id = compte_encours.id if compte_encours else None
    palier.compte_dotation_id = compte_dotation.id if compte_dotation else None
    palier.compte_provision_id = compte_provision.id if compte_provision else None
    palier.compte_reprise_id = compte_reprise.id if compte_reprise else None
    palier.is_terminal = is_terminal
    palier.updated_by = par
    db.flush()

    ecrire_audit(
        db,
        action="credit.delinquency_tier.updated",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=palier.id,
        old_values=avant,
        new_values={
            "code": code,
            "libelle": libelle,
            "seuil_jours": seuil_jours,
            "taux_provision_bp": taux_provision_bp,
            "compte_encours": compte_encours_number,
            "compte_dotation": compte_dotation_number,
            "compte_provision": compte_provision_number,
            "compte_reprise": compte_reprise_number,
            "is_terminal": is_terminal,
            "motif": motif,
        },
    )
    return palier


def supprimer(
    db: Session,
    palier: DelinquencyTier,
    *,
    motif: str,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> None:
    """Retire UN palier. Refuse si un dossier de crédit y est actuellement classé (CR5c,
    `applications.delinquency_tier_id`) — même garde-fou que la désactivation d'un tiers avec
    engagements ouverts, annoncé dès la docstring d'origine de cette fonction (CR5a)."""
    nb_dossiers = db.execute(
        select(func.count())
        .select_from(Application)
        .where(Application.delinquency_tier_id == palier.id)
    ).scalar_one()
    if nb_dossiers > 0:
        raise PalierEnUsageError(
            f"{nb_dossiers} dossier(s) de crédit sont actuellement classés dans le palier "
            f"« {palier.libelle} » — impossible de le supprimer."
        )

    trace = {
        "code": palier.code,
        "libelle": palier.libelle,
        "seuil_jours": palier.seuil_jours,
        "motif": motif,
    }
    palier_id = palier.id
    db.delete(palier)
    db.flush()

    ecrire_audit(
        db,
        action="credit.delinquency_tier.deleted",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=palier_id,
        old_values=trace,
    )
