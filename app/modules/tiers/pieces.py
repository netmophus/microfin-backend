"""Pièces d'identité des tiers (T2c) — saisie, unicité conditionnelle, vérification.

RÈGLES portées ici (la base ne peut pas les exprimer) :

  - UNICITÉ CONDITIONNELLE. Certains types (`enforce_unique`, ex. CNI) exigent un numéro unique
    dans TOUT le réseau ; d'autres (attestations) non. Le contrôle est réseau, mais son résultat
    respecte le cloisonnement : si la fiche en conflit est dans MON périmètre, on la nomme (l'agent
    a déjà le droit de la voir, il résout au guichet) ; sinon, refus strictement générique — ni
    nom, ni numéro de fiche, ni agence (même principe que le 404 vs 403). La collision est tracée
    dans l'AUDIT (privilégié) même quand elle est masquée à l'agent.
  - PAS D'ÉCHAPPATOIRE. Contrairement au téléphone (partageable), une pièce unique n'a aucun cas
    légitime de doublon : forcer un second exemplaire n'aide pas, il faut corriger l'existant.
  - VALIDITÉ CALCULÉE, jamais stockée : voir `etat_validite`. Une pièce périmée ne bloque JAMAIS
    la saisie (l'agent constate ce qu'il a) ; l'état sert au KYC (T3) et à l'affichage.
  - SUPPRESSION LOGIQUE AVEC MOTIF ; refus de supprimer la principale s'il reste d'autres pièces.
  - VÉRIFICATION = acte de contrôle réservé (tiers.identity.verify), séparé de la saisie.
  - DOUBLE TRACE : lifecycle_event 'updated' + ecrire_audit() EN DERNIER (D5).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.orm import Session

from app.modules.audit.service import ContexteRequete, ecrire_audit
from app.modules.parameters.models import IdentityDocumentType
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.tiers.models import (
    GroupProfile,
    IdentityDocument,
    IndividualProfile,
    LegalEntityProfile,
    LifecycleEvent,
    Tier,
)
from app.modules.tiers.schemas import Validite

RESSOURCE = "identity_document"
_T = Tier.__table__
_IND = IndividualProfile.__table__
_LE = LegalEntityProfile.__table__
_GP = GroupProfile.__table__
_AGENCE = _T.c.primary_agency_id


def _nom_fiche() -> ColumnElement[str]:
    """Nom lisible de la fiche selon son type, tier_number en dernier recours."""
    return func.coalesce(
        func.concat_ws(" ", _IND.c.last_name, _IND.c.first_name),
        _LE.c.legal_name,
        _GP.c.group_name,
        _T.c.tier_number,
    ).label("nom")

# Fenêtre « expire bientôt » : une pièce valide dont l'échéance approche. Politique d'affichage
# (et signal KYC), pas une donnée stockée — donc modifiable sans migration.
SEUIL_EXPIRATION_PROCHE = timedelta(days=90)


class TierIntrouvableError(Exception):
    """Tiers inexistant, supprimé, OU hors périmètre. -> 404."""


class PieceIntrouvableError(Exception):
    """Pièce inexistante, supprimée, ou d'un autre tiers. -> 404."""


class DoublonPieceError(Exception):
    """Le numéro existe déjà sur une AUTRE fiche pour un type à numéro unique. -> 422.

    dans_perimetre décide de ce que l'agent a le droit de voir : la fiche nommée, ou rien.
    """

    def __init__(
        self, *, dans_perimetre: bool, tier_number: str | None = None, nom: str | None = None
    ) -> None:
        self.dans_perimetre = dans_perimetre
        self.tier_number = tier_number
        self.nom = nom


class SuppressionPrincipaleError(Exception):
    """On supprime la pièce principale alors qu'il reste d'autres pièces vivantes. -> 409."""


def normaliser_numero(brut: str) -> str:
    """Retire les espaces et met en majuscules. Doit rester identique au backfill SQL de 0011."""
    return "".join(brut.split()).upper()


def etat_validite(expiry: date | None, *, aujourdhui: date | None = None) -> Validite:
    """État de validité CALCULÉ à la volée (jamais stocké) : toujours juste sans job de mise à jour.

    'sans_objet' si pas d'échéance (type qui n'en exige pas) ; sinon valide / expire_bientot /
    perimee selon la date du jour. Une pièce reste valide LE JOUR de son échéance."""
    if expiry is None:
        return "sans_objet"
    ref = aujourdhui or datetime.now(UTC).date()
    if expiry < ref:
        return "perimee"
    if expiry <= ref + SEUIL_EXPIRATION_PROCHE:
        return "expire_bientot"
    return "valide"


@dataclass(frozen=True)
class DonneesPiece:
    document_type_id: uuid.UUID
    document_number: str
    issuing_country_id: uuid.UUID | None = None
    issuing_authority: str | None = None
    date_of_issue: date | None = None
    expiry_date: date | None = None
    notes: str | None = None


def _charger_tier(db: Session, courant: UtilisateurCourant, tier_id: uuid.UUID) -> Tier:
    tier = db.execute(
        select(Tier).where(
            Tier.id == tier_id,
            courant.condition_perimetre(_AGENCE),
            Tier.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if tier is None:
        raise TierIntrouvableError()
    return tier


def _charger_piece(db: Session, tier_id: uuid.UUID, piece_id: uuid.UUID) -> IdentityDocument:
    piece = db.execute(
        select(IdentityDocument).where(
            IdentityDocument.id == piece_id,
            IdentityDocument.tier_id == tier_id,
            IdentityDocument.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if piece is None:
        raise PieceIntrouvableError()
    return piece


def _debasculer_principale(db: Session, tier_id: uuid.UUID) -> None:
    db.execute(
        update(IdentityDocument)
        .where(
            IdentityDocument.tier_id == tier_id,
            IdentityDocument.is_primary.is_(True),
            IdentityDocument.deleted_at.is_(None),
        )
        .values(is_primary=False)
    )


def _controler_unicite(
    db: Session,
    courant: UtilisateurCourant,
    tier_id: uuid.UUID,
    type_id: uuid.UUID,
    numero_norm: str,
    contexte: ContexteRequete,
) -> None:
    """Refuse un numéro déjà pris sur une AUTRE fiche, pour un type à numéro unique.

    Contrôle RÉSEAU (l'unicité l'exige), exposition CLOISONNÉE (le message respecte le périmètre).
    La collision est auditée dans tous les cas — l'agent front peut être aveuglé, pas la conformité.
    """
    enforce = db.execute(
        select(IdentityDocumentType.enforce_unique).where(IdentityDocumentType.id == type_id)
    ).scalar_one_or_none()
    if not enforce:
        return  # type sans exigence d'unicité (attestation…) : rien à contrôler

    conflit = db.execute(
        select(
            IdentityDocument.tier_id,
            _T.c.tier_number,
            _T.c.primary_agency_id,
            _nom_fiche(),
        )
        .select_from(IdentityDocument)
        .join(_T, _T.c.id == IdentityDocument.tier_id)
        .outerjoin(_IND, _IND.c.tier_id == _T.c.id)
        .outerjoin(_LE, _LE.c.tier_id == _T.c.id)
        .outerjoin(_GP, _GP.c.tier_id == _T.c.id)
        .where(
            IdentityDocument.document_type_id == type_id,
            IdentityDocument.document_number_normalized == numero_norm,
            IdentityDocument.deleted_at.is_(None),
            IdentityDocument.tier_id != tier_id,
            _T.c.deleted_at.is_(None),
        )
        .limit(1)
    ).first()
    if conflit is None:
        return

    dans_perimetre = courant.voit_tout or conflit.primary_agency_id == courant.agency_id
    # Trace privilégiée : la conformité voit la collision réseau, même masquée à l'agent.
    ecrire_audit(
        db,
        action="tier.identity.duplicate_blocked",
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type="tier",
        resource_id=tier_id,
        agency_id=courant.agency_id,
        new_values={
            "document_type_id": str(type_id),
            "conflit_tier_id": str(conflit.tier_id),
            "conflit_agency_id": str(conflit.primary_agency_id),
        },
    )
    db.commit()  # l'audit du refus persiste, indépendamment de l'insertion refusée
    raise DoublonPieceError(
        dans_perimetre=dans_perimetre,
        tier_number=conflit.tier_number if dans_perimetre else None,
        nom=conflit.nom if dans_perimetre else None,
    )


def _tracer_et_auditer(
    db: Session,
    courant: UtilisateurCourant,
    tier: Tier,
    action: str,
    description: str,
    contexte: ContexteRequete,
    piece_id: uuid.UUID,
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
        resource_id=piece_id,
        agency_id=courant.agency_id,
        new_values={"tier_id": str(tier.id), "detail": description},
    )
    db.commit()


# --- écritures -------------------------------------------------------------------------


def ajouter_piece(
    db: Session,
    courant: UtilisateurCourant,
    tier_id: uuid.UUID,
    *,
    donnees: DonneesPiece,
    is_primary: bool,
    contexte: ContexteRequete,
) -> IdentityDocument:
    tier = _charger_tier(db, courant, tier_id)
    numero_norm = normaliser_numero(donnees.document_number)
    # Peut lever DoublonPieceError (et auditer + commit le refus) AVANT toute insertion.
    _controler_unicite(db, courant, tier_id, donnees.document_type_id, numero_norm, contexte)

    if is_primary:
        _debasculer_principale(db, tier_id)
    piece = IdentityDocument(
        tier_id=tier_id,
        document_type_id=donnees.document_type_id,
        document_number=donnees.document_number.strip(),
        document_number_normalized=numero_norm,
        issuing_country_id=donnees.issuing_country_id,
        issuing_authority=donnees.issuing_authority,
        date_of_issue=donnees.date_of_issue,
        expiry_date=donnees.expiry_date,
        notes=donnees.notes,
        is_primary=is_primary,
        created_by=courant.user_id,
        updated_by=courant.user_id,
    )
    db.add(piece)
    db.flush()
    _tracer_et_auditer(
        db, courant, tier, "tier.identity_added", "Pièce ajoutée", contexte, piece.id
    )
    return piece


def definir_principale(
    db: Session,
    courant: UtilisateurCourant,
    tier_id: uuid.UUID,
    piece_id: uuid.UUID,
    contexte: ContexteRequete,
) -> IdentityDocument:
    tier = _charger_tier(db, courant, tier_id)
    piece = _charger_piece(db, tier_id, piece_id)
    if not piece.is_primary:
        _debasculer_principale(db, tier_id)
        piece.is_primary = True
        piece.updated_by = courant.user_id
        db.flush()
        _tracer_et_auditer(
            db, courant, tier, "tier.identity_primary_set", "Pièce principale modifiée",
            contexte, piece.id,
        )
    else:
        db.commit()
    return piece


def verifier_piece(
    db: Session,
    courant: UtilisateurCourant,
    tier_id: uuid.UUID,
    piece_id: uuid.UUID,
    notes: str | None,
    contexte: ContexteRequete,
) -> IdentityDocument:
    """Acte de CONTRÔLE (tiers.identity.verify) : atteste que la pièce a été vue et validée."""
    tier = _charger_tier(db, courant, tier_id)
    piece = _charger_piece(db, tier_id, piece_id)
    piece.is_verified = True
    piece.verified_at = datetime.now(UTC)
    piece.verified_by = courant.user_id
    piece.verification_notes = notes
    piece.updated_by = courant.user_id
    db.flush()
    _tracer_et_auditer(
        db, courant, tier, "tier.identity_verified", "Pièce vérifiée", contexte, piece.id
    )
    return piece


def supprimer_piece(
    db: Session,
    courant: UtilisateurCourant,
    tier_id: uuid.UUID,
    piece_id: uuid.UUID,
    motif: str | None,
    contexte: ContexteRequete,
) -> None:
    """Suppression LOGIQUE avec motif. Refuse de retirer la principale s'il en reste d'autres."""
    tier = _charger_tier(db, courant, tier_id)
    piece = _charger_piece(db, tier_id, piece_id)

    if piece.is_primary:
        autres = db.execute(
            select(IdentityDocument.id).where(
                IdentityDocument.tier_id == tier_id,
                IdentityDocument.id != piece_id,
                IdentityDocument.deleted_at.is_(None),
            )
        ).first()
        if autres is not None:
            raise SuppressionPrincipaleError()

    piece.deleted_at = datetime.now(UTC)
    piece.deleted_by = courant.user_id
    piece.deletion_reason = motif
    piece.is_primary = False
    db.flush()
    _tracer_et_auditer(
        db, courant, tier, "tier.identity_removed", "Pièce supprimée", contexte, piece.id
    )


# --- lecture ---------------------------------------------------------------------------


def lister_pieces(
    db: Session, courant: UtilisateurCourant, tier_id: uuid.UUID
) -> list[IdentityDocument] | None:
    """Pièces vivantes d'un tiers, ou None si le tiers est hors périmètre (-> 404)."""
    try:
        _charger_tier(db, courant, tier_id)
    except TierIntrouvableError:
        return None
    return list(
        db.execute(
            select(IdentityDocument)
            .where(IdentityDocument.tier_id == tier_id, IdentityDocument.deleted_at.is_(None))
            .order_by(IdentityDocument.is_primary.desc(), IdentityDocument.created_at)
        )
        .scalars()
        .all()
    )
