"""Postes de caisse (Bloc B) — CRUD, rattachement comptable, assignation des guichetiers.

Trois acteurs, trois responsabilités distinctes, jamais mélangées :
  - `caisse.poste.manage` (RESPONSABLE_AGENCE, SON agence) : créer/renommer/(dés)activer un
    poste, et l'assigner à des guichetiers — décision d'ORGANISATION de l'agence, pas comptable.
  - `compta.plan.manage` (existant, INSTITUTION ENTIÈRE, comme les 3 autres écrans Bloc 5) :
    rattacher le compte comptable d'un poste — décision comptable, jamais bornée à une agence.
  - Lecture : institution entière pour `compta.plan.manage`, cloisonnée à l'agence sinon.

GARDE-FOU sur les comptes : `comptes.compte_saisie_actif`, même discipline que les autres
rattachements Bloc 5.

DÉSACTIVATION : refuse si une session de caisse est actuellement ouverte sur ce poste — jamais
désactiver un objet avec un engagement actif, même règle que partout ailleurs dans ce projet.
Jamais de suppression physique (§15) : is_active seulement.

ASSIGNATION : l'utilisateur ciblé doit être rattaché OU habilité à la MÊME agence que le poste
— même définition que security/utilisateurs.py::_releve_de_agence (dupliquée ici, fonction
privée d'un autre module, pas importée à travers la frontière)."""

import uuid
from collections.abc import Sequence

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.caisse.models import CaisseSession, Poste, PosteAssignation
from app.modules.comptabilite.comptes import compte_saisie_actif
from app.modules.comptabilite.models import Account
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.security.models import User, UserAgency

RESSOURCE = "caisse.poste"

PERMISSION_RATTACHEMENT = "compta.plan.manage"


class PosteError(Exception):
    """Base des erreurs métier de ce module."""


class CodeDejaUtiliseError(PosteError):
    """Un autre poste de cette agence utilise déjà ce code."""


class PosteEnUsageError(PosteError):
    """Une session de caisse est actuellement ouverte sur ce poste — désactivation refusée."""


class UtilisateurHorsPerimetreError(PosteError):
    """L'utilisateur ciblé n'est ni rattaché ni habilité à l'agence de ce poste."""


class PosteIntrouvableError(PosteError):
    """Poste inexistant ou hors périmètre de l'acteur — 404, jamais 403 (IDOR)."""


def charger_poste_gere(db: Session, courant: UtilisateurCourant, poste_id: uuid.UUID) -> Poste:
    """Poste dans le périmètre de GESTION de l'acteur (création/renommage/(dés)activation/
    assignation) — SON agence, jamais au-delà. Hors périmètre ou inexistant ->
    PosteIntrouvableError (404, jamais 403 : IDOR, on ne révèle pas qu'un poste hors de portée
    existe)."""
    poste = db.execute(
        select(Poste).where(Poste.id == poste_id, courant.condition_perimetre(Poste.agency_id))
    ).scalar_one_or_none()
    if poste is None:
        raise PosteIntrouvableError()
    return poste


def charger_poste_pour_rattachement(db: Session, poste_id: uuid.UUID) -> Poste:
    """Poste pour le RATTACHEMENT comptable (compta.plan.manage) — institution entière, comme
    les 3 autres écrans Bloc 5 : le comptable configure le plan de comptes du réseau, pas une
    agence."""
    poste = db.get(Poste, poste_id)
    if poste is None:
        raise PosteIntrouvableError()
    return poste


def lister(db: Session, courant: UtilisateurCourant) -> Sequence[Poste]:
    """Tous les postes visibles : institution entière pour compta.plan.manage (comme les autres
    écrans Bloc 5, pour rattacher un compte n'importe où), sinon cloisonné à l'agence de
    l'acteur (RESPONSABLE_AGENCE — caisse.poste.manage)."""
    if PERMISSION_RATTACHEMENT in courant.permissions:
        stmt = select(Poste)
    else:
        stmt = select(Poste).where(courant.condition_perimetre(Poste.agency_id))
    return db.execute(stmt.order_by(Poste.agency_id, Poste.code)).scalars().all()


def _verifier_code_disponible(
    db: Session, *, agency_id: uuid.UUID | None, code: str, exclure_id: uuid.UUID | None = None
) -> None:
    conflit = db.execute(
        select(Poste.id).where(Poste.agency_id == agency_id, Poste.code == code)
    ).scalar_one_or_none()
    if conflit is not None and conflit != exclure_id:
        raise CodeDejaUtiliseError(
            f"Le code « {code} » est déjà utilisé par un poste de cette agence."
        )


def creer(
    db: Session,
    courant: UtilisateurCourant,
    *,
    code: str,
    libelle: str,
    motif: str,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> Poste:
    """Crée un poste POUR L'AGENCE COURANTE de l'acteur — jamais une agence soumise par le
    client, même discipline que `caissier_id` sur l'ouverture de session. Compte non rattaché à
    la création (état légitime, comme `Agency.compte_caisse_id` avant lui)."""
    _verifier_code_disponible(db, agency_id=courant.agency_id, code=code)

    poste = Poste(
        agency_id=courant.agency_id,
        code=code,
        libelle=libelle,
        created_by=courant.user_id,
        updated_by=courant.user_id,
    )
    db.add(poste)
    db.flush()

    ecrire_audit(
        db,
        action="caisse.poste.created",
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=poste.id,
        agency_id=courant.agency_id,
        new_values={"code": code, "libelle": libelle, "motif": motif},
    )
    return poste


def renommer(
    db: Session,
    courant: UtilisateurCourant,
    poste: Poste,
    *,
    code: str,
    libelle: str,
    motif: str,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> Poste:
    _verifier_code_disponible(db, agency_id=poste.agency_id, code=code, exclure_id=poste.id)

    avant = {"code": poste.code, "libelle": poste.libelle}
    poste.code = code
    poste.libelle = libelle
    poste.updated_by = courant.user_id
    db.flush()

    ecrire_audit(
        db,
        action="caisse.poste.updated",
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=poste.id,
        agency_id=poste.agency_id,
        old_values=avant,
        new_values={"code": code, "libelle": libelle, "motif": motif},
    )
    return poste


def changer_activation(
    db: Session,
    courant: UtilisateurCourant,
    poste: Poste,
    *,
    is_active: bool,
    motif: str,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> Poste:
    """(Dés)active — la désactivation REFUSE si une session est actuellement ouverte sur ce
    poste (jamais désactiver un objet avec un engagement actif, même règle que partout ailleurs
    dans ce projet). Jamais de suppression physique (§15)."""
    if not is_active:
        ouverte = db.execute(
            select(CaisseSession.id).where(
                CaisseSession.poste_id == poste.id, CaisseSession.status == "ouverte"
            )
        ).first()
        if ouverte is not None:
            raise PosteEnUsageError(
                "une session de caisse est actuellement ouverte sur ce poste : impossible de "
                "le désactiver."
            )

    avant = poste.is_active
    poste.is_active = is_active
    poste.updated_by = courant.user_id
    db.flush()

    ecrire_audit(
        db,
        action="caisse.poste.activated" if is_active else "caisse.poste.deactivated",
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=poste.id,
        agency_id=poste.agency_id,
        old_values={"is_active": avant},
        new_values={"is_active": is_active, "motif": motif},
    )
    return poste


def rattacher_compte(
    db: Session,
    courant: UtilisateurCourant,
    poste: Poste,
    *,
    compte_caisse_number: str | None,
    motif: str,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> Poste:
    """Re-pointe le compte de caisse d'UN POSTE — MOTIF obligatoire, tracé avant/après. Vider le
    rattachement (None) est une action LÉGITIME, pas une erreur (même discipline que
    parameters/rattachements.py::modifier_compte_caisse, dont ceci est le pendant par poste)."""
    nouveau = compte_saisie_actif(db, compte_caisse_number) if compte_caisse_number else None

    avant_numero = None
    if poste.compte_caisse_id is not None:
        avant = db.get(Account, poste.compte_caisse_id)
        avant_numero = avant.account_number if avant else None

    poste.compte_caisse_id = nouveau.id if nouveau else None
    poste.updated_by = courant.user_id
    db.flush()

    ecrire_audit(
        db,
        action="caisse.poste.compte_caisse_updated",
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=poste.id,
        agency_id=poste.agency_id,
        old_values={"compte_caisse": avant_numero},
        new_values={"compte_caisse": compte_caisse_number, "motif": motif},
    )
    return poste


def lister_assignations(db: Session, poste: Poste) -> Sequence[User]:
    return (
        db.execute(
            select(User)
            .join(PosteAssignation, PosteAssignation.user_id == User.id)
            .where(PosteAssignation.poste_id == poste.id)
            .order_by(User.last_name, User.first_name)
        )
        .scalars()
        .all()
    )


def assigner(
    db: Session,
    courant: UtilisateurCourant,
    poste: Poste,
    *,
    user_id: uuid.UUID,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> None:
    """Affecte un guichetier à ce poste — refuse si l'utilisateur ciblé n'est ni rattaché ni
    habilité à l'agence de ce poste (on n'assigne pas quelqu'un qui n'a pas le droit d'y
    travailler). Idempotent : déjà assigné -> ne fait rien, pas une erreur."""
    habilite = db.execute(
        select(User.id).where(
            User.id == user_id,
            or_(
                User.primary_agency_id == poste.agency_id,
                select(1)
                .where(UserAgency.user_id == User.id, UserAgency.agency_id == poste.agency_id)
                .exists(),
            ),
        )
    ).scalar_one_or_none()
    if habilite is None:
        raise UtilisateurHorsPerimetreError(
            "cet utilisateur n'est ni rattaché ni habilité à l'agence de ce poste."
        )

    deja = db.get(PosteAssignation, {"poste_id": poste.id, "user_id": user_id})
    if deja is not None:
        return

    db.add(PosteAssignation(poste_id=poste.id, user_id=user_id, granted_by=courant.user_id))
    db.flush()

    ecrire_audit(
        db,
        action="caisse.poste.assignation_ajoutee",
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=poste.id,
        agency_id=poste.agency_id,
        new_values={"user_id": str(user_id)},
    )


def revoquer(
    db: Session,
    courant: UtilisateurCourant,
    poste: Poste,
    *,
    user_id: uuid.UUID,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> None:
    """Retire l'affectation — idempotent : absente -> ne fait rien."""
    assignation = db.get(PosteAssignation, {"poste_id": poste.id, "user_id": user_id})
    if assignation is None:
        return

    db.delete(assignation)
    db.flush()

    ecrire_audit(
        db,
        action="caisse.poste.assignation_retiree",
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=poste.id,
        agency_id=poste.agency_id,
        old_values={"user_id": str(user_id)},
    )
