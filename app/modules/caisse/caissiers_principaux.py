"""Chantier coffre-fort/caisses, sous-chantier 3, Lot A : désignation du caissier principal
d'une agence (lecture/écriture de `caisse.caissiers_principaux`).

UN caissier principal par agence à la fois (`agency_id` UNIQUE, upsert) — jamais un rattachement
N:N comme `postes.assigner`. `designer` REMPLACE la désignation existante, `retirer` la lève
(état « non désigné », légitime et transitoire — voir `transferts.py::_verifier_caissier_principal`,
qui refuse proprement plutôt que de deviner qui agit).

GARDE-FOU, DEUX CONDITIONS CUMULÉES (Lot B) : l'utilisateur désigné doit (1) être rattaché ou
habilité à L'AGENCE concernée — même discipline que `postes.assigner` — ET (2) détenir le rôle
CAISSIER. Le modèle B distingue explicitement responsable (coffre, rôle) et caissier principal
(personne) : désigner un responsable ou un comptable comme « caissier principal » n'aurait pas
de sens métier — d'où (2), une vérification de RÔLE, ici seulement (pas dans `transferts.py`,
qui reste permission-based par discipline d'autorisation).

VOLONTAIREMENT SÉPARÉ de `niveaux.py` (comptes, `compta.plan.manage`) : cette désignation est
organisationnelle, éditée sous `caisse.principale.manage` (Lot B/C), pas la même permission ni
la même nature de décision."""

import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.caisse.models import CaissierPrincipal
from app.modules.security.models import Role, User, UserAgency, UserRole

RESSOURCE = "caisse.caissier_principal"

CODE_ROLE_CAISSIER = "CAISSIER"


class CaissierPrincipalError(Exception):
    """Base des erreurs métier de ce module."""


class UtilisateurHorsPerimetreError(CaissierPrincipalError):
    """L'utilisateur désigné n'est ni rattaché ni habilité à cette agence."""

    def __init__(self) -> None:
        super().__init__(
            "cet utilisateur n'est ni rattaché ni habilité à cette agence : il ne peut pas en "
            "être le caissier principal."
        )


class RoleCaissierRequisError(CaissierPrincipalError):
    """L'utilisateur désigné ne détient pas le rôle Caissier."""

    def __init__(self) -> None:
        super().__init__(
            "cet utilisateur ne détient pas le rôle Caissier : seul un caissier peut être "
            "désigné caissier principal."
        )


def lire(db: Session, agency_id: uuid.UUID) -> CaissierPrincipal | None:
    """`None` est un état LÉGITIME (aucune désignation encore faite), jamais une erreur."""
    return db.execute(
        select(CaissierPrincipal).where(CaissierPrincipal.agency_id == agency_id)
    ).scalar_one_or_none()


def designer(
    db: Session,
    agency_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    motif: str,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> CaissierPrincipal:
    """Désigne (ou redésigne) LE caissier principal de cette agence — REMPLACE toute désignation
    existante, jamais un ajout. MOTIF obligatoire, tracé — décision consequente (qui peut
    mouvementer la principale), même discipline que les rattachements du Bloc 5. Refuse si
    l'utilisateur ciblé n'est ni rattaché ni habilité à cette agence, OU s'il ne détient pas le
    rôle Caissier (voir docstring module)."""
    habilite = db.execute(
        select(User.id).where(
            User.id == user_id,
            or_(
                User.primary_agency_id == agency_id,
                select(1)
                .where(UserAgency.user_id == User.id, UserAgency.agency_id == agency_id)
                .exists(),
            ),
        )
    ).scalar_one_or_none()
    if habilite is None:
        raise UtilisateurHorsPerimetreError()

    est_caissier = db.execute(
        select(UserRole.user_id)
        .join(Role, Role.id == UserRole.role_id)
        .where(UserRole.user_id == user_id, Role.code == CODE_ROLE_CAISSIER)
    ).first()
    if est_caissier is None:
        raise RoleCaissierRequisError()

    designation = lire(db, agency_id)
    avant = {"user_id": str(designation.user_id)} if designation is not None else None

    if designation is None:
        designation = CaissierPrincipal(agency_id=agency_id, user_id=user_id, assigned_by=par)
        db.add(designation)
    else:
        designation.user_id = user_id
        designation.assigned_by = par
    db.flush()

    ecrire_audit(
        db,
        action="caisse.caissier_principal.designe",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=designation.id,
        agency_id=agency_id,
        old_values=avant,
        new_values={"user_id": str(user_id), "motif": motif},
    )
    return designation


def retirer(
    db: Session,
    agency_id: uuid.UUID,
    *,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> None:
    """Lève la désignation — idempotent : aucune désignation -> ne fait rien. Retour à l'état
    « non désigné », légitime et transitoire (refus propre à l'usage, jamais un caissier
    deviné)."""
    designation = lire(db, agency_id)
    if designation is None:
        return

    avant = {"user_id": str(designation.user_id)}
    db.delete(designation)
    db.flush()

    ecrire_audit(
        db,
        action="caisse.caissier_principal.retire",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        agency_id=agency_id,
        old_values=avant,
        new_values=None,
    )
