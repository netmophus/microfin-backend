"""Caisse CA1 — ouverture, fermeture, calcul de l'écart. AUCUNE écriture comptable posée ici
(CA3, une fois le compte d'écart validé par l'expert), AUCUN blocage sur écart (CA2), AUCUNE
tolérance/seuil (CA2, `caisse.parametres` pas encore créée).

LE CALCUL CENTRAL (`calculer_solde_theorique`) est un calcul DÉRIVÉ, jamais un solde stocké
qu'on relit — même discipline que `epargne.rapprochement.rapprocher()` : on interroge les
écritures VALIDÉES du compte de caisse (`comptabilite.journal_lines`/`journal_entries`),
fenêtrées entre l'ouverture de la session et l'instant demandé, filtrées sur LE CAISSIER de
cette session (`journal_entries.created_by`) — pas sur tout le compte, qui est partagé par
l'agence entière si plusieurs caissiers travaillent en parallèle (décision : une session par
caissier, pas par agence).

UNE SEULE SESSION OUVERTE PAR CAISSIER : vérifié ici (message clair) ET en base (index unique
partiel `uq_caisse_sessions_caissier_ouverte`, dernier rempart si ce contrôle était contourné).

LECTURE : deux publics, un seul chemin (`lire_session`/`lister_sessions_manquantes`). Le
CAISSIER voit TOUJOURS ses propres sessions (caisse.session.read, sans condition d'agence —
c'est SA donnée, comme consulter sa propre fiche). Un tiers (responsable/audit/direction) ne
voit une session qui n'est pas la sienne que s'il détient caisse.session.read.autres ET qu'elle
est dans son périmètre (condition_perimetre : son agence, ou tout le réseau pour voit_tout).
Sert la lettre de demande d'explication (manquant à la fermeture) — document, pas un
garde-fou : aucune écriture, aucun blocage, orthogonal à CA2 (seuil/motif, pas construit).
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.modules.audit.service import CONTEXTE_VIDE, ContexteRequete, ecrire_audit
from app.modules.caisse.models import CaisseSession, Poste, PosteAssignation
from app.modules.caisse.postes import PosteIntrouvableError
from app.modules.comptabilite.models import Account
from app.modules.parameters.models import Agency
from app.modules.security.autorisation import UtilisateurCourant
from app.modules.security.models import User

RESSOURCE = "caisse.session"

# Permission élargie : lire une session qui n'appartient pas à l'acteur (voir docstring module).
PERMISSION_LECTURE_AUTRES = "caisse.session.read.autres"

TAILLE_PAGE_DEFAUT = 25
TAILLE_PAGE_MAX = 100


class SessionDejaOuverteError(Exception):
    """Ce caissier a déjà une session ouverte — une seule à la fois."""


class SessionIntrouvableError(Exception):
    """Session inexistante, ou n'appartenant pas à l'acteur. -> 404 (jamais 403 : IDOR)."""


class SessionDejaFermeeError(Exception):
    """Cette session est déjà fermée — on ne referme pas ce qui l'est déjà."""


class RattachementManquantError(Exception):
    """Le poste choisi n'a pas de compte de caisse rattaché (paramétrage)."""


class AucuneSessionOuverteError(Exception):
    """Aucun guichet ne peut opérer en espèces sans une session de caisse ouverte pour ce
    caissier — le tiroir doit être ouvert avant tout mouvement."""


def ouvrir_session(
    db: Session,
    courant: UtilisateurCourant,
    *,
    poste_id: uuid.UUID,
    fonds_initial: int,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> CaisseSession:
    """Ouvre une session POUR L'ACTEUR LUI-MÊME (`caissier_id = courant.user_id`) — jamais pour
    un autre, jamais un champ soumis par le client. Refuse si une session est déjà ouverte pour
    lui (message clair ; l'index unique partiel est le dernier rempart en base si ce contrôle
    était contourné par un appel concurrent).

    `poste_id` TOUJOURS soumis par le client (Bloc C) — jamais déduit ici, même quand l'acteur
    n'a qu'un seul poste assigné (voir `schemas.OuvertureSession`, motif détaillé). Le poste
    doit être ACTIF, ASSIGNÉ à l'acteur (`PosteAssignation`) ET dans l'agence COURANTE de sa
    session (`courant.agency_id`, pas seulement une agence où il est habilité — une session de
    caisse s'ouvre POUR l'agence où l'on travaille aujourd'hui) : filtré par un id FIXE (clé
    primaire), la requête ne peut plus jamais rendre deux lignes — l'ancienne ambiguïté du
    Bloc A/B (résolution automatique sur toute l'agence) disparaît structurellement, pas par un
    garde-fou en plus. Hors périmètre ou inexistant -> `PosteIntrouvableError` (404, jamais
    403 : IDOR, on ne révèle pas qu'un poste hors de portée existe).

    `compte_caisse_id` est ANCRÉ ici, copié depuis le POSTE choisi à cet instant — jamais
    recalculé ensuite, même si le rattachement change après coup."""
    deja = db.execute(
        select(CaisseSession.id).where(
            CaisseSession.caissier_id == courant.user_id, CaisseSession.status == "ouverte"
        )
    ).first()
    if deja is not None:
        raise SessionDejaOuverteError(
            "Une session de caisse est déjà ouverte pour vous : fermez-la avant d'en ouvrir "
            "une nouvelle."
        )

    poste = db.execute(
        select(Poste)
        .join(PosteAssignation, PosteAssignation.poste_id == Poste.id)
        .where(
            Poste.id == poste_id,
            Poste.agency_id == courant.agency_id,
            Poste.is_active.is_(True),
            PosteAssignation.user_id == courant.user_id,
        )
    ).scalar_one_or_none()
    if poste is None:
        raise PosteIntrouvableError()
    if poste.compte_caisse_id is None:
        raise RattachementManquantError(
            "ce poste de caisse n'a pas de compte rattaché (paramétrage)"
        )

    session = CaisseSession(
        agency_id=courant.agency_id,
        caissier_id=courant.user_id,
        poste_id=poste.id,
        compte_caisse_id=poste.compte_caisse_id,
        fonds_initial=fonds_initial,
        created_by=courant.user_id,
        updated_by=courant.user_id,
    )
    db.add(session)
    db.flush()

    ecrire_audit(
        db,
        action="caisse.session.ouverte",
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=session.id,
        agency_id=courant.agency_id,
        new_values={"fonds_initial": fonds_initial},
    )
    return session


def resoudre_session_active(db: Session, caissier_id: uuid.UUID) -> CaisseSession:
    """LE point de contrôle centralisé (Bloc C2) que chaque guichet (parts C3, épargne C4,
    crédit décaissement C5, crédit remboursement C6) appellera dans la branche où IL résout
    lui-même le compte de caisse (ex. `compte_debit is None`) — jamais en tête de fonction.
    C'est cette place, pas une autre, qui exemptera structurellement le prélèvement
    automatique (CR5d) : il fournit toujours `compte_source_id`/`compte_debit` explicitement
    et n'atteindra jamais cette branche, donc jamais cet appel — pas un cas spécial ajouté
    après coup, une conséquence de où le contrôle est posé.

    Prend `caissier_id: uuid.UUID`, pas `UtilisateurCourant` : `decaisser()` et `rembourser()`
    ne portent pas ce type aujourd'hui (l'un l'a en option, l'autre pas du tout — `par:
    uuid.UUID` seulement) ; élargir leur signature publique pour ce seul besoin aurait été un
    changement plus large que nécessaire.

    ISOLÉE ici (Bloc C2) : RIEN NE L'APPELLE ENCORE. Le câblage guichet par guichet vient aux
    blocs suivants (C3 à C6), un commit séparé par guichet, la suite complète relancée à
    chaque bascule.

    `compte_caisse_id` de la session retournée est déjà ANCRÉ (voir `ouvrir_session`) — jamais
    recalculé ici, même si le rattachement du poste a changé depuis l'ouverture.

    Aucune session ouverte -> `AucuneSessionOuverteError` : pas un objet manquant (donc pas
    404), un état préalable non rempli — même famille que SessionDejaOuverteError /
    SessionDejaFermeeError, laissée au routeur appelant de traduire en 422."""
    session = db.execute(
        select(CaisseSession).where(
            CaisseSession.caissier_id == caissier_id, CaisseSession.status == "ouverte"
        )
    ).scalar_one_or_none()
    if session is None:
        raise AucuneSessionOuverteError(
            "Aucune session de caisse ouverte : ouvrez votre session avant d'effectuer cette "
            "opération."
        )
    return session


def _charger_session_de_lacteur(
    db: Session, courant: UtilisateurCourant, session_id: uuid.UUID, *, verrou: bool = False
) -> CaisseSession:
    """Charge une session qui appartient à L'ACTEUR — hors périmètre ou inexistante ->
    SessionIntrouvableError (404), jamais 403 : on ne révèle pas qu'une session d'un autre
    caissier existe. Contrôle au niveau de l'OBJET, pas seulement de la route (IDOR).

    `verrou=True` (fermeture, une mutation) sérialise une double fermeture concurrente ; une
    simple lecture ne verrouille jamais (coûterait la ligne pour toute la durée de la requête)."""
    stmt = select(CaisseSession).where(
        CaisseSession.id == session_id, CaisseSession.caissier_id == courant.user_id
    )
    if verrou:
        stmt = stmt.with_for_update()
    session = db.execute(stmt).scalar_one_or_none()
    if session is None:
        raise SessionIntrouvableError()
    return session


def calculer_solde_theorique(
    db: Session, session: CaisseSession, *, jusqu_a: datetime | None = None
) -> int:
    """fonds_initial + Σ mouvements de caisse VALIDÉS du CAISSIER de cette session, sur son
    compte de caisse ancré, entre l'ouverture et `jusqu_a` (par défaut : maintenant — utile pour
    un aperçu en cours de session, avant toute fermeture).

    Calcul PUR sur les écritures déjà postées (`journal_entries.status = 'validee'`) — même
    discipline que `epargne.rapprochement.rapprocher()` : jamais un solde stocké qu'on relirait,
    toujours recalculable à l'identique tant que les écritures (immuables) n'ont pas changé.

    101111 (Caisse) est de sens NORMAL DÉBITEUR : un dépôt (D CAISSE) l'augmente, un retrait
    (C CAISSE) le diminue — solde = Σ débits - Σ crédits, PAS l'inverse de `rapprocher()` (qui
    rapproche un compte de sens créditeur, une dette envers les membres)."""
    borne = jusqu_a
    if borne is None:
        borne = db.execute(text("SELECT NOW()")).scalar_one()

    mouvements = db.execute(
        text(
            "SELECT COALESCE(SUM(CASE WHEN jl.side = 'D' THEN jl.amount ELSE -jl.amount END), 0) "
            "FROM comptabilite.journal_lines jl "
            "JOIN comptabilite.journal_entries je ON je.id = jl.entry_id "
            "WHERE jl.account_id = :compte "
            "AND je.status = 'validee' "
            "AND je.created_by = :caissier "
            "AND je.posted_at >= :ouverture "
            "AND je.posted_at <= :borne"
        ),
        {
            "compte": session.compte_caisse_id,
            "caissier": session.caissier_id,
            "ouverture": session.opened_at,
            "borne": borne,
        },
    ).scalar_one()
    return session.fonds_initial + int(mouvements)


@dataclass(frozen=True)
class ResultatFermeture:
    """Ce que la fermeture rend — l'écart est déjà calculé, jamais à recalculer côté écran."""

    session: CaisseSession
    solde_theorique: int
    ecart: int


def fermer_session(
    db: Session,
    courant: UtilisateurCourant,
    session_id: uuid.UUID,
    *,
    montant_reel: int,
    contexte: ContexteRequete = CONTEXTE_VIDE,
) -> ResultatFermeture:
    """Ferme la session de L'ACTEUR : calcule et FIGE le solde théorique et l'écart, à cet
    instant précis. NE BLOQUE JAMAIS (CA2 : la politique de tolérance/motif viendra plus tard,
    sans jamais empêcher la fermeture elle-même — un caissier ne reste pas coincé au guichet).
    NE POSE AUCUNE ÉCRITURE (CA3)."""
    session = _charger_session_de_lacteur(db, courant, session_id, verrou=True)
    if session.status != "ouverte":
        raise SessionDejaFermeeError(
            f"Cette session est déjà fermée depuis le {session.closed_at}."
        )

    maintenant = db.execute(text("SELECT NOW()")).scalar_one()
    theorique = calculer_solde_theorique(db, session, jusqu_a=maintenant)
    ecart = montant_reel - theorique

    avant = {"status": session.status}
    session.montant_reel_cloture = montant_reel
    session.solde_theorique_cloture = theorique
    session.ecart = ecart
    session.closed_at = maintenant
    session.status = "fermee"
    session.updated_by = courant.user_id
    db.flush()

    ecrire_audit(
        db,
        action="caisse.session.fermee",
        contexte=contexte,
        acteur_id=courant.user_id,
        resource_type=RESSOURCE,
        resource_id=session.id,
        agency_id=session.agency_id,
        old_values=avant,
        new_values={
            "status": "fermee",
            "fonds_initial": session.fonds_initial,
            "montant_reel_cloture": montant_reel,
            "solde_theorique_cloture": theorique,
            "ecart": ecart,
        },
    )
    return ResultatFermeture(session=session, solde_theorique=theorique, ecart=ecart)


def session_ouverte_de_lacteur(
    db: Session, courant: UtilisateurCourant
) -> CaisseSession | None:
    """La session actuellement ouverte de l'acteur, ou None — pour qu'un écran sache s'il doit
    proposer « ouvrir » ou « fermer » sans deviner."""
    return db.execute(
        select(CaisseSession).where(
            CaisseSession.caissier_id == courant.user_id, CaisseSession.status == "ouverte"
        )
    ).scalar_one_or_none()


def _condition_lecture(courant: UtilisateurCourant) -> ColumnElement[bool]:
    """La condition de visibilité en LECTURE (lire_session / lister_sessions_manquantes) :
    TOUJOURS ses propres sessions ; en plus, celles du périmètre si caisse.session.read.autres
    est détenue. Point UNIQUE — si demain un troisième public apparaît, c'est ici qu'il se
    branche, pas recopié dans chaque fonction."""
    if PERMISSION_LECTURE_AUTRES not in courant.permissions:
        return CaisseSession.caissier_id == courant.user_id
    return or_(
        CaisseSession.caissier_id == courant.user_id,
        courant.condition_perimetre(CaisseSession.agency_id),
    )


def lire_session(db: Session, courant: UtilisateurCourant, session_id: uuid.UUID) -> CaisseSession:
    """Lit UNE session : TOUJOURS la sienne ; une autre SEULEMENT avec caisse.session.read.autres
    ET dans le périmètre de l'acteur. Hors périmètre ou inexistante -> SessionIntrouvableError
    (404, jamais 403 : IDOR, on ne révèle pas qu'une session hors de portée existe)."""
    session = db.execute(
        select(CaisseSession).where(
            CaisseSession.id == session_id, _condition_lecture(courant)
        )
    ).scalar_one_or_none()
    if session is None:
        raise SessionIntrouvableError()
    return session


@dataclass(frozen=True)
class LigneSessionManquante:
    """Une session fermée avec un MANQUANT (écart < 0) — identité déjà résolue, comme
    `audit.consultation.LigneAudit`. Jamais l'excédent : hors périmètre de la lettre de demande
    d'explication (décision explicite, à confirmer séparément si un jour souhaité)."""

    id: uuid.UUID
    caissier_id: uuid.UUID
    caissier_nom: str
    agency_id: uuid.UUID
    agency_nom: str
    compte_caisse_number: str
    fonds_initial: int
    opened_at: datetime
    closed_at: datetime
    montant_reel_cloture: int
    solde_theorique_cloture: int
    ecart: int


@dataclass(frozen=True)
class PageSessionsManquantes:
    lignes: Sequence[LigneSessionManquante]
    total: int
    page: int
    taille: int


def lister_sessions_manquantes(
    db: Session,
    courant: UtilisateurCourant,
    *,
    page: int = 1,
    taille: int = TAILLE_PAGE_DEFAUT,
) -> PageSessionsManquantes:
    """Sessions FERMÉES avec un manquant, dans le périmètre de lecture de l'acteur (voir
    `_condition_lecture`) — c'est ce qui permet de retrouver une lettre de demande
    d'explication plus tard, sans dépendre d'un lien reçu au moment de la fermeture (le
    caissier retrouve les SIENNES ; un responsable/audit/direction, celles de son périmètre).
    Triée par fermeture la plus RÉCENTE d'abord."""
    taille = max(1, min(taille, TAILLE_PAGE_MAX))
    page = max(1, page)

    conditions = (
        CaisseSession.status == "fermee",
        CaisseSession.ecart < 0,
        _condition_lecture(courant),
    )

    total = db.execute(
        select(func.count()).select_from(CaisseSession).where(*conditions)
    ).scalar_one()

    lignes = db.execute(
        select(
            CaisseSession.id,
            CaisseSession.caissier_id,
            func.concat_ws(" ", User.first_name, User.last_name).label("caissier_nom"),
            CaisseSession.agency_id,
            Agency.name.label("agency_nom"),
            Account.account_number.label("compte_caisse_number"),
            CaisseSession.fonds_initial,
            CaisseSession.opened_at,
            CaisseSession.closed_at,
            CaisseSession.montant_reel_cloture,
            CaisseSession.solde_theorique_cloture,
            CaisseSession.ecart,
        )
        .select_from(CaisseSession)
        .join(User, User.id == CaisseSession.caissier_id)
        .join(Agency, Agency.id == CaisseSession.agency_id)
        .join(Account, Account.id == CaisseSession.compte_caisse_id)
        .where(*conditions)
        .order_by(CaisseSession.closed_at.desc())
        .offset((page - 1) * taille)
        .limit(taille)
    ).all()

    return PageSessionsManquantes(
        lignes=[LigneSessionManquante(**ligne._mapping) for ligne in lignes],
        total=total,
        page=page,
        taille=taille,
    )
