"""Paramètres des paliers de souffrance (CR5a, Bloc 5 du paramétrage comptable).

Distinct du calcul automatique (CR5c, pas encore construit) : ici, c'est la LECTURE et
l'ÉCRITURE des paliers eux-mêmes, depuis l'écran du comptable — seuil (jours), taux de
provision, rattachement encours/dotation. Rien ici ne reclasse un crédit ; ce module ne touche
que la configuration.

GARDE-FOU sur les comptes : chaque numéro soumis passe par `comptes.compte_saisie_actif`
(comptabilite), qui refuse tout compte de regroupement ou désactivé — même soumis directement
à l'API, en contournant le sélecteur. Même discipline que les rattachements produit/agence/
parts déjà en place.

CONTRAIREMENT à `share_parameters` (singleton), cette table a PLUSIEURS lignes : ce module
sait donc CRÉER et SUPPRIMER un palier, pas seulement en modifier un existant — le nombre de
paliers est une donnée, pas une structure figée (voir migration 0036).

`seuil_jours` sert LUI-MÊME de clé de tri (pas de colonne `ordre` séparée) : `lister` trie
dessus, jamais sur autre chose.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.comptabilite.comptes import compte_saisie_actif
from app.modules.comptabilite.models import Account
from app.modules.credit.demandes import CreditError
from app.modules.credit.models import DelinquencyTier

RESSOURCE = "credit.delinquency_tier"


class SeuilDejaUtiliseError(CreditError):
    """Un autre palier utilise déjà ce seuil_jours — le message nomme le palier en conflit."""


class CodeDejaUtiliseError(CreditError):
    """Un autre palier utilise déjà ce code."""


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

    palier = DelinquencyTier(
        code=code,
        libelle=libelle,
        seuil_jours=seuil_jours,
        taux_provision_bp=taux_provision_bp,
        compte_encours_id=compte_encours.id if compte_encours else None,
        compte_dotation_id=compte_dotation.id if compte_dotation else None,
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
        "is_terminal": palier.is_terminal,
    }

    palier.code = code
    palier.libelle = libelle
    palier.seuil_jours = seuil_jours
    palier.taux_provision_bp = taux_provision_bp
    palier.compte_encours_id = compte_encours.id if compte_encours else None
    palier.compte_dotation_id = compte_dotation.id if compte_dotation else None
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
    """Retire UN palier. CR5a : aucun crédit ne référence encore un palier (la reclassification
    automatique, CR5c, n'existe pas), donc rien n'est orphelin ici — quand CR5c introduira
    `applications.delinquency_tier_id`, CETTE fonction devra refuser de supprimer un palier
    encore utilisé par un dossier (même garde-fou que la désactivation d'un tiers avec
    engagements ouverts). Non implémenté maintenant : la contrainte n'existe pas encore."""
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
