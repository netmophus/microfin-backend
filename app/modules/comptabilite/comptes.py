"""Plan de comptes — consultation et gestion UNITAIRE (Bloc 1 du paramétrage comptable).

Distinct de plan.py (import CSV en masse, tout ou rien) : ici, un compte à la fois, au fil de
l'eau, depuis l'écran. Les mêmes règles de cohérence que l'import s'appliquent (numéro/classe/
sens/parent), exprimées pour une saisie déjà TYPÉE (Pydantic a déjà écarté les chaînes brutes
invalides) plutôt que ré-empruntées telles quelles à plan.py (les deux contextes — fichier vs
corps de requête typé — ne se prêtent pas à un partage propre sans sur-abstraction).

Les garde-fous de service.py (compte système, mouvementé, enfants actifs) sont réutilisés SANS
modification et remontent TELS QUELS : ce module ne les réimplémente pas.

TRAÇABILITÉ : chaque écriture (création, modification, changement de sens, désactivation) est
auditée avant/après. Les actes SENSIBLES (sens, désactivation) exigent un MOTIF, inclus dans
l'audit. C'est du TRACE-ONLY pour l'instant — le double contrôle (maker-checker) est un chantier
séparé, généraliste (il servira aussi le Crédit), pas un bricolage local ici.

Un compte créé À LA MAIN (par ce module) n'est PAS provisoire : contrairement à l'import CSV, qui
livre un modèle générique à valider, la création manuelle EST la décision déjà validée du
comptable. is_system reste toujours FALSE : un compte système ne vient que du plan de référence.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.comptabilite.models import Account
from app.modules.comptabilite.service import compte_a_des_ecritures
from app.modules.comptabilite.service import desactiver as _desactiver_service
from app.modules.comptabilite.service import modifier_sens as _modifier_sens_service

TAILLE_PAGE_DEFAUT = 25
TAILLE_PAGE_MAX = 100

RESSOURCE = "comptabilite.account"

CHAMPS_MODIFIABLES = ("name", "short_name", "notes")


class CompteError(Exception):
    """Base des refus propres à ce module (les garde-fous de service.py remontent tels quels)."""


class ChampInvalideError(CompteError):
    """Une règle de cohérence du plan est violée (classe/numéro, parent)."""


class NumeroDejaUtiliseError(CompteError):
    """Le numéro de compte existe déjà."""


class ParentIntrouvableError(CompteError):
    """Le compte parent indiqué n'existe pas."""


@dataclass(frozen=True)
class FiltresComptes:
    q: str | None = None
    classe: int | None = None
    inclure_inactifs: bool = False


@dataclass(frozen=True)
class PageComptesResultat:
    comptes: list[Account]
    parents: dict[uuid.UUID, str]  # id du compte -> NUMÉRO de son parent (résolu)
    total: int


def _requete(filtres: FiltresComptes) -> Select:
    stmt = select(Account)
    if not filtres.inclure_inactifs:
        stmt = stmt.where(Account.is_active)
    if filtres.classe is not None:
        stmt = stmt.where(Account.account_class == filtres.classe)
    if filtres.q:
        motif = f"%{filtres.q}%"
        stmt = stmt.where(or_(Account.account_number.ilike(motif), Account.name.ilike(motif)))
    return stmt


def _resoudre_parents(db: Session, comptes: list[Account]) -> dict[uuid.UUID, str]:
    ids_parents = {c.parent_id for c in comptes if c.parent_id is not None}
    if not ids_parents:
        return {}
    numeros = dict(
        db.execute(
            select(Account.id, Account.account_number).where(Account.id.in_(ids_parents))
        ).all()
    )
    return {c.id: numeros[c.parent_id] for c in comptes if c.parent_id in numeros}


def lister(
    db: Session, filtres: FiltresComptes, *, page: int = 1, taille: int = TAILLE_PAGE_DEFAUT
) -> PageComptesResultat:
    """Liste paginée, triée par NUMÉRO — l'ordre naturel d'un plan comptable."""
    base = _requete(filtres)
    total = db.execute(select(func.count()).select_from(base.subquery())).scalar_one()
    comptes = list(
        db.execute(
            base.order_by(Account.account_number).limit(taille).offset((page - 1) * taille)
        ).scalars()
    )
    return PageComptesResultat(comptes=comptes, parents=_resoudre_parents(db, comptes), total=total)


def lire(db: Session, compte_id: uuid.UUID) -> tuple[Account, str | None] | None:
    """Rend (compte, numéro du parent) ou None si absent."""
    compte = db.get(Account, compte_id)
    if compte is None:
        return None
    parent_numero = None
    if compte.parent_id is not None:
        parent_numero = db.execute(
            select(Account.account_number).where(Account.id == compte.parent_id)
        ).scalar_one_or_none()
    return compte, parent_numero


def creer(
    db: Session,
    *,
    account_number: str,
    name: str,
    short_name: str | None,
    account_class: int,
    parent_number: str | None,
    normal_side: str,
    is_posting: bool,
    notes: str | None,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> Account:
    """Crée un compte. Refuse si la classe ne correspond pas au 1er chiffre du numéro, si le
    parent indiqué n'existe pas ou n'est pas un préfixe du numéro, ou si le numéro existe déjà."""
    if not account_number[0].isdigit() or int(account_number[0]) != account_class:
        raise ChampInvalideError(
            f"La classe ({account_class}) doit correspondre au premier chiffre du numéro "
            f"« {account_number} »."
        )

    parent_id: uuid.UUID | None = None
    if parent_number:
        parent = db.execute(
            select(Account.id).where(Account.account_number == parent_number)
        ).scalar_one_or_none()
        if parent is None:
            raise ParentIntrouvableError(f"Le compte parent « {parent_number} » n'existe pas.")
        if account_number == parent_number or not account_number.startswith(parent_number):
            raise ChampInvalideError(
                f"Le compte parent « {parent_number} » doit être un préfixe du numéro "
                f"« {account_number} »."
            )
        parent_id = parent

    # Pré-contrôle (message clair) ; l'UNIQUE en base reste le filet de sécurité (course rare
    # entre deux créations simultanées du même numéro — l'appelant traduit l'IntegrityError).
    deja = db.execute(
        select(Account.id).where(Account.account_number == account_number)
    ).scalar_one_or_none()
    if deja is not None:
        raise NumeroDejaUtiliseError(f"Le compte « {account_number} » existe déjà.")

    compte = Account(
        account_number=account_number,
        name=name,
        short_name=short_name,
        account_class=account_class,
        parent_id=parent_id,
        normal_side=normal_side,
        is_posting=is_posting,
        is_system=False,
        is_provisional=False,
        notes=notes,
        created_by=par,
        updated_by=par,
    )
    db.add(compte)
    db.flush()

    ecrire_audit(
        db,
        action="compta.plan.created",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=compte.id,
        new_values={
            "account_number": account_number,
            "name": name,
            "account_class": account_class,
            "normal_side": normal_side,
            "is_posting": is_posting,
            "parent_number": parent_number,
        },
    )
    return compte


def modifier(
    db: Session,
    compte: Account,
    modifications: dict[str, object],
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> Account:
    """Modification PARTIELLE du libellé/notes. Seuls les champs FOURNIS sont touchés."""
    inconnus = set(modifications) - set(CHAMPS_MODIFIABLES)
    if inconnus:
        raise ValueError(f"Champs non modifiables : {sorted(inconnus)}")
    if "name" in modifications and not str(modifications["name"] or "").strip():
        raise ChampInvalideError("Le libellé ne peut pas être vide.")

    avant = {champ: getattr(compte, champ) for champ in modifications}
    for champ, valeur in modifications.items():
        setattr(compte, champ, valeur)
    compte.updated_by = par
    db.flush()

    ecrire_audit(
        db,
        action="compta.plan.updated",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=compte.id,
        old_values=avant,
        new_values=modifications,
    )
    return compte


def changer_sens(
    db: Session,
    compte: Account,
    nouveau_sens: str,
    motif: str,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> Account:
    """Change le sens normal — MOTIF obligatoire, tracé. Les garde-fous (système, mouvementé)
    de service.py remontent TELS QUELS si le changement est refusé."""
    avant = compte.normal_side
    _modifier_sens_service(
        db, compte, nouveau_sens, par, est_mouvemente=lambda cid: compte_a_des_ecritures(db, cid)
    )
    ecrire_audit(
        db,
        action="compta.plan.sens_change",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=compte.id,
        old_values={"normal_side": avant},
        new_values={"normal_side": nouveau_sens, "motif": motif},
    )
    return compte


def desactiver_compte(
    db: Session,
    compte: Account,
    motif: str,
    par: uuid.UUID | None,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> Account:
    """Désactive un compte — MOTIF obligatoire, tracé. Les garde-fous (mouvementé, enfants
    actifs) de service.py remontent TELS QUELS si la désactivation est refusée."""
    _desactiver_service(
        db, compte, par, est_mouvemente=lambda cid: compte_a_des_ecritures(db, cid)
    )
    ecrire_audit(
        db,
        action="compta.plan.deactivated",
        contexte=contexte,
        acteur_id=par,
        resource_type=RESSOURCE,
        resource_id=compte.id,
        old_values={"is_active": True},
        new_values={"is_active": False, "motif": motif},
    )
    return compte
