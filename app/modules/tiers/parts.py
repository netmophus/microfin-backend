"""Parts sociales (PS1) — souscription et libération, LE cœur transactionnel du sociétariat.

LA TRANSACTION UNIQUE (non négociable), comme l'épargne. Une opération fait, dans UNE seule
transaction, UN seul commit à la fin :
  1. charge le tier DANS le périmètre de l'acteur, exige un membre ACTIF (gate KYC) ;
  2. verrou de ligne sur le solde de parts (member_shares FOR UPDATE) — sérialise la concurrence ;
  3. contrôles (nombre de parts > 0, valeur d'une part fixée, libération ≤ parts non libérées) ;
  4. pose la PIÈCE comptable équilibrée (le GÉNÉRAL) ;
  5. écrit le MOUVEMENT de parts (append-only, relié à la pièce) — l'AUXILIAIRE ;
  6. met à jour le CACHE (member_shares) ;
  7. bascule is_member selon le PARAMÈTRE d'institution (membership_on) — DANS la transaction ;
  8. audit ; 9. commit.
Si l'une des étapes 4-8 lève, RIEN n'est committé : pas de parts libérées sans écriture, pas de
is_member basculé sans paiement. Le mouvement et la pièce sont posés ENSEMBLE.

FRONTIÈRE mécanique / valeurs : la mécanique est ici ; les VALEURS (valeur d'une part, minimum
pour adhérer, moment de l'adhésion) sont dans `tiers.share_parameters`, PROVISOIRES.

HYPOTHÈSE (provisoire) : valeur d'une part CONSTANTE. La libération et le rapprochement l'utilisent
telle quelle ; si l'IMF change la valeur, il faudra historiser (comme la date de valeur d'épargne).
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.tiers.models import MemberShares, ShareParameters, ShareSubscription, Tier
from app.modules.tiers.parts_operations import (
    TYPE_ANNULATION,
    TYPE_LIBERATION,
    TYPE_REMBOURSEMENT,
    TYPE_SOUSCRIPTION,
    TYPE_SOUSCRIPTION_COMPTANT,
    poser_ecriture_parts,
)


class PartsError(Exception):
    """Base des refus d'opération sur les parts sociales."""


class TierIntrouvableError(PartsError):
    """Tier inexistant ou hors périmètre de l'acteur. -> 404."""


class MembreNonActifError(PartsError):
    """Le tier n'est pas actif (gate KYC) : pas de souscription pour un prospect/suspendu."""


class PartsInvalidesError(PartsError):
    """Nombre de parts nul ou négatif."""


class MontantInvalideError(PartsError):
    """Montant nul : la valeur d'une part n'est pas fixée (paramétrage)."""


class LiberationExcessiveError(PartsError):
    """On tente de libérer plus de parts que le membre n'en a souscrit sans les libérer."""


class RemboursementExcessifError(PartsError):
    """On tente de rembourser plus de parts LIBÉRÉES que le membre n'en détient."""


class AnnulationExcessiveError(PartsError):
    """On tente d'annuler plus de parts NON LIBÉRÉES que le membre n'en a souscrit."""


class NonRemboursableError(PartsError):
    """Les parts ne sont pas remboursables (statuts de l'IMF) : le remboursement est refusé."""


class ParametrageManquantError(PartsError):
    """Aucune config de parts sociales : l'IMF n'a pas paramétré le sociétariat."""


class ActionsAudit:
    SOUSCRIPTION = "tiers.shares.subscribe"
    LIBERATION = "tiers.shares.liberate"
    REMBOURSEMENT = "tiers.shares.refund"
    ANNULATION = "tiers.shares.cancel"


@dataclass(frozen=True)
class ResultatParts:
    """Ce que l'opération rend (reçu de guichet)."""

    tier_id: uuid.UUID
    shares_liberees: int
    shares_non_liberees: int
    is_member: bool
    entry_number: str | None


def _config(db: Session) -> ShareParameters:
    config = db.execute(select(ShareParameters).limit(1)).scalar_one_or_none()
    if config is None:
        raise ParametrageManquantError(
            "Les parts sociales ne sont pas paramétrées : contactez votre administrateur."
        )
    return config


def _charger_tier(
    db: Session, courant: UtilisateurCourant, tier_id: uuid.UUID, *, exiger_actif: bool
) -> tuple[uuid.UUID, str]:
    """Rend (agence du tier, statut) si le tier est dans le périmètre ; lève 404 sinon.

    `exiger_actif` : à la SOUSCRIPTION/libération, on exige un membre ACTIF (gate KYC — pas de
    parts pour un prospect/suspendu). Au REMBOURSEMENT (sortie), on ne l'exige pas : un membre
    suspendu doit pouvoir récupérer son capital.
    """
    ligne = db.execute(
        select(Tier.primary_agency_id, Tier.status).where(
            Tier.id == tier_id,
            Tier.deleted_at.is_(None),
            courant.condition_perimetre(Tier.primary_agency_id),
        )
    ).one_or_none()
    if ligne is None:
        raise TierIntrouvableError()
    agency_id, statut = ligne
    if exiger_actif and statut != "actif":
        raise MembreNonActifError(
            "Ce tiers doit être actif (dossier validé) avant de souscrire des parts sociales."
        )
    return agency_id, statut


def _solde_verrou(db: Session, tier_id: uuid.UUID, par: uuid.UUID | None) -> MemberShares:
    """Charge le solde de parts SOUS VERROU, ou le crée (lazy). UNIQUE(tier_id) sérialise deux
    créations concurrentes (la seconde échoue proprement)."""
    shares = db.execute(
        select(MemberShares)
        .where(MemberShares.tier_id == tier_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if shares is None:
        shares = MemberShares(tier_id=tier_id, updated_by=par)
        db.add(shares)
        db.flush()
    return shares


def _appliquer_marqueur(
    db: Session, tier_id: uuid.UUID, shares: MemberShares, config: ShareParameters
) -> bool:
    """Bascule is_member selon le PARAMÈTRE d'institution — DANS la transaction de l'opération.
    Défaut 'liberation' : membre quand le CAPITAL est réellement là (parts libérées ≥ minimum)."""
    if config.membership_on == "souscription":
        detenues = shares.shares_liberees + shares.shares_non_liberees
    else:  # 'liberation' (défaut prudent)
        detenues = shares.shares_liberees
    est_membre = detenues >= config.minimum_shares
    db.execute(
        text("UPDATE tiers.tiers SET is_member = :m, updated_at = NOW() WHERE id = :t"),
        {"m": est_membre, "t": tier_id},
    )
    return est_membre


def _operer(
    db: Session,
    courant: UtilisateurCourant,
    tier_id: uuid.UUID,
    shares_count: int,
    *,
    code_operation: str,
    action_audit: str,
    exiger_actif: bool = True,
    contexte: ContexteRequete,
) -> ResultatParts:
    """Tronc commun de TOUTES les opérations de parts : périmètre + gate, verrou, contrôles, pièce
    + mouvement + cache + marqueur, audit, commit. La TRANSACTION UNIQUE : tout ou rien."""
    if shares_count <= 0:
        raise PartsInvalidesError("Nombre de parts invalide : saisissez au moins une part.")
    config = _config(db)
    amount = shares_count * config.unit_value
    if amount <= 0:
        raise MontantInvalideError(
            "Montant invalide : la valeur d'une part n'est pas encore fixée."
        )

    agency_id, _ = _charger_tier(db, courant, tier_id, exiger_actif=exiger_actif)
    shares = _solde_verrou(db, tier_id, courant.user_id)

    # Contrôles propres à l'opération (sur le solde SOUS verrou).
    if code_operation == TYPE_LIBERATION and shares.shares_non_liberees < shares_count:
        raise LiberationExcessiveError(
            "Libération impossible : ce membre n'a pas autant de parts souscrites non libérées."
        )
    if code_operation == TYPE_REMBOURSEMENT:
        if not config.is_refundable:
            raise NonRemboursableError(
                "Les parts sociales ne sont pas remboursables selon les statuts en vigueur."
            )
        if shares.shares_liberees < shares_count:
            raise RemboursementExcessifError(
                "Remboursement impossible : ce membre ne détient pas autant de parts libérées."
            )
    if code_operation == TYPE_ANNULATION and shares.shares_non_liberees < shares_count:
        raise AnnulationExcessiveError(
            "Annulation impossible : ce membre n'a pas autant de parts souscrites non libérées."
        )

    # GÉNÉRAL : la pièce équilibrée (peut lever si rattachement manquant -> refus propre).
    piece = poser_ecriture_parts(
        db,
        code_operation,
        amount,
        courant.user_id,
        agency_id=agency_id,
        compte_liberees_id=config.compte_parts_liberees_id,
        compte_non_liberees_id=config.compte_parts_non_liberees_id,
        libelle=f"{code_operation} {shares_count} parts",
        contexte=contexte,
    )

    # AUXILIAIRE : le mouvement, relié à la pièce, dans la MÊME transaction.
    db.add(
        ShareSubscription(
            tier_id=tier_id,
            agency_id=agency_id,
            type=code_operation.split(".", 1)[-1],
            shares_count=shares_count,
            unit_value=config.unit_value,
            amount=amount,
            journal_entry_id=piece.id,
            created_by=courant.user_id,
        )
    )

    # CACHE : libérées (capital réel -> 1021) vs non libérées (promesses -> 1022).
    if code_operation == TYPE_SOUSCRIPTION_COMPTANT:
        shares.shares_liberees += shares_count
    elif code_operation == TYPE_SOUSCRIPTION:
        shares.shares_non_liberees += shares_count
    elif code_operation == TYPE_LIBERATION:
        shares.shares_non_liberees -= shares_count
        shares.shares_liberees += shares_count
    elif code_operation == TYPE_REMBOURSEMENT:
        shares.shares_liberees -= shares_count  # on rend le capital libéré
    elif code_operation == TYPE_ANNULATION:
        shares.shares_non_liberees -= shares_count  # on solde la promesse non payée
    shares.updated_by = courant.user_id
    db.flush()

    est_membre = _appliquer_marqueur(db, tier_id, shares, config)

    ecrire_audit(
        db,
        action=action_audit,
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type="tiers.shares",
        resource_id=tier_id,
        agency_id=agency_id,
        new_values={
            "type": code_operation,
            "shares_count": shares_count,
            "amount": amount,
            "shares_liberees": shares.shares_liberees,
            "is_member": est_membre,
            "piece": piece.entry_number,
        },
    )
    db.commit()
    return ResultatParts(
        tier_id=tier_id,
        shares_liberees=shares.shares_liberees,
        shares_non_liberees=shares.shares_non_liberees,
        is_member=est_membre,
        entry_number=piece.entry_number,
    )


def souscrire(
    db: Session,
    courant: UtilisateurCourant,
    tier_id: uuid.UUID,
    shares_count: int,
    *,
    comptant: bool = True,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> ResultatParts:
    """Souscrit des parts. `comptant` (défaut) : paiement immédiat au guichet -> D CAISSE / C 1021,
    parts libérées. Sinon : engagement sans paiement -> D 1022 / C 1021, parts non libérées."""
    code = TYPE_SOUSCRIPTION_COMPTANT if comptant else TYPE_SOUSCRIPTION
    return _operer(
        db, courant, tier_id, shares_count,
        code_operation=code, action_audit=ActionsAudit.SOUSCRIPTION, contexte=contexte,
    )


def liberer(
    db: Session,
    courant: UtilisateurCourant,
    tier_id: uuid.UUID,
    shares_count: int,
    *,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> ResultatParts:
    """Libère (paie) des parts souscrites non libérées -> D CAISSE / C 1022. Le capital devient
    réel ; is_member bascule si le minimum libéré est atteint (paramètre 'liberation')."""
    return _operer(
        db, courant, tier_id, shares_count,
        code_operation=TYPE_LIBERATION, action_audit=ActionsAudit.LIBERATION, contexte=contexte,
    )


def rembourser(
    db: Session,
    courant: UtilisateurCourant,
    tier_id: uuid.UUID,
    shares_count: int,
    *,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> ResultatParts:
    """Rembourse des parts LIBÉRÉES (départ du sociétaire) -> D 1021 / C CAISSE, le capital sort.
    Refusé si les parts ne sont pas remboursables (statuts). Le marqueur suit : sous le minimum
    libéré, is_member repasse à FALSE (redevient client) — remboursement TOTAL = sortie complète.
    N'exige PAS un membre actif : un membre suspendu doit pouvoir récupérer son capital."""
    return _operer(
        db, courant, tier_id, shares_count,
        code_operation=TYPE_REMBOURSEMENT, action_audit=ActionsAudit.REMBOURSEMENT,
        exiger_actif=False, contexte=contexte,
    )


def annuler_souscription(
    db: Session,
    courant: UtilisateurCourant,
    tier_id: uuid.UUID,
    shares_count: int,
    *,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> ResultatParts:
    """Annule des parts NON LIBÉRÉES (promesses jamais payées) -> D 1021 / C 1022, sans caisse.
    Complète une sortie : rien à rendre (rien n'a été versé), on solde l'engagement."""
    return _operer(
        db, courant, tier_id, shares_count,
        code_operation=TYPE_ANNULATION, action_audit=ActionsAudit.ANNULATION,
        exiger_actif=False, contexte=contexte,
    )
